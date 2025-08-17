
import os
import time
import json
from typing import Dict, List, Tuple
import requests
from flask import Flask, jsonify
from flask_cors import CORS

KRAKEN = "https://api.kraken.com/0/public"

app = Flask(__name__)
CORS(app)

# ----- config (env or file) -----
DEFAULT_UNIVERSE = "BTC,ETH,SOL,XRP,LINK,ADA,DOGE,AVAX,ATOM,TRX"
DEFAULT_QUOTES = "USD,USDT,EUR,USDC"

def load_config() -> Dict:
    # optional config.json (sits next to main.py)
    cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    file_cfg = {}
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            file_cfg = json.load(f)
    except Exception:
        file_cfg = {}
    env_universe = os.getenv("CHENDA_UNIVERSE", file_cfg.get("universe", DEFAULT_UNIVERSE))
    env_quotes = os.getenv("CHENDA_QUOTE_PREFS", file_cfg.get("quote_prefs", DEFAULT_QUOTES))
    universe = [s.strip().upper() for s in env_universe.split(",") if s.strip()]
    quotes = [q.strip().upper() for q in env_quotes.split(",") if q.strip()]
    max_depth = int(os.getenv("CHENDA_DEPTH_COUNT", str(file_cfg.get("depth_count", 20))))
    whale_usd_floor = float(os.getenv("CHENDA_WHALE_USD_FLOOR", str(file_cfg.get("whale_usd_floor", 100_000))))
    return {
        "universe": universe,
        "quotes": quotes,
        "depth_count": max_depth,
        "whale_usd_floor": whale_usd_floor,
    }

CFG = load_config()

# ----- simple cache to avoid rate limits -----
_cache = {
    "assets": None,           # (data, ts)
    "pairs_by_alt": None,     # (data, ts)
    "pairs_by_key": None,     # (data, ts)
    "cache_ttl": 300.0,       # seconds
}

