# Chenda backend (Kraken-only) — Flask
# Ready for Render. Provides:
#   GET /            -> health check
#   GET /symbols     -> list of symbols from config
#   GET /signal      -> live snapshot (price + whale levels)
#   GET /status      -> uptime and cache info
#   POST /chat       -> simple rule‑based "Chenda" chat over latest snapshot
#
# Environment (optional):
#   PORT                (Render injects this automatically)
#   CONFIG_PATH         path to config.json (default: ./config.json)
#   SNAPSHOT_TTL_SEC    override cache ttl (seconds)
#
# Requirements (requirements.txt):
#   Flask==3.0.3
#   flask-cors==4.0.0
#   requests==2.32.3
#   python-dotenv==1.0.1 (optional)
#
# To run locally:
#   export FLASK_APP=main:app && flask run -p 5000
# On Render:
#   gunicorn -w 1 -b 0.0.0.0:$PORT main:app

from __future__ import annotations

import os
import time
import math
import json
from dataclasses import dataclass, field
from typing import List, Dict, Any, Tuple

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

APP_START_TS = time.time()

def _load_cfg() -> dict:
    cfg_path = os.getenv("CONFIG_PATH") or os.path.join(os.path.dirname(__file__), "config.json")
    # Render working dir is /opt/render/project/src; relative works.
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        # Safe defaults
        cfg = {
            "backend_name": "chenda",
            "kraken_pairs": ["XRP/USD", "SOL/USD", "LINK/USD"],
            "min_whale_usd": 250000,
            "max_levels": 50,
            "snapshot_ttl_sec": 20,
            "allow_binance": False
        }
    # Allow ENV override of symbols as CSV (e.g., "XRP,SOL,LINK")
    sym_csv = os.getenv("CHENDA_SYMBOLS")
    if sym_csv:
        cfg["kraken_pairs"] = [s.strip().upper() + "/USD" if "/" not in s else s.strip().upper() for s in sym_csv.split(",") if s.strip()]
    # Allow ENV override of TTL
    ttl_env = os.getenv("SNAPSHOT_TTL_SEC")
    if ttl_env and ttl_env.isdigit():
        cfg["snapshot_ttl_sec"] = int(ttl_env)
    return cfg

CFG = _load_cfg()

# ---- Kraken helpers ---------------------------------------------------------

# Kraken Depth/Ticker use "altname" query but return "result" keyed by "wsname" or legacy code.
# We'll query by altname (e.g., XRPUSD,SOLUSD,LINKUSD) and normalize the response.

def _pair_altname(pair: str) -> str:
    # "XRP/USD" -> "XRPUSD"
    p = pair.replace("/", "").upper()
    return p

def _wsname_from_result_key(key: str) -> str:
    # Many keys are like "XXRPZUSD" with wsname "XRP/USD" inside Ticker meta.
    # If not present we try to infer.
    # For display in our API we stick to "COIN".
    for sep in ["/", ":"]:
        if sep in key:
            base = key.split(sep)[0]
            return base
    # Strip leading X/Z if present (legacy asset codes)
    base = key.replace("X", "").replace("Z", "")
    # Heuristic: letters until USD/USDT/EUR found
    for quote in ["USD", "USDT", "EUR"]:
        if base.endswith(quote):
            return base[: -len(quote)]
    return base

def _kraken_get(url: str, params: dict) -> dict:
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"Kraken error: {data['error']}")
    return data["result"]

def _fetch_ticker(pairs_alt: List[str]) -> Dict[str, dict]:
    # Returns dict keyed by result key with "c" last trade price list
    res = _kraken_get("https://api.kraken.com/0/public/Ticker", {"pair": ",".join(pairs_alt)})
    return res

def _fetch_depth(pair_alt: str, count: int = 50) -> dict:
    res = _kraken_get("https://api.kraken.com/0/public/Depth", {"pair": pair_alt, "count": count})
    # res is keyed by some legacy name, take first value
    key = next(iter(res.keys()))
    return res[key]

