import os
import json
import time
from datetime import datetime, timezone
from typing import Dict, List, Tuple

import requests
from flask import Flask, jsonify
from flask_cors import CORS

KRAKEN = "https://api.kraken.com/0/public"

app = Flask(__name__)
# Allow frontend from anywhere (for simple demo). Lock this down if you want.
CORS(app)


# ----------------------- Config -----------------------
def _as_list(val, upper=True):
    """
    Accepts None | str (comma-separated or JSON list) | list and returns a list[str].
    """
    if val is None:
        return None
    # Environment variables are strings. But we also guard for lists (when someone passed config obj here).
    if isinstance(val, list):
        return [str(x).upper() if upper else str(x) for x in val]
    sval = str(val).strip()
    if not sval:
        return []
    if sval[0] in "[{":
        try:
            arr = json.loads(sval)
            if isinstance(arr, list):
                return [str(x).upper() if upper else str(x) for x in arr]
        except Exception:
            pass
    # comma separated
    return [s.strip().upper() if upper else s.strip() for s in sval.split(",") if s.strip()]


def load_config():
    # defaults
    cfg = {
        "universe": ["BTC", "ETH", "SOL", "XRP", "ADA", "DOGE"],
        "quote_preferences": ["USD", "USDT", "EUR"],
        "depth_levels": 10,
        "whale_usd_floor": 150_000
    }
    # config.json (optional)
    if os.path.exists("config.json"):
        try:
            with open("config.json", "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
                if isinstance(file_cfg, dict):
                    cfg.update(file_cfg)
        except Exception:
            pass

    # Environment overrides (strings only; do NOT pass config dicts in os.getenv)
    env_universe = os.getenv("UNIVERSE")
    env_quotes = os.getenv("QUOTE_PREFERENCES")
    env_depth = os.getenv("DEPTH_LEVELS")
    env_floor = os.getenv("WHALE_USD_FLOOR")

    if env_universe is not None:
        parsed = _as_list(env_universe)
        if parsed:
            cfg["universe"] = parsed

    if env_quotes is not None:
        parsed = _as_list(env_quotes)
        if parsed:
            cfg["quote_preferences"] = parsed

    if env_depth:
        try:
            cfg["depth_levels"] = int(env_depth)
        except Exception:
            pass

    if env_floor:
        try:
            cfg["whale_usd_floor"] = float(env_floor)
        except Exception:
            pass

    # Normalize to upper
    cfg["universe"] = [s.upper() for s in cfg["universe"]]
    cfg["quote_preferences"] = [s.upper() for s in cfg["quote_preferences"]]
    return cfg


CFG = load_config()


# ----------------------- Kraken helpers -----------------------

def norm_asset(code: str) -> str:
    """Map odd Kraken asset codes to common tickers (XBT->BTC, XDG->DOGE, ZUSD->USD, etc.)."""
    c = code.upper()
    # Drop leading 'X'/'Z' that Kraken uses in some classic codes
    if len(c) >= 3 and (c[0] in ("X", "Z")) and c not in ("XBT", "XDG"):
        c = c[1:]
    # special cases
    if c == "XBT":
        return "BTC"
    if c == "XDG":
        return "DOGE"
    return c


def load_asset_pairs() -> Dict[Tuple[str, str], str]:
    """Return map of (BASE,QUOTE)->kraken_pair_name using /AssetPairs."""
    url = f"{KRAKEN}/AssetPairs"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(",".join(data["error"]))
    pairs = {}
    for name, meta in data["result"].items():
        base = norm_asset(meta.get("base", ""))
        quote = norm_asset(meta.get("quote", ""))
        pairs[(base, quote)] = name
    return pairs


# Cache for a bit to avoid hammering /AssetPairs
_PAIR_CACHE = {"ts": 0.0, "pairs": {}}


def get_pair_name(base: str, quotes: List[str]) -> Tuple[str, str]:
    """Pick first available quote from preferences and return (kraken_pair_name, quote)."""
    now = time.time()
    if now - _PAIR_CACHE["ts"] > 60 * 30 or not _PAIR_CACHE["pairs"]:
        _PAIR_CACHE["pairs"] = load_asset_pairs()
        _PAIR_CACHE["ts"] = now

    base = base.upper()
    for q in quotes:
        tup = (base, q.upper())
        name = _PAIR_CACHE["pairs"].get(tup)
        if name:
            return name, q.upper()
    # As a fallback try BTC ticker alias for XBT and DOGE alias
    alias = {"BTC": "XBT", "DOGE": "XDG"}
    rev_alias = {v: k for k, v in alias.items()}
    # Try both aliases on base
    for candidate in [base, alias.get(base, base), rev_alias.get(base, base)]:
        for q in quotes:
            tup = (candidate.upper(), q.upper())
            name = _PAIR_CACHE["pairs"].get(tup)
            if name:
                return name, q.upper()
    raise RuntimeError(f"No Kraken pair for {base} with quotes {quotes}")


def fetch_tickers(symbols: List[str]) -> Dict[str, dict]:
    """Fetch last price per symbol using preferred quotes."""
    pairs = []
    sym_to_pair = {}
    for s in symbols:
        try:
            pair_name, quote = get_pair_name(s, CFG["quote_preferences"])
            pairs.append(pair_name)
            sym_to_pair[s] = (pair_name, quote)
        except Exception:
            # skip symbols we can't map
            continue
    if not pairs:
        return {}

    # Kraken allows comma separated
    url = f"{KRAKEN}/Ticker"
    res = requests.get(url, params={"pair": ",".join(pairs)}, timeout=20)
    res.raise_for_status()
    data = res.json()
    if data.get("error"):
        raise RuntimeError(",".join(data["error"]))

    results = {}
    result_obj = data.get("result", {})
    # result keys may be canonical pair names, but also aliases; map back
    pair_to_price = {}
    for pair_name, payload in result_obj.items():
        # 'c' is last trade close array [price, lot volume]
        last = float(payload.get("c", [0])[0])
        pair_to_price[pair_name] = last

    for sym, (pair_name, quote) in sym_to_pair.items():
        price = pair_to_price.get(pair_name)
        if price is not None:
            results[sym] = {
                "symbol": sym,
                "price": price,
                "price_venue": "kraken",
                "quote": quote,
            }
    return results


def fetch_depth(pair_name: str, count: int = 10) -> dict:
    """Fetch order book depth for a single pair."""
    url = f"{KRAKEN}/Depth"
    res = requests.get(url, params={"pair": pair_name, "count": count}, timeout=20)
    res.raise_for_status()
    data = res.json()
    if data.get("error"):
        raise RuntimeError(",".join(data["error"]))
    return list(data["result"].values())[0]  # first (and only) book


def summarize_whales(book: dict) -> dict:
    """Sum top-of-book USD on both sides and compute imbalance ratio."""
    bids = book.get("bids", [])  # [price, vol, ts]
    asks = book.get("asks", [])
    def side_usd(levels):
        total_qty = 0.0
        total_usd = 0.0
        for p, v, _ in levels:
            p = float(p); v = float(v)
            total_qty += v
            total_usd += p * v
        return total_qty, total_usd

    bid_qty, bid_usd = side_usd(bids)
    ask_qty, ask_usd = side_usd(asks)
    total = bid_usd + ask_usd
    imb = (bid_usd - ask_usd) / total if total else 0.0
    return {
        "bids": {"qty": bid_qty, "usd": bid_usd},
        "asks": {"qty": ask_qty, "usd": ask_usd},
        "imbalance": imb
    }


def classify_signal(imbalance: float, whale_usd: float, floor: float) -> Tuple[str, str]:
    """
    Simple rule: if imbalance > 0.2 and whale_usd > floor => BUY
                  if imbalance < -0.2 and whale_usd > floor => SELL
                  otherwise HOLD
    """
    if whale_usd < floor:
        return "HOLD", f"Not enough whale pressure (USD {whale_usd:,.0f} < {floor:,.0f})."
    if imbalance > 0.2:
        return "BUY", f"Bid USD outweighs ask USD; imbalance {imbalance:.2f}."
    if imbalance < -0.2:
        return "SELL", f"Ask USD outweighs bid USD; imbalance {imbalance:.2f}."
    return "HOLD", f"Balanced order book; imbalance {imbalance:.2f}."


# ----------------------- Routes -----------------------

@app.get("/status")
def status():
    # Quick health + what universe we are tracking
    try:
        # cheap call to ensure we can reach Kraken
        _ = requests.get(f"{KRAKEN}/Time", timeout=10).json()
        ok = True
    except Exception:
        ok = False
    return jsonify({
        "ok": ok,
        "service": "chenda-kraken",
        "ts": int(time.time()),
        "universe": CFG["universe"],
        "quote_preferences": CFG["quote_preferences"],
        "depth_levels": CFG["depth_levels"]
    })


@app.get("/signal")
def signal():
    ts = int(time.time())
    out = {"ts": ts, "data": [], "scan": [], "errors": []}
    symbols = CFG["universe"]
    try:
        prices = fetch_tickers(symbols)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    for sym in symbols:
        if sym not in prices:
            out["errors"].append(f"{sym}: no pair")
            continue
        pair_name, quote = get_pair_name(sym, CFG["quote_preferences"])
        try:
            book = fetch_depth(pair_name, CFG["depth_levels"])
            whales = summarize_whales(book)
            whale_usd = max(whales["bids"]["usd"], whales["asks"]["usd"])
            imb = whales["imbalance"]
            bias, why = classify_signal(imb, whale_usd, CFG["whale_usd_floor"])
            out["data"].append({
                "symbol": sym,
                "price": prices[sym]["price"],
                "price_venue": "kraken",
                "quote": quote,
                "pair": pair_name,
                "whales": whales,
                "bias": bias,
                "why": why,
            })
        except Exception as e:
            out["errors"].append(f"{sym}: {e}")

    # scan: top by whale USD (bids or asks), include imbalance
    scan_rows = []
    for row in out["data"]:
        whales = row["whales"]
        whale_usd = max(whales["bids"]["usd"], whales["asks"]["usd"])
        scan_rows.append({
            "symbol": row["symbol"],
            "price": row["price"],
            "bias": row["bias"],
            "why": row["why"],
            "whale_usd": whale_usd,
            "imbalance": whales["imbalance"]
        })
    scan_rows.sort(key=lambda r: r["whale_usd"], reverse=True)
    out["scan"] = scan_rows[:15]
    return jsonify(out)


@app.get("/")
def root():
    return jsonify({"ok": True, "service": "chenda-kraken", "next": ["/status", "/signal"]})


if __name__ == "__main__":
    # For local dev
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=True)
