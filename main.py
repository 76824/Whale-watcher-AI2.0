
import os
import time
import json
from typing import Dict, List, Tuple, Optional
import requests
from flask import Flask, jsonify, request, make_response
from flask_cors import CORS

KRAKEN_BASE = "https://api.kraken.com/0/public"

app = Flask(__name__)
CORS(app)

# ---------- Config / Universe ----------

DEFAULT_UNIVERSE = [
    "BTC", "ETH", "SOL", "XRP", "LINK", "ADA",
    "DOGE", "DOT", "ATOM", "AVAX", "LTC", "TRX"
]

def load_universe() -> List[str]:
    # Env var takes precedence; else config.json; else default
    csv_env = os.getenv("CHENDA_UNIVERSE", "").strip()
    if csv_env:
        return [s.strip().upper() for s in csv_env.split(",") if s.strip()]
    cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        arr = cfg.get("universe") or []
        if arr:
            return [s.strip().upper() for s in arr if s.strip()]
    except Exception:
        pass
    return DEFAULT_UNIVERSE

UNIVERSE = load_universe()

# ---------- Kraken asset-pair cache ----------

_ASSET_CACHE = {
    "ts": 0.0,
    "pairname_to_alt": {},   # e.g., "XXBTZUSD" -> "XBTUSD"
    "alt_to_pairname": {},   # e.g., "XBTUSD" -> "XXBTZUSD"
    "altnames": set(),       # set of altnames
}

SYNONYMS = {
    "BTC": ["BTC", "XBT"],
    # Kraken has used XDG historically, but altname is typically DOGE now.
    "DOGE": ["DOGE", "XDG"],
}

