
import os
import time
import math
import json
import re
from datetime import datetime, timezone
from functools import lru_cache

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

APP_T0 = time.time()

# ---------------- Configuration ----------------
CFG_PATH = os.environ.get("CHENDA_CONFIG", "config.json")

DEFAULT_CFG = {
    "exchange": "kraken",
    "discover": True,
    "discover_top_n": 12,
    "symbols": ["XRP", "SOL", "LINK"],
    "min_whale_usd": 250000,
    "snapshot_ttl_sec": 20,
    "ohlc_points": 30,
    "log_reasons": False
}

def _load_cfg():
    cfg = DEFAULT_CFG.copy()
    if os.path.exists(CFG_PATH):
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
            cfg.update(on_disk or {})
    # Allow env overrides
    if os.environ.get("CHENDA_SYMBOLS"):
        cfg["symbols"] = [s.strip().upper() for s in os.environ["CHENDA_SYMBOLS"].split(",") if s.strip()]
    if os.environ.get("MIN_WHALE_USD"):
        cfg["min_whale_usd"] = float(os.environ["MIN_WHALE_USD"])
    if os.environ.get("SNAPSHOT_TTL_SEC"):
        cfg["snapshot_ttl_sec"] = int(os.environ["SNAPSHOT_TTL_SEC"])
    return cfg

CFG = _load_cfg()

# CORS allowlist
ALLOW_ORIGINS = os.environ.get("CORS_ALLOW_ORIGINS", "*")
allow_origins = [o.strip() for o in ALLOW_ORIGINS.split(",")] if ALLOW_ORIGINS else ["*"]

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": allow_origins}}, supports_credentials=False)

# ---------------- Kraken helpers ----------------
KRAKEN_API = "https://api.kraken.com/0/public"

@lru_cache(maxsize=1)
def _asset_pairs():
    """Fetch and cache all Kraken asset pairs with mapping to altname and wsname."""
    url = f"{KRAKEN_API}/AssetPairs"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    result = r.json().get("result", {})
    pairs = {}
    for code, meta in result.items():
        alt = meta.get("altname")  # e.g., XRPUSD
        ws = meta.get("wsname")    # e.g., XRP/USD
        base = meta.get("base")
        quote = meta.get("quote")
        pairs[alt] = {"code": code, "altname": alt, "wsname": ws, "base": base, "quote": quote}
    return pairs

def _discover_usd_symbols(top_n=12):
    """Return a list of altnames like XRPUSD for top movers by 24h change."""
    pairs = _asset_pairs()
    usd = {alt for alt, m in pairs.items() if m["altname"] and (m["altname"].endswith("USD") or m["altname"].endswith("USDT"))}
    if not usd:
        return []
    # get 24h change via Ticker (needs pair list)
    alts = list(usd)[:100]  # limit one shot
    chunks = [alts[i:i+50] for i in range(0, len(alts), 50)]
    changes = []
    for chunk in chunks:
        q = ",".join(chunk)
        r = requests.get(f"{KRAKEN_API}/Ticker", params={"pair": q}, timeout=15)
        r.raise_for_status()
        res = r.json().get("result", {})
        for k, v in res.items():
            # v['c'][0]=last, v['o']=today's opening price
            try:
                last = float(v["c"][0])
                openp = float(v["o"])
                chg = (last/openp - 1.0) if openp else 0.0
                changes.append((k, chg))
            except Exception:
                continue
    changes.sort(key=lambda x: abs(x[1]), reverse=True)
    return [k for k, _ in changes[:top_n]]

def _ensure_universe():
    if CFG.get("discover"):
        try:
            alts = _discover_usd_symbols(CFG.get("discover_top_n", 12))
            # map to simple symbols: strip USD/USDT suffix
            simple = sorted({re.sub(r"(USD|USDT)$", "", a) for a in alts})
            return simple[:CFG.get("discover_top_n", 12)] or CFG["symbols"]
        except Exception:
            return CFG["symbols"]
    return CFG["symbols"]

def _alt_from_symbol(symbol):
    # Prefer USD pairs
    pairs = _asset_pairs()
    for suffix in ("USD", "USDT"):
        alt = f"{symbol.upper()}{suffix}"
        if alt in pairs:
            return alt
    # Fall back to first matching alt by wsname base equals symbol
    for alt, m in pairs.items():
        ws = m.get("wsname") or ""
        if ws.startswith(symbol.upper() + "/"):
            return alt
    return None

def _ticker_many(alts):
    """Return Kraken Ticker map for multiple altnames."""
    if not alts:
        return {}
    chunks = [alts[i:i+20] for i in range(0, len(alts), 20)]
    out = {}
    for chunk in chunks:
        q = ",".join(chunk)
        r = requests.get(f"{KRAKEN_API}/Ticker", params={"pair": q}, timeout=15)
        r.raise_for_status()
        res = r.json().get("result", {})
        out.update(res)
    return out