def _to_float(x: Any) -> float:
    try:
        return float(x)
    except Exception:
        return math.nan

def _aggregate_whales(levels: List[List[Any]], min_usd: float, price_hint: float) -> List[dict]:
    # Kraken levels entries: [price, volume, timestamp]
    whales = []
    for level in levels:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        price = _to_float(level[0])
        qty = _to_float(level[1])
        if math.isnan(price) or math.isnan(qty):
            continue
        usd = price * qty
        if usd >= min_usd:
            whales.append({"price": round(price, 6), "qty": round(qty, 6), "usd": round(usd, 3)})
    # Sort high to low USD
    whales.sort(key=lambda x: x["usd"], reverse=True)
    return whales

# ---- Cache ------------------------------------------------------------------

_last_snapshot: dict | None = None
_last_snapshot_ts: float | None = None

def _compute_snapshot() -> dict:
    symbols = [p.split("/")[0] for p in CFG.get("kraken_pairs", [])]
    alt_list = [_pair_altname(p) for p in CFG.get("kraken_pairs", [])]
    min_whale = float(CFG.get("min_whale_usd", 250000))
    max_levels = int(CFG.get("max_levels", 50))

    # Fetch ticker once
    ticker = _fetch_ticker(alt_list)

    items = []
    for pair_alt, pair_human in zip(alt_list, CFG.get("kraken_pairs", [])):
        try:
            depth = _fetch_depth(pair_alt, count=max_levels)
        except Exception as e:
            depth = {"asks": [], "bids": []}

        # Find a ticker row for this pair (result is keyed by some internal name)
        # We try alt first; else any key whose wsname matches.
        last_price = None
        for k, v in ticker.items():
            # v might have "c": ["101.234", "1"]
            if "c" in v and isinstance(v["c"], list) and len(v["c"]) >= 1:
                # best we can do is accept the first that contains the base symbol
                base = pair_human.split("/")[0]
                # Kraken "wsname" gives human pair; prefer that
                wsname = v.get("wsname") or v.get("W", None)
                if wsname:
                    if wsname.replace(" ", "") == pair_human:
                        last_price = _to_float(v["c"][0])
                        break
                    if wsname.startswith(base):
                        last_price = _to_float(v["c"][0])
                else:
                    if base in k:
                        last_price = _to_float(v["c"][0])
        if last_price is None:
            last_price = float("nan")

        asks = _aggregate_whales(depth.get("asks", []), min_whale, last_price)
        bids = _aggregate_whales(depth.get("bids", []), min_whale, last_price)

        items.append({
            "book_venues": ["kraken"],
            "price": last_price,
            "price_venue": "kraken",
            "symbol": pair_human.split("/")[0],
            "ts": int(time.time() * 1000),
            "whales": {
                "asks": asks,
                "bids": bids
            }
        })

    snapshot = {"data": items}
    return snapshot

def _get_snapshot_cached() -> dict:
    global _last_snapshot, _last_snapshot_ts
    ttl = int(CFG.get("snapshot_ttl_sec", 20))
    now = time.time()
    if _last_snapshot is None or _last_snapshot_ts is None or (now - _last_snapshot_ts) > ttl:
        try:
            _last_snapshot = _compute_snapshot()
            _last_snapshot_ts = now
        except Exception as e:
            # keep previous snapshot if fetch fails
            if _last_snapshot is None:
                _last_snapshot = {"data": []}
                _last_snapshot_ts = now
    return _last_snapshot

# ---- Flask app --------------------------------------------------------------

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

@app.get("/")
def root():
    return jsonify({
        "ok": True,
        "service": "chenda-backend",
        "version": "2025-08-16",
        "uptime_sec": round(time.time() - APP_START_TS, 2)
    })

@app.get("/symbols")
def symbols():
    syms = [p.split("/")[0] for p in CFG.get("kraken_pairs", [])]
    return jsonify({"symbols": syms})