def _refresh_asset_pairs(force: bool = False) -> None:
    """Fetch and cache all Kraken AssetPairs (altname and pairname mappings)."""
    if not force and time.time() - _ASSET_CACHE["ts"] < 3600 and _ASSET_CACHE["altnames"]:
        return
    resp = requests.get(f"{KRAKEN_BASE}/AssetPairs", timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(",".join(data["error"]))
    result = data.get("result", {})
    pairname_to_alt = {}
    alt_to_pairname = {}
    altnames = set()
    for pairname, info in result.items():
        alt = info.get("altname")
        if not alt:
            continue
        pairname_to_alt[pairname] = alt
        # Note: altname appears unique
        alt_to_pairname[alt] = pairname
        altnames.add(alt)
    _ASSET_CACHE.update({
        "ts": time.time(),
        "pairname_to_alt": pairname_to_alt,
        "alt_to_pairname": alt_to_pairname,
        "altnames": altnames,
    })

def best_alt_for_symbol(symbol: str) -> Optional[str]:
    """Choose the best altname for a symbol: prefer USD, else USDT; handle synonyms like BTC->XBT."""
    _refresh_asset_pairs()
    altnames = _ASSET_CACHE["altnames"]
    names = [symbol] + SYNONYMS.get(symbol, [])
    # Prefer USD
    for n in names:
        alt = f"{n}USD"
        if alt in altnames:
            return alt
    # Fallback to USDT
    for n in names:
        alt = f"{n}USDT"
        if alt in altnames:
            return alt
    return None

def alt_to_pairname(alt: str) -> Optional[str]:
    _refresh_asset_pairs()
    return _ASSET_CACHE["alt_to_pairname"].get(alt)

# ---------- Data fetch ----------

def fetch_tickers(symbols: List[str]) -> Dict[str, Dict]:
    """Return mapping symbol -> {price, alt, pairname}"""
    mapping = {}
    # Map symbols to alts and pairnames
    alts = []
    sym_for_alt = {}
    for sym in symbols:
        alt = best_alt_for_symbol(sym)
        if not alt:
            continue
        pairname = alt_to_pairname(alt)
        if not pairname:
            continue
        alts.append((alt, pairname))
        sym_for_alt[alt] = sym
    if not alts:
        return mapping

    # Query tickers in one call
    pairnames = ",".join(p for _, p in alts)
    r = requests.get(f"{KRAKEN_BASE}/Ticker", params={"pair": pairnames}, timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        # If any pair failed, try per-pair to salvage others
        result = {}
        for alt, pairname in alts:
            rr = requests.get(f"{KRAKEN_BASE}/Ticker", params={"pair": pairname}, timeout=8)
            rr.raise_for_status()
            dj = rr.json()
            if dj.get("error"):
                continue
            result.update(dj.get("result", {}))
    else:
        result = data.get("result", {})

    # Build mapping using cached pairname->alt
    _refresh_asset_pairs()
    p2a = _ASSET_CACHE["pairname_to_alt"]

    for pairname, info in result.items():
        alt = p2a.get(pairname, pairname)  # fallback
        sym = sym_for_alt.get(alt)
        if not sym:
            continue
        # last trade price "c"[0], bid "b"[0], ask "a"[0]
        try:
            last = float(info["c"][0])
        except Exception:
            # try bid
            last = float(info["b"][0])
        mapping[sym] = {
            "price": last,
            "alt": alt,
            "pairname": pairname,
        }
    return mapping

def fetch_orderbooks(pairs: List[str], count: int = 20) -> Dict[str, Dict[str, List[List[float]]]]:
    """Fetch depth for multiple pairnames; return pairname -> {bids, asks} (numeric lists)."""
    if not pairs:
        return {}
    r = requests.get(f"{KRAKEN_BASE}/Depth", params={"pair": ",".join(pairs), "count": count}, timeout=12)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        # fallback per-pair
        result = {}
        for p in pairs:
            rr = requests.get(f"{KRAKEN_BASE}/Depth", params={"pair": p, "count": count}, timeout=8)
            rr.raise_for_status()
            dj = rr.json()
            if dj.get("error"):
                continue
            result.update(dj.get("result", {}))
        return result
    return data.get("result", {})

# ---------- Signal logic ----------

def dollars(levels: List[List[float]], take: int = 10) -> float:
    # levels are [price, volume, ts]
    total = 0.0
    for row in levels[:take]:
        try:
            px = float(row[0])
            qty = float(row[1])
            total += px * qty
        except Exception:
            continue
    return total

def bias_from(imbalance: float) -> Tuple[str, str]:
    # imbalance in [-1, 1]; positive -> bid pressure
    if imbalance > 0.18:
        return "BUY", "Strong bid-side pressure vs asks."
    if imbalance > 0.06:
        return "HOLD", "Bids slightly outweigh asks; watch for continuation."
    if imbalance < -0.18:
        return "SELL", "Strong ask-side pressure vs bids."
    if imbalance < -0.06:
        return "HOLD", "Asks slightly outweigh bids; momentum uncertain."
    return "HOLD", "Order book is balanced; no clear edge."

# Cache last signal to reduce rate (simple 2s cache)
_LAST_SIGNAL = {"ts": 0.0, "payload": None}

def compute_signal(symbols: List[str]) -> Dict:
    # 2s cache
    now = time.time()
    if _LAST_SIGNAL["payload"] and now - _LAST_SIGNAL["ts"] < 2.0:
        return _LAST_SIGNAL["payload"]

    prices = fetch_tickers(symbols)
    pairnames = [v["pairname"] for v in prices.values() if "pairname" in v]
    books_raw = fetch_orderbooks(pairnames, count=25)

    # We need mapping pairname -> symbol
    _refresh_asset_pairs()
    p2a = _ASSET_CACHE["pairname_to_alt"]
    alt_to_sym = {v["alt"]: s for s, v in prices.items()}

    scan_rows = []
    overview_rows = []

    for pairname, ob in books_raw.items():
        alt = p2a.get(pairname, pairname)
        sym = alt_to_sym.get(alt)
        if not sym:
            continue
        bids = ob.get("bids", [])
        asks = ob.get("asks", [])
        bid_usd = dollars(bids, 10)
        ask_usd = dollars(asks, 10)
        total = max(bid_usd, ask_usd, 1.0)
        imbalance = (bid_usd - ask_usd) / total
        bias, why = bias_from(imbalance)
        px = prices[sym]["price"]

        scan_rows.append({
            "symbol": sym,
            "price": px,
            "whale_usd": round(bid_usd + ask_usd, 2),
            "bias": bias,
            "imbalance": round(imbalance, 4),
        })
        overview_rows.append({
            "symbol": sym,
            "price": px,
            "bias": bias,
            "why": why,
        })

    scan_rows.sort(key=lambda r: (abs(r["imbalance"]), r["whale_usd"]), reverse=True)
    universe = []
    for sym in symbols:
        alt = best_alt_for_symbol(sym)
        if alt:
            universe.append({"symbol": sym, "pair": alt})

    payload = {
        "ts": int(time.time()*1000),
        "universe": universe,
        "overview": overview_rows,
        "scan": scan_rows[:20],
        "raw": {
            "symbols": symbols,
        }
    }
    _LAST_SIGNAL.update({"ts": now, "payload": payload})
    return payload

# ---------- Routes ----------

@app.route("/status")
def status():
    try:
        _refresh_asset_pairs()
        return jsonify({
            "ok": True,
            "universe": UNIVERSE,
            "pairs_cached": len(_ASSET_CACHE["altnames"]),
            "ts": int(time.time()*1000)
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/signal")
def signal():
    try:
        # Allow ?symbols=CSV override
        csv = request.args.get("symbols", "").strip()
        syms = [s.strip().upper() for s in csv.split(",") if s.strip()] if csv else UNIVERSE
        payload = compute_signal(syms)
        resp = make_response(jsonify(payload), 200)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/chat", methods=["POST"])
def chat():
    try:
        data = request.get_json(force=True, silent=True) or {}
        q = (data.get("q") or "").strip()
        if not q:
            return jsonify({"reply": "Ask me about a symbol (e.g., 'Why BUY SOL?') or say 'top' to see strongest signals."})
        q_up = q.upper()
        if "TOP" in q_up:
            sig = compute_signal(UNIVERSE)
            rows = sig.get("scan", [])[:5]
            if not rows:
                return jsonify({"reply": "No fresh signals yet. Try again in a moment."})
            lines = [f"{r['symbol']}: {r['bias']} (imbalance {r['imbalance']:+.2f}, whale ${r['whale_usd']:,})" for r in rows]
            return jsonify({"reply": "Top signals:\n" + "\n".join(lines)})
        # Find symbol mentioned
        sym = None
        for s in UNIVERSE:
            if s in q_up:
                sym = s
                break
        if not sym:
            return jsonify({"reply": "Tell me a symbol (e.g., 'XRP') or 'top' for strongest signals."})
        sig = compute_signal([sym])
        ov = next((o for o in sig.get("overview", []) if o["symbol"] == sym), None)
        if not ov:
            return jsonify({"reply": f"I couldn't fetch fresh data for {sym} yet. Try again shortly."})
        reply = f"{sym}: {ov['bias']} at ~{ov['price']}. Reason: {ov['why']}"
        return jsonify({"reply": reply})
    except Exception as e:
        return jsonify({"reply": f"Error: {e}"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