def _depth(alt, count=20):
    r = requests.get(f"{KRAKEN_API}/Depth", params={"pair": alt, "count": count}, timeout=15)
    r.raise_for_status()
    res = r.json().get("result", {})
    # Kraken returns dict keyed by alt
    return res.get(alt) or next(iter(res.values()), {"bids": [], "asks": []})

def _ohlc(alt, interval=1):
    r = requests.get(f"{KRAKEN_API}/OHLC", params={"pair": alt, "interval": interval}, timeout=15)
    r.raise_for_status()
    res = r.json().get("result", {})
    # Return last serieslist
    for k, v in res.items():
        if k == "last":
            continue
        return v[-CFG.get("ohlc_points", 30):]
    return []

# ---------------- Signals & reasons ----------------

def _whale_pressure(depth, min_usd=250000.0, max_levels=10):
    """Compute net whale pressure from orderbook depth."""
    bids = depth.get("bids", [])[:max_levels]
    asks = depth.get("asks", [])[:max_levels]
    def usd(side):
        s = 0.0
        bigs = []
        for p, q, *_ in side:
            p = float(p); q = float(q)
            u = p*q
            if u >= min_usd:
                s += u
                bigs.append({"price": p, "qty": q, "usd": u})
        return s, bigs
    bid_usd, big_bids = usd(bids)
    ask_usd, big_asks = usd(asks)
    total = (bid_usd + ask_usd) or 1.0
    score = (bid_usd - ask_usd) / total  # -1..+1
    return score, big_bids, big_asks

def _momentum_1m(ohlc):
    """Return recent momentum (% change last 5 mins) and volatility (ATR-like)."""
    if len(ohlc) < 6:
        return 0.0, 0.0
    closes = [float(x[4]) for x in ohlc]  # [time,open,high,low,close,vwap,volume,count]
    last = closes[-1]
    prev5 = closes[-6]
    mom = (last/prev5 - 1.0)
    trs = []
    prev_close = closes[0]
    highs = [float(x[2]) for x in ohlc]
    lows = [float(x[3]) for x in ohlc]
    for i in range(1, len(ohlc)):
        tr = max(highs[i]-lows[i], abs(highs[i]-prev_close), abs(lows[i]-prev_close))
        trs.append(tr)
        prev_close = closes[i]
    atr = sum(trs[-14:])/max(1, min(14, len(trs))) if trs else 0.0
    return mom, atr

def _bias_from_features(mom, whale_score):
    # Simple rule blend
    score = 0.0
    score += 0.6 * whale_score
    score += 0.4 * (mom*10)  # scale momentum (~%*10)
    # Clamp
    score = max(-1.0, min(1.0, score))
    if score > 0.25:
        label = "BUY"
    elif score < -0.25:
        label = "SELL"
    else:
        label = "HOLD"
    return label, score

def _entry_plan(price, atr):
    """Generate an entry plan dict with stop/targets based on ATR."""
    if atr <= 0:
        atr = price * 0.005  # fallback 0.5%
    entry = {
        "buy_zone": [round(price*0.995, 6), round(price*1.005, 6)],
        "sell_zone": [round(price*0.995, 6), round(price*1.005, 6)],
        "sl": round(price - 1.5*atr, 6),
        "tp1": round(price + 1.5*atr, 6),
        "tp2": round(price + 3.0*atr, 6),
        "rr": round((3.0*atr) / (1.5*atr + 1e-9), 2)
    }
    return entry

_snapshot_cache = {"ts": 0, "payload": None}

def compute_snapshot():
    # Cache
    ttl = CFG.get("snapshot_ttl_sec", 20)
    now = time.time()
    if _snapshot_cache["payload"] and now - _snapshot_cache["ts"] < ttl:
        return _snapshot_cache["payload"]

    symbols = _ensure_universe()
    alts = [a for a in (_alt_from_symbol(s) for s in symbols) if a]

    ticker = _ticker_many(alts)
    data = []
    whale_levels_all = []
    reasons = {}

    for sym, alt in zip(symbols, alts):
        t = ticker.get(alt) or {}
        price = float(t.get("c", [0])[0]) if t.get("c") else 0.0
        openp = float(t.get("o", 0.0) or 0.0)
        change_24h = (price/openp - 1.0) if openp else 0.0

        depth = _depth(alt, count=20)
        ohlc = _ohlc(alt, interval=1)
        mom, atr = _momentum_1m(ohlc)
        whale_score, big_bids, big_asks = _whale_pressure(depth, CFG.get("min_whale_usd", 250000), 10)
        bias, score = _bias_from_features(mom, whale_score)

        entry = _entry_plan(price, atr)
        reason = {
            "symbol": sym,
            "price": price,
            "bias": bias,
            "score": round(score, 3),
            "explain": {
                "whale_pressure": round(whale_score, 3),
                "mom_5m_pct": round(mom*100, 2),
                "change_24h_pct": round(change_24h*100, 2),
                "atr_approx": round(atr, 6),
            },
            "entry": entry
        }
        reasons[sym] = reason

        # collect whale levels (top few)
        for lvl in big_bids:
            whale_levels_all.append({"symbol": sym, "side": "BID", **lvl})
        for lvl in big_asks:
            whale_levels_all.append({"symbol": sym, "side": "ASK", **lvl})

        data.append({
            "symbol": sym,
            "price": price,
            "price_venue": "kraken",
            "book_venues": ["kraken"],
            "ts": int(time.time()*1000),
            "whales": {
                "bids": big_bids,
                "asks": big_asks
            }
        })

    # sort whales by USD size
    whale_levels_all.sort(key=lambda x: x["usd"], reverse=True)
    snapshot = {
        "ts": int(time.time()*1000),
        "universe": symbols,
        "data": data,
        "whale_levels_top": whale_levels_all[:50],
        "reasons": reasons
    }
    _snapshot_cache["ts"] = time.time()
    _snapshot_cache["payload"] = snapshot
    return snapshot

