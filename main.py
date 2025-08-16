# Chenda backend (Kraken) — robust asset-pair mapping with USD/USDT fallback
# Flask app serving /signal and simple /status
#
# Environment:
#   CHENDA_UNIVERSE: comma-separated base symbols (e.g. "BTC,ETH,SOL,XRP,LINK,ADA,DOGE,AVAX,ATOM,TRX")
#   CHENDA_QUOTE_PREFS: comma-separated quotes in priority order (default "USD,USDT")
#
# Author: ChatGPT

from flask import Flask, jsonify, request
from flask_cors import CORS
import requests, time, os, json

app = Flask(__name__)
CORS(app)

KRAKEN = "https://api.kraken.com/0/public"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Chenda/1.0"})

# Caches
ASSETS_CACHE = {"ts": 0, "data": {}}
PAIRS_CACHE  = {"ts": 0, "data": {}}
CACHE_TTL = 600  # seconds

# --- utility ---------------------------------------------------------------

def _get(url, params=None, timeout=10):
    r = SESSION.get(url, params=params or {}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        # don't raise here; caller decides
        return {"error": data["error"], "result": {}}
    return {"error": [], "result": data.get("result", {})}

def _refresh_assets():
    now = time.time()
    if now - ASSETS_CACHE["ts"] < CACHE_TTL and ASSETS_CACHE["data"]:
        return ASSETS_CACHE["data"]
    res = _get(f"{KRAKEN}/Assets")
    if res["error"]:
        # keep old cache if any
        return ASSETS_CACHE["data"]
    ASSETS_CACHE["ts"] = now
    ASSETS_CACHE["data"] = res["result"]
    return ASSETS_CACHE["data"]

def _refresh_pairs():
    now = time.time()
    if now - PAIRS_CACHE["ts"] < CACHE_TTL and PAIRS_CACHE["data"]:
        return PAIRS_CACHE["data"]
    res = _get(f"{KRAKEN}/AssetPairs")
    if res["error"]:
        return PAIRS_CACHE["data"]
    PAIRS_CACHE["ts"] = now
    PAIRS_CACHE["data"] = res["result"]
    return PAIRS_CACHE["data"]

def _synonyms_map():
    """Return mapping of symbol variants (upper) to Kraken asset code."""
    assets = _refresh_assets() or {}
    syn = {}
    for code, info in assets.items():
        alt = (info.get("altname") or code).upper()
        syn[code.upper()] = code
        syn[alt.upper()] = code
    # common community synonyms
    # BTC on Kraken is XBT code
    if "XBT" in assets:
        syn["BTC"] = "XBT"
    # DOGE historically XDG
    if "XDG" in assets and "DOGE" not in syn:
        syn["DOGE"] = "XDG"
    # ETH stays ETH; add WBTC -> XBT where needed (skip if not present)
    if "XBT" in assets:
        syn.setdefault("WBTC", "XBT")
    return syn

def _pair_index():
    """Return index: base_code -> {quote_alt: kraken_pair_altname} for quick lookup."""
    pairs = _refresh_pairs() or {}
    assets = _refresh_assets() or {}
    # reverse map code->alt for quick compare
    code2alt = {code: (info.get("altname") or code).upper() for code, info in assets.items()}
    idx = {}
    for pkey, pobj in pairs.items():
        base = pobj.get("base")   # e.g., 'XXBT'
        quote = pobj.get("quote") # e.g., 'ZUSD'
        altname = pobj.get("altname") or pkey   # e.g., 'XBTUSD'
        if not base or not quote:
            continue
        base_alt  = code2alt.get(base, base).upper()
        quote_alt = code2alt.get(quote, quote).upper()
        d = idx.setdefault(base_alt, {})
        d[quote_alt] = altname
        # also index by "raw code" in case altname differs
        d = idx.setdefault(base.upper(), {})
        d[quote_alt] = altname
    return idx

def pick_pair_for_symbol(sym, quote_prefs=("USD","USDT")):
    """Return Kraken pair altname for given base symbol string or None if not tradable in preferred quotes."""
    if not sym:
        return None
    sym_up = sym.upper().strip()
    syn = _synonyms_map()
    base_code = syn.get(sym_up) or sym_up  # fallback to input
    # translate to ALT (human) name for pair index
    assets = _refresh_assets() or {}
    # find alt of base_code
    base_alt = None
    if base_code in assets:
        base_alt = (assets[base_code].get("altname") or base_code).upper()
    else:
        # maybe they passed an alt already
        base_alt = base_code
    pidx = _pair_index()
    choices = pidx.get(base_alt) or pidx.get(base_code) or {}
    for q in quote_prefs:
        q_up = q.upper()
        if q_up in choices:
            return choices[q_up]
    return None

def fetch_tickers(symbols, quote_prefs=("USD","USDT")):
    """Return dict {symbol: {pair, price}} for symbols tradable on Kraken; skip unknowns safely."""
    # find pairs
    found = {}
    pairs = []
    for sym in symbols:
        pair = pick_pair_for_symbol(sym, quote_prefs=quote_prefs)
        if pair:
            found[sym.upper()] = {"pair": pair}
            pairs.append(pair)
    if not pairs:
        return {}
    # To avoid EQuery errors if cache gets stale, only request pairs that exist in the cache index alt names
    res = _get(f"{KRAKEN}/Ticker", params={"pair": ",".join(pairs)})
    if res["error"]:
        # attempt to reindex once and retry with only valid pairs we can confirm
        _refresh_pairs()
        # Filter again using fresh index
        pidx = _pair_index()
        valid = []
        for info in pidx.values():
            valid.extend(info.values())
        valid_set = set(valid)
        safe_pairs = [p for p in pairs if p in valid_set]
        if not safe_pairs:
            return {}
        res = _get(f\"{KRAKEN}/Ticker\", params={\"pair\": \",\".join(safe_pairs)})
        if res[\"error\"]:
            # give up but don't crash
            return {}
    tick = res.get(\"result\", {})
    out = {}
    # Map back by pair name
    pair2sym = {v[\"pair\"]: k for k, v in found.items()}
    for pair, tinfo in tick.items():
        sym = pair2sym.get(pair)
        if not sym:
            # sometimes Kraken returns canonical key different from altname; try simple match
            for s, meta in found.items():
                if pair.upper().startswith(meta[\"pair\"].upper()[:3]):  # crude but avoids crash
                    sym = s; break
        price = None
        if isinstance(tinfo, dict):
            # last trade price 'c'[0], fall back to 'p'[0]
            if isinstance(tinfo.get(\"c\"), list) and tinfo[\"c\"]:
                price = float(tinfo[\"c\"][0])
            elif isinstance(tinfo.get(\"p\"), list) and tinfo[\"p\"]:
                price = float(tinfo[\"p\"][0])
        if sym and price is not None:
            out[sym] = {\"pair\": pair, \"price\": price}
    return out

def fetch_book(pair, depth=50):
    res = _get(f\"{KRAKEN}/Depth\", params={\"pair\": pair, \"count\": depth})
    if res[\"error\"]:
        return {\"bids\": [], \"asks\": []}
    book = list(res.get(\"result\", {}).values())[0] if res.get(\"result\") else {}
    return {
        \"bids\": book.get(\"bids\", []),
        \"asks\": book.get(\"asks\", []),
    }

def analyze_bias(book):
    # whale bias using notional top-25 levels
    def notional(levels):
        n = 0.0
        for px, qty, _ts in levels[:25]:
            try:
                n += float(px) * float(qty)
            except Exception:
                pass
        return n
    bid_n = notional(book[\"bids\"])
    ask_n = notional(book[\"asks\"])
    total = bid_n + ask_n
    imbalance = (bid_n - ask_n) / total if total > 0 else 0.0
    if imbalance > 0.10:
        label = \"BUY\"
    elif imbalance < -0.10:
        label = \"SELL\"
    else:
        label = \"HOLD\"
    why = f\"bids ${bid_n:,.0f} vs asks ${ask_n:,.0f} (imbalance {imbalance:+.1%})\"
    return label, why, bid_n, ask_n, imbalance

def universe_from_env():
    default = [\"BTC\",\"ETH\",\"SOL\",\"XRP\",\"LINK\",\"ADA\",\"DOGE\",\"AVAX\",\"ATOM\",\"TRX\"]
    env = os.getenv(\"CHENDA_UNIVERSE\")
    if env:
        try:
            syms = [s.strip().upper() for s in env.split(\",\") if s.strip()]
            return syms or default
        except Exception:
            return default
    # allow config.json fallback if present
    try:
        with open(\"config.json\", \"r\") as f:
            cfg = json.load(f)
            arr = cfg.get(\"universe\", [])
            if isinstance(arr, list) and arr:
                return [str(x).upper() for x in arr]
    except Exception:
        pass
    return default

def quote_prefs_from_env():
    env = os.getenv(\"CHENDA_QUOTE_PREFS\")
    prefs = [q.strip().upper() for q in env.split(\",\")] if env else [\"USD\",\"USDT\"]
    return tuple([p for p in prefs if p])

# --- routes ---------------------------------------------------------------

@app.get(\"/\")
def root():
    return jsonify({\"ok\": True, \"service\": \"chenda-kraken\", \"ts\": int(time.time())})

@app.get(\"/status\")
def status():
    # show assets cache info
    assets = _refresh_assets() or {}
    pairs = _refresh_pairs() or {}
    return jsonify({
        \"ok\": True,
        \"assets_cached\": len(assets),
        \"pairs_cached\": len(pairs),
        \"quote_prefs\": list(quote_prefs_from_env()),
        \"universe_default\": universe_from_env(),
    })

@app.get(\"/signal\")
def signal():
    # Inputs
    symbols_param = request.args.get(\"symbols\", \"\").strip()
    symbols = [s.strip().upper() for s in symbols_param.split(\",\") if s.strip()] if symbols_param else universe_from_env()
    quote_prefs = quote_prefs_from_env()

    # Resolve valid tickers
    prices = fetch_tickers(symbols, quote_prefs=quote_prefs)
    if not prices:
        # Return gracefully rather than 500
        return jsonify({
            \"ok\": False,
            \"error\": \"No valid tradable pairs for requested symbols in preferred quotes.\",
            \"symbols\": symbols,
            \"quote_prefs\": list(quote_prefs),
        }), 200

    # Build records
    records = []
    for sym in symbols:
        meta = prices.get(sym)
        if not meta:
            continue  # skipped symbol
        pair = meta[\"pair\"]
        price = meta[\"price\"]
        book = fetch_book(pair, depth=50)
        bias, why, bid_n, ask_n, imb = analyze_bias(book)
        records.append({
            \"symbol\": sym,
            \"pair\": pair,
            \"price\": price,
            \"bias\": bias,
            \"why\": why,
            \"whale_usd\": round(bid_n + ask_n, 2),
            \"imbalance\": imb,
        })

    # Sort by whale_usd * |imbalance|
    records.sort(key=lambda r: (r[\"whale_usd\"] * abs(r[\"imbalance\"])), reverse=True)

    return jsonify({
        \"ok\": True,
        \"quote_prefs\": list(quote_prefs),
        \"universe\": [r[\"symbol\"] for r in records],
        \"data\": records,
        \"ts\": int(time.time()),
    })

if __name__ == \"__main__\":
    app.run(host=\"0.0.0.0\", port=int(os.getenv(\"PORT\", 8080)))