def _get(url: str, params: Dict = None, timeout: int = 10) -> Dict:
    r = requests.get(url, params=params or {}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(",".join(data["error"]))
    return data["result"]

def fetch_assets() -> Dict[str, Dict]:
    # returns map of asset_key -> info; also altname map
    now = time.time()
    if _cache["assets"] and now - _cache["assets"][1] < _cache["cache_ttl"]:
        return _cache["assets"][0]
    res = _get(f"{KRAKEN}/Assets")
    # Build altname index too
    alt_to_key = {}
    for k, v in res.items():
        alt = v.get("altname", "").upper()
        if alt:
            alt_to_key[alt] = k
    res["_alt_to_key"] = alt_to_key
    _cache["assets"] = (res, now)
    return res

def fetch_asset_pairs() -> Tuple[Dict[str, Dict], Dict[str, Dict]]:
    # returns (by_altname, by_key)
    now = time.time()
    if _cache["pairs_by_alt"] and now - _cache["pairs_by_alt"][1] < _cache["cache_ttl"]:
        return _cache["pairs_by_alt"][0], _cache["pairs_by_key"][0]
    res = _get(f"{KRAKEN}/AssetPairs")
    by_alt = {}
    by_key = {}
    for key, v in res.items():
        alt = v.get("altname", "").upper()
        if alt:
            by_alt[alt] = {"key": key, **v}
        by_key[key] = {"altname": alt, **v}
    _cache["pairs_by_alt"] = (by_alt, now)
    _cache["pairs_by_key"] = (by_key, now)
    return by_alt, by_key

# Common symbol aliases -> Kraken altnames
ALIASES = {
    "BTC": "XBT",
    "DOGE": "XDG",
    "BCH": "BCH",
    "XRP": "XRP",
    "ETH": "ETH",
    "MIOTA": "IOTA",
    "IOTA": "IOTA",
    "USDT": "USDT",
    "USDC": "USDC",
    "SOL": "SOL",
    "ADA": "ADA",
    "LINK": "LINK",
    "AVAX": "AVAX",
    "ATOM": "ATOM",
    "TRX": "TRX",
    "MATIC": "MATIC",
}

def resolve_alt(symbol: str) -> str:
    assets = fetch_assets()
    alt_to_key = assets.get("_alt_to_key", {})
    s = symbol.upper()
    # Known alias mapping first
    if s in ALIASES:
        s_alt = ALIASES[s]
        if s_alt in alt_to_key:
            return s_alt
    # Try direct altname hit
    if s in alt_to_key:
        return s
    # Some assets have prefixes like X, Z in keys; altname in payload is cleaner
    # If not found, just return s and we'll likely skip later
    return s

def pick_pair_for_symbol(symbol: str, quotes: List[str]) -> Tuple[str, str]:
    """
    Returns (pair_key, pair_altname) if found, else (None, None)
    """
    base_alt = resolve_alt(symbol)
    pairs_by_alt, _pairs_by_key = fetch_asset_pairs()
    for q in quotes:
        q_alt = resolve_alt(q)
        altname = f"{base_alt}{q_alt}"
        if altname in pairs_by_alt:
            return pairs_by_alt[altname]["key"], altname
    return None, None

def fetch_tickers(pair_keys: List[str]) -> Dict[str, Dict]:
    if not pair_keys:
        return {}
    res = _get(f"{KRAKEN}/Ticker", params={"pair": ",".join(pair_keys)})
    return res

def fetch_depth(pair_key: str, count: int = 20) -> Dict[str, List[List[float]]]:
    res = _get(f"{KRAKEN}/Depth", params={"pair": pair_key, "count": count})
    # Depth returns {pair_key: {"bids":[[price, volume, ts],...], "asks":[...] } }
    return res.get(pair_key, {"bids": [], "asks": []})

def summarize_whales(depth: Dict[str, List[List[float]]], usd_floor: float) -> Tuple[List[Dict], List[Dict], float, float]:
    bids = []
    asks = []
    bid_usd_total = 0.0
    ask_usd_total = 0.0
    for row in depth.get("bids", []):
        price = float(row[0]); qty = float(row[1])
        usd = price * qty
        if usd >= usd_floor:
            bids.append({"price": price, "qty": qty, "usd": usd})
        bid_usd_total += usd
    for row in depth.get("asks", []):
        price = float(row[0]); qty = float(row[1])
        usd = price * qty
        if usd >= usd_floor:
            asks.append({"price": price, "qty": qty, "usd": usd})
        ask_usd_total += usd
    return bids, asks, bid_usd_total, ask_usd_total

def bias_from_imbalance(bid_usd: float, ask_usd: float) -> Tuple[str, str]:
    tiny = 1e-9
    denom = max(bid_usd + ask_usd, tiny)
    imbalance = (bid_usd - ask_usd) / denom
    if imbalance > 0.2:
        return "BUY", f"Bid pressure {imbalance:.0%} higher (bids ${bid_usd:,.0f} vs asks ${ask_usd:,.0f})."
    if imbalance < -0.2:
        return "SELL", f"Ask pressure {abs(imbalance):.0%} higher (asks ${ask_usd:,.0f} vs bids ${bid_usd:,.0f})."
    return "HOLD", f"Balanced book (bids ${bid_usd:,.0f} vs asks ${ask_usd:,.0f})."

@app.get("/status")
def status():
    pairs = []
    skipped = []
    for sym in CFG["universe"]:
        k, alt = pick_pair_for_symbol(sym, CFG["quotes"])
        if k:
            pairs.append({"symbol": sym, "pair_key": k, "altname": alt})
        else:
            skipped.append(sym)
    return jsonify({
        "ok": True,
        "venue": "kraken",
        "universe": CFG["universe"],
        "preferred_quotes": CFG["quotes"],
        "resolved_pairs": pairs,
        "skipped": skipped,
        "ts": int(time.time())
    })

@app.get("/signal")
def signal():
    # build pairs from universe
    pairs = []
    skipped = []
    for sym in CFG["universe"]:
        k, alt = pick_pair_for_symbol(sym, CFG["quotes"])
        if k:
            pairs.append((sym, k, alt))
        else:
            skipped.append(sym)

    pair_keys = [k for _, k, _ in pairs]
    tickers = fetch_tickers(pair_keys) if pair_keys else {}

    data_out = []
    for sym, key, alt in pairs:
        t = tickers.get(key, {})
        # price is last close in "c"
        price = None
        try:
            price = float(t.get("c", [None])[0])
        except Exception:
            price = None
        depth = fetch_depth(key, count=CFG["depth_count"])
        bids, asks, bid_usd, ask_usd = summarize_whales(depth, CFG["whale_usd_floor"])
        bias, why = bias_from_imbalance(bid_usd, ask_usd)
        data_out.append({
            "symbol": sym,
            "pair_key": key,
            "pair_alt": alt,
            "price": price,
            "price_venue": "kraken",
            "ts": int(time.time()),
            "whales": {
                "bids": bids,
                "asks": asks,
                "bid_usd_total": bid_usd,
                "ask_usd_total": ask_usd
            },
            "bias": bias,
            "why": why
        })

    return jsonify({
        "ok": True,
        "skipped": skipped,
        "data": data_out
    })

# Simple root to prove liveness
@app.get("/")
def root():
    return jsonify({"ok": True, "service": "chenda-kraken", "ts": int(time.time())})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False)