# ---------------- Routes ----------------
@app.route("/")
def root():
    return "Chenda backend OK", 200

@app.route("/status")
def status():
    snap = compute_snapshot()
    return jsonify({
        "ok": True,
        "busy": False,
        "since": int(APP_T0),
        "symbols": snap["universe"],
        "last_ts": snap["ts"]
    })

@app.route("/symbols")
def symbols():
    # Return discovered symbols with 24h change
    syms = _ensure_universe()
    alts = [a for a in (_alt_from_symbol(s) for s in syms) if a]
    res = []
    t = _ticker_many(alts)
    for s, a in zip(syms, alts):
        tik = t.get(a) or {}
        price = float(tik.get("c", [0])[0]) if tik.get("c") else 0.0
        openp = float(tik.get("o", 0.0) or 0.0)
        change = (price/openp - 1.0) if openp else 0.0
        res.append({"symbol": s, "price": price, "change_24h_pct": round(change*100, 2)})
    return jsonify({"symbols": res})

@app.route("/signal")
def signal():
    snap = compute_snapshot()
    return jsonify(snap)

@app.route("/scan")
def scan():
    """Return top candidates: momentum + whale pressure alignment."""
    snap = compute_snapshot()
    items = []
    for s, r in snap["reasons"].items():
        x = r["explain"]
        combo = 0.6*x["whale_pressure"] + 0.4*(x["mom_5m_pct"]/10.0)
        items.append({
            "symbol": s,
            "bias": r["bias"],
            "score": r["score"],
            "combo": round(combo,3),
            "price": r["price"],
            "entry": r["entry"],
            "explain": x
        })
    # rank by |combo|
    items.sort(key=lambda i: abs(i["combo"]), reverse=True)
    return jsonify({"top": items})

@app.route("/chat", methods=["POST"])
def chat():
    try:
        msg = (request.json or {}).get("message","").strip()
    except Exception:
        msg = ""
    snap = compute_snapshot()
    lower = msg.lower()
    # Try to detect a symbol in message
    sym = None
    for s in snap["universe"]:
        if re.search(rf"\b{s.lower()}\b", lower):
            sym = s
            break
    if not msg or msg == "":
        return jsonify({"reply": "Ask me about a symbol (e.g., 'bias on XRP' or 'best entry for SOL')."})
    if sym:
        r = snap["reasons"].get(sym)
        if not r:
            return jsonify({"reply": f"I don't have data for {sym} right now."})
        x = r["explain"]
        entry = r["entry"]
        why = (
            f"{sym}: {r['bias']} (score {r['score']}). "
            f"Whale pressure {x['whale_pressure']} and 5‑min momentum {x['mom_5m_pct']}%. "
            f"24h change {x['change_24h_pct']}%. ATR≈{x['atr_approx']}. "
            f"Entry zone {entry['buy_zone'][0]}–{entry['buy_zone'][1]}, SL {entry['sl']}, "
            f"TP1 {entry['tp1']}, TP2 {entry['tp2']} (RR~{entry['rr']})."
        )
        return jsonify({"reply": why})
    # Generic questions
    if "top" in lower or "gainer" in lower or "spike" in lower or "entry" in lower:
        scan = requests.get(request.url_root.rstrip("/") + "/scan", timeout=15).json()
        top = scan.get("top", [])[:5]
        lines = []
        for it in top:
            lines.append(f"{it['symbol']}: {it['bias']} (score {it['score']}) @ {it['price']}, "
                         f"entry {it['entry']['buy_zone'][0]}–{it['entry']['buy_zone'][1]}, "
                         f"SL {it['entry']['sl']}, TP1 {it['entry']['tp1']}")
        return jsonify({"reply": "Here are my current top setups:\n" + "\n".join(lines)})
    return jsonify({"reply": "Try: 'why HOLD XRP?', 'best entry SOL', or 'top setups'."})

# ---------------- Error handling ----------------
@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "not found"}), 404

@app.errorhandler(Exception)
def err(e):
    return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