@app.get("/signal")
def signal():
    snap = _get_snapshot_cached()
    return jsonify(snap)

@app.get("/status")
def status():
    syms = [p.split("/")[0] for p in CFG.get("kraken_pairs", [])]
    ttl = int(CFG.get("snapshot_ttl_sec", 20))
    return jsonify({
        "ok": True,
        "source": "kraken-only",
        "symbols": syms,
        "symbols_count": len(syms),
        "ttl_sec": ttl,
        "last_refresh_ts": int((_last_snapshot_ts or 0) * 1000),
        "uptime_sec": round(time.time() - APP_START_TS, 2),
    })

# ---- Simple "Chenda" chat ---------------------------------------------------

def _aggregate_pressure(items: List[dict]) -> Tuple[float, float]:
    """Return (bid_usd, ask_usd) totals across all symbols top whale levels."""
    bid_total = 0.0
    ask_total = 0.0
    for it in items:
        for b in it.get("whales", {}).get("bids", [])[:5]:
            bid_total += float(b.get("usd", 0))
        for a in it.get("whales", {}).get("asks", [])[:5]:
            ask_total += float(a.get("usd", 0))
    return bid_total, ask_total

@app.post("/chat")
def chat():
    payload = request.get_json(silent=True) or {}
    msg = (payload.get("message") or "").strip().lower()
    snap = _get_snapshot_cached()
    items = snap.get("data", [])
    bid_usd, ask_usd = _aggregate_pressure(items)

    def reply(text: str, extra: dict | None = None):
        out = {
            "ok": True,
            "author": "Chenda",
            "message": text,
            "meta": {
                "bid_usd_top5": round(bid_usd, 2),
                "ask_usd_top5": round(ask_usd, 2),
                "symbols": [it.get("symbol") for it in items]
            }
        }
        if extra:
            out.update(extra)
        return jsonify(out)

    if not msg:
        return reply("Tell me what you want to know. Try: 'symbols', 'status', or 'bias?'.")

    if "help" in msg:
        return reply("Try: 'symbols', 'status', 'bias on XRP', 'bias?' or 'explain whales'.")

    if "symbol" in msg or "symbols" in msg or "list" in msg:
        syms = ", ".join([it.get("symbol") for it in items]) or "—"
        return reply(f"I'm tracking: {syms}.")

    if "status" in msg:
        return reply(f"I'm up and watching Kraken. Cache TTL {CFG.get('snapshot_ttl_sec', 20)}s. Last refresh just now.")

    # Coin‑specific bias
    for it in items:
        sym = it.get("symbol", "").lower()
        if sym and sym in msg:
            bsum = sum(float(b.get("usd", 0)) for b in it.get("whales", {}).get("bids", [])[:5])
            asum = sum(float(a.get("usd", 0)) for a in it.get("whales", {}).get("asks", [])[:5])
            if bsum > asum * 1.12:
                return reply(f"{sym.upper()}: buy bias (bids outweigh asks).")
            elif asum > bsum * 1.12:
                return reply(f"{sym.upper()}: sell bias (asks outweigh bids).")
            else:
                return reply(f"{sym.upper()}: neutral / hold (balanced flows).")

    # Global bias
    if "bias" in msg or "buy" in msg or "sell" in msg or "hold" in msg:
        if bid_usd > ask_usd * 1.1:
            return reply("Market buy bias across tracked pairs (bids > asks).")
        elif ask_usd > bid_usd * 1.1:
            return reply("Market sell bias across tracked pairs (asks > bids).")
        else:
            return reply("Market neutral / hold (flows balanced).")

    if "whale" in msg:
        return reply("I flag orderbook levels where price*quantity exceeds your min_whale_usd in config.json.")

    return reply("Got it. (I can answer about symbols, status, and buy/sell/hold bias.)")

# ---- Main guard for local run ----------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
