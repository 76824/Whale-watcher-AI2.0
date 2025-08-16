import os, time, json, math, statistics
from typing import Dict, Any, List
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

APP_VERSION = "chenda-backend v3.1"

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "config.json")

def _load_cfg() -> Dict[str, Any]:
    # Defaults that are safe in case config.json is missing
    cfg = {
        "discover": True,
        "discover_top_n": 10,
        "symbols": [],
        "min_whale_usd": 250000,
        "book_levels": 20,
        "snapshot_ttl_sec": 20,
        "discover_ttl_sec": 900,
        "timeout_sec": 10,
    }
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                cfg.update(data)
    except FileNotFoundError:
        pass
    return cfg

CFG = _load_cfg()

app = Flask(__name__)

# CORS: allow Firebase app and localhost by default
default_origins = os.getenv(
    "CORS_ALLOW_ORIGINS",
    "*"
)
if default_origins == "*" or default_origins.strip() == "*":
    CORS(app)
else:
    origins = [o.strip() for o in default_origins.split(",") if o.strip()]
    CORS(app, resources={r"/*": {"origins": origins}})

# ------------ Kraken helpers ------------

KRAKEN_BASE = "https://api.kraken.com/0/public"

def kget(endpoint: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    url = f"{KRAKEN_BASE}/{endpoint}"
    r = requests.get(url, params=params or {}, timeout=CFG.get("timeout_sec", 10))
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError("; ".join(data["error"]))
    return data["result"]

# Light TTL cache
_CACHE: Dict[str, Dict[str, Any]] = {}

def ttl_cache(key: str, ttl: int):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and (now - hit["ts"] < ttl):
        return hit["value"]
    return None

def set_cache(key: str, value: Any):
    _CACHE[key] = {"value": value, "ts": time.time()}

def discover_universe() -> Dict[str, Any]:
    key = "discover"
    cached = ttl_cache(key, CFG.get("discover_ttl_sec", 900))
    if cached:
        return cached

    pairs = kget("AssetPairs")
    # Map display symbol (e.g., XRP) -> kraken pair code (e.g., XXRPZUSD)
    sym_to_pair: Dict[str, str] = {}
    for pair_code, meta in pairs.items():
        ws = meta.get("wsname") or ""
        if "/USD" in ws or "/USDT" in ws:
            base = ws.split("/")[0].upper()
            sym_to_pair[base] = pair_code

    # Choose symbols
    if CFG.get("symbols"):
        symbols = [s.strip().upper() for s in CFG["symbols"] if s.strip().upper() in sym_to_pair]
    else:
        # Rank by absolute 24h change using Ticker 'o' (open) vs 'c'[0] (last)
        try:
            ticker = kget("Ticker", {"pair": ",".join(sym_to_pair.values())})
        except Exception:
            ticker = {}
        movers: List[tuple[float, str]] = []
        for sym, pair in sym_to_pair.items():
            t = ticker.get(pair, {})
            try:
                last = float((t.get("c") or [0])[0] or 0)
                openp = float(t.get("o") or 0)
            except Exception:
                last, openp = 0.0, 0.0
            chg = abs((last - openp) / openp) if last and openp else 0.0
            movers.append((chg, sym))
        movers.sort(reverse=True)
        top_n = int(CFG.get("discover_top_n", 10))
        symbols = [s for _, s in movers[:top_n]]

    value = {"symbols": symbols, "map": sym_to_pair}
    set_cache(key, value)
    return value

def get_ticker(pair_code: str) -> Dict[str, Any]:
    key = f"ticker:{pair_code}"
    cached = ttl_cache(key, CFG.get("snapshot_ttl_sec", 20))
    if cached:
        return cached
    t = kget("Ticker", {"pair": pair_code})
    val = t.get(pair_code) or next(iter(t.values()), {})
    set_cache(key, val)
    return val

def get_depth(pair_code: str, count: int = 20) -> Dict[str, Any]:
    count = int(CFG.get("book_levels", 20))
    key = f"depth:{pair_code}:{count}"
    cached = ttl_cache(key, CFG.get("snapshot_ttl_sec", 20))
    if cached:
        return cached
    d = kget("Depth", {"pair": pair_code, "count": count})
    val = d.get(pair_code) or next(iter(d.values()), {})
    set_cache(key, val)
    return val

def get_ohlc(pair_code: str, interval: int = 1, bars: int = 60) -> List[List[float]]:
    key = f"ohlc:{pair_code}:{interval}"
    cached = ttl_cache(key, CFG.get("snapshot_ttl_sec", 20))
    if cached:
        data = cached
    else:
        r = kget("OHLC", {"pair": pair_code, "interval": interval})
        data = r.get(pair_code) or next(iter(r.values()), [])
        set_cache(key, data)
    # last 'bars' rows
    return data[-bars:]

def sum_usd(levels: List[List[Any]]) -> float:
    # levels: [price, volume, ts]
    total = 0.0
    for row in levels:
        try:
            price = float(row[0]); vol = float(row[1])
            total += price * vol
        except Exception:
            continue
    return total

def extract_whale_levels(levels: List[List[Any]], min_usd: float, top_n: int = 5) -> List[Dict[str, float]]:
    # Return top_n levels whose price*qty >= min_usd
    enriched = []
    for row in levels:
        try:
            price = float(row[0]); qty = float(row[1]); usd = price * qty
            if usd >= min_usd:
                enriched.append({"price": price, "qty": qty, "usd": usd})
        except Exception:
            continue
    enriched.sort(key=lambda x: x["usd"], reverse=True)
    return enriched[:top_n]

def compute_signal_for_symbol(symbol: str) -> Dict[str, Any]:
    u = discover_universe()
    sym_map = u["map"]
    sym = symbol.upper()
    if sym not in sym_map:
        raise ValueError(f"Unknown symbol '{symbol}' on Kraken (USD/USDT quoted).")
    pair = sym_map[sym]

    ticker = get_ticker(pair)
    try:
        last = float((ticker.get("c") or [0])[0] or 0.0)
        openp = float(ticker.get("o") or 0.0)
    except Exception:
        last, openp = 0.0, 0.0

    depth = get_depth(pair)
    bids = depth.get("bids", [])
    asks = depth.get("asks", [])
    bids_usd = sum_usd(bids)
    asks_usd = sum_usd(asks)

    # 1‑minute OHLC for momentum & ATR‑like range
    ohlc = get_ohlc(pair, interval=1, bars=60)
    closes = [float(x[4]) for x in ohlc[-30:]] if ohlc else []
    highs = [float(x[2]) for x in ohlc[-30:]] if ohlc else []
    lows  = [float(x[3]) for x in ohlc[-30:]] if ohlc else []

    momentum = 0.0
    if len(closes) >= 6:
        momentum = (closes[-1] - closes[-6]) / (closes[-6] or 1.0)

    # ATR‑lite = mean(high-low)
    atr = 0.0
    if highs and lows and len(highs) == len(lows):
        ranges = [h - l for h, l in zip(highs, lows)]
        if ranges:
            atr = statistics.fmean(ranges)

    # Whale pressure score
    whale_score = 0.0
    if (bids_usd + asks_usd) > 0:
        whale_score = (bids_usd - asks_usd) / (bids_usd + asks_usd)

    # Combined score
    score = 0.6 * whale_score + 0.4 * momentum
    bias = "HOLD"
    if score > 0.25:
        bias = "BUY"
    elif score < -0.25:
        bias = "SELL"

    # Suggested entry (simple)
    entry = None
    if atr and last:
        if bias == "BUY":
            entry = {"type": "limit", "zone": [round(last - 0.6 * atr, 6), round(last - 0.2 * atr, 6)],
                     "stop": round(last - 1.2 * atr, 6), "tp": [round(last + 1.5 * atr, 6), round(last + 3.0 * atr, 6)]}
        elif bias == "SELL":
            entry = {"type": "limit", "zone": [round(last + 0.6 * atr, 6), round(last + 0.2 * atr, 6)],
                     "stop": round(last + 1.2 * atr, 6), "tp": [round(last - 1.5 * atr, 6), round(last - 3.0 * atr, 6)]}

    why_parts = []
    if momentum:
        why_parts.append(f"1m momentum {'↑' if momentum>0 else '↓'} {momentum*100:.1f}%")
    if whale_score:
        if whale_score > 0:
            why_parts.append(f"bid depth +{(whale_score*100):.1f}% vs asks")
        else:
            why_parts.append(f"ask pressure {abs(whale_score*100):.1f}% > bids")
    if openp and last:
        day = (last - openp) / openp * 100
        why_parts.append(f"24h {day:+.2f}%")
    why = "; ".join(why_parts) or "Stable for now."

    whales = {
        "asks": extract_whale_levels(asks, float(CFG.get("min_whale_usd", 250000)), top_n=5),
        "bids": extract_whale_levels(bids, float(CFG.get("min_whale_usd", 250000)), top_n=5),
    }

    return {
        "symbol": sym,
        "pair": pair,
        "price": last,
        "price_venue": "kraken",
        "ts": int(time.time()*1000),
        "whales": whales,
        "features": {
            "momentum_1m": momentum,
            "atr_like": atr,
            "bids_usd": bids_usd,
            "asks_usd": asks_usd,
            "score": score,
        },
        "bias": bias,
        "why": why,
        "entry": entry,
    }

def quick_row_for_symbol(symbol: str) -> Dict[str, Any]:
    try:
        sig = compute_signal_for_symbol(symbol)
        return {
            "symbol": sig["symbol"],
            "price": sig["price"],
            "bias": sig["bias"],
            "score": sig["features"]["score"],
            "why": sig["why"],
        }
    except Exception as e:
        return {
            "symbol": symbol.upper(),
            "error": str(e),
        }

# ------------ Routes ------------

@app.get("/")
def root():
    return "OK - " + APP_VERSION, 200

@app.get("/status")
def status():
    try:
        u = discover_universe()
        return jsonify({
            "ok": True,
            "ts": int(time.time()*1000),
            "backend": APP_VERSION,
            "symbols": u["symbols"],
            "universe_size": len(u["map"]),
            "cfg": {
                "discover_top_n": CFG.get("discover_top_n"),
                "book_levels": CFG.get("book_levels"),
                "min_whale_usd": CFG.get("min_whale_usd"),
                "snapshot_ttl_sec": CFG.get("snapshot_ttl_sec"),
            }
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/symbols")
def symbols():
    try:
        u = discover_universe()
        return jsonify({"ok": True, "symbols": u["symbols"]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/signal")
def signal():
    symbol = (request.args.get("symbol") or "").upper()
    if not symbol:
        return jsonify({"ok": False, "error": "Missing ?symbol=XYZ"}), 400
    try:
        sig = compute_signal_for_symbol(symbol)
        return jsonify({"ok": True, "data": sig})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/scan")
def scan():
    try:
        u = discover_universe()
        rows = [quick_row_for_symbol(s) for s in u["symbols"]]
        # Filter rows that have price
        rows_ok = [r for r in rows if "price" in r]
        rows_ok.sort(key=lambda r: r.get("score", 0.0), reverse=True)
        return jsonify({"ok": True, "rows": rows_ok})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/chat")
def chat():
    try:
        payload = request.get_json(silent=True) or {}
        prompt = (payload.get("prompt") or "").strip().lower()
        if not prompt:
            return jsonify({"ok": True, "reply": "Ask me about a symbol (e.g., 'why XRP?', 'best entry for SOL', 'top scans')."})
        # Simple intent routing
        if "top" in prompt or "scan" in prompt or "gainer" in prompt:
            u = discover_universe()
            rows = [quick_row_for_symbol(s) for s in u["symbols"]]
            rows_ok = [r for r in rows if "price" in r]
            rows_ok.sort(key=lambda r: r.get("score", 0.0), reverse=True)
            top = rows_ok[:5]
            lines = [f"{i+1}. {r['symbol']} — {r['bias']} at {r['price']:.6f} ({r['why']})" for i, r in enumerate(top)]
            return jsonify({"ok": True, "reply": "Top radar:\n" + "\n".join(lines)})
        # extract symbol
        tokens = prompt.replace("?", " ").replace(",", " ").upper().split()
        sym = None
        for tok in tokens:
            if tok.isalpha() and 2 <= len(tok) <= 5:
                sym = tok
                break
        if sym:
            sig = compute_signal_for_symbol(sym)
            if "entry" in prompt:
                e = sig.get("entry")
                if e:
                    reply = f"{sym}: {sig['bias']}.\nEntry zone {e['zone'][0]}–{e['zone'][1]}, stop {e['stop']}, take-profit {e['tp'][0]} / {e['tp'][1]}.\nWhy: {sig['why']}"
                else:
                    reply = f"{sym}: {sig['bias']}. No entry suggestion yet (low volatility). Why: {sig['why']}"
                return jsonify({"ok": True, "reply": reply})
            # default "why"
            reply = f"{sym}: {sig['bias']} at {sig['price']:.6f}. {sig['why']}"
            return jsonify({"ok": True, "reply": reply})
        return jsonify({"ok": True, "reply": "I didn't catch the symbol. Try 'why XRP' or 'best entry SOL'."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
