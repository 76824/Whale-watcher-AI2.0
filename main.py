# ===== Chenda Public Backend (Binance-free) =====
# Flask app that fetches prices + orderbooks from public sources (no API keys).
# Endpoints:
#   GET /signal?min_usd=200000
#   GET /books?symbol=XRP&min_usd=200000
#   GET /               -> hello + config
#
# Sources used:
#   Prices: Kraken, Coinbase, KuCoin, Bitstamp, CoinGecko (fallback)
#   Orderbooks for whale levels: Kraken, Gate.io
#
# Notes:
# - No API keys required
# - Designed for Render; reads config.json next to this file
# - Health check path: /signal

from __future__ import annotations
import os, json, time
from typing import Dict, Any, List, Tuple, Optional

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv

import os, json

CFG_PATH = os.environ.get("CFG_PATH", "config.json")

def _load_cfg():
    """
    Load configuration in this order:
    1) CHENDA_CFG_JSON env var (JSON string)
    2) config.json file (path from CFG_PATH env or default "config.json")
    3) Built-in safe defaults (Kraken-only)
    This ensures the app never crashes when config.json is missing.
    """
    # 1) ENV override
    raw = os.environ.get("CHENDA_CFG_JSON")
    if raw:
        try:
            return json.loads(raw)
        except Exception as e:
            print("WARN: CHENDA_CFG_JSON not valid JSON:", e)

    # 2) File
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"WARN: {CFG_PATH} not found; using defaults")
    except Exception as e:
        print(f"WARN: failed to read {CFG_PATH}: {e}; using defaults")

    # 3) Defaults (Kraken-only)
    return {
        "universes": {
            "kraken": [
                "KRAKEN:XRPUSD",
                "KRAKEN:BTCUSD",
                "KRAKEN:SOLUSD",
                "KRAKEN:LINKUSD"
            ]
        },
        "min_usd": 200000
    }



# ---------- Config ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE_DIR, "config.json")
load_dotenv(os.path.join(BASE_DIR, ".env"))  # optional

def _load_cfg() -> Dict[str, Any]:
    with open(CFG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

CFG = _load_cfg()
PORT = int(CFG.get("port", 10000))
DEPTH = int(CFG.get("depth", 200))
DEFAULT_MIN_USD = float(CFG.get("whale_qty", 200000))
SYMBOLS = [s.upper() for s in CFG.get("symbols", ["XRP","SOL","LINK"])]

# ----- helpers -----
def now_ts() -> int:
    return int(time.time())

def ok(data: Any, status:int=200):
    return jsonify({"ok": True, "ts": now_ts(), **({"data": data} if not isinstance(data, dict) else data)}), status

def err(msg: str, status:int=500):
    return jsonify({"ok": False, "ts": now_ts(), "error": msg}), status

def _coingecko_id(symbol: str) -> Optional[str]:
    # Minimal mapping (expand as needed)
    m = {
        "BTC":"bitcoin","ETH":"ethereum","XRP":"ripple","SOL":"solana","LINK":"chainlink",
        "ADA":"cardano","DOGE":"dogecoin","AVAX":"avalanche-2","MATIC":"matic-network",
        "DOT":"polkadot","NEO":"neo","LTC":"litecoin","ALGO":"algorand","PEPE":"pepe"
    }
    return m.get(symbol.upper())

# ---------- Price fetchers ----------
def price_kraken(symbol: str) -> Optional[Tuple[float,str]]:
    pair = f"{symbol.upper()}USD"
    url = f"https://api.kraken.com/0/public/Ticker?pair={pair}"
    r = requests.get(url, timeout=10)
    j = r.json()
    if j.get("error"):
        return None
    res = j.get("result", {})
    if not res:
        return None
    # result key may differ (e.g. XXRPZUSD); take first
    _, v = next(iter(res.items()))
    last = v.get("c", [None, None])[0]
    if last is None: return None
    return float(last), "kraken"

def price_coinbase(symbol: str) -> Optional[Tuple[float,str]]:
    pair = f"{symbol.upper()}-USD"
    url = f"https://api.exchange.coinbase.com/products/{pair}/ticker"
    r = requests.get(url, timeout=10)
    if r.status_code != 200:
        return None
    j = r.json()
    p = j.get("price")
    return (float(p), "coinbase") if p else None

def price_kucoin(symbol: str) -> Optional[Tuple[float,str]]:
    pair = f"{symbol.upper()}-USDT"
    url = f"https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={pair}"
    r = requests.get(url, timeout=10)
    j = r.json()
    if j.get("code") != "200000":
        return None
    data = j.get("data", {})
    p = data.get("price")
    return (float(p), "kucoin") if p else None

def price_bitstamp(symbol: str) -> Optional[Tuple[float,str]]:
    pair = f"{symbol.lower()}usd"
    url = f"https://www.bitstamp.net/api/v2/ticker/{pair}/"
    r = requests.get(url, timeout=10)
    if r.status_code != 200:
        return None
    j = r.json()
    p = j.get("last")
    return (float(p), "bitstamp") if p else None

def price_coingecko(symbol: str) -> Optional[Tuple[float,str]]:
    cid = _coingecko_id(symbol)
    if not cid: return None
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={cid}&vs_currencies=usd"
    r = requests.get(url, timeout=10)
    if r.status_code != 200: return None
    j = r.json()
    usd = j.get(cid, {}).get("usd")
    return (float(usd), "coingecko") if usd is not None else None

PRICE_SOURCES = [price_kraken, price_coinbase, price_kucoin, price_bitstamp, price_coingecko]

def get_price(symbol: str) -> Optional[Dict[str, Any]]:
    for fn in PRICE_SOURCES:
        try:
            got = fn(symbol)
            if got:
                p, venue = got
                return {"symbol": symbol.upper(), "price": p, "venue": venue, "ts": now_ts()}
        except Exception:
            continue
    return None

# ---------- Orderbooks & whale levels ----------
def _whale_levels_from_book(bids: List[List[float]], asks: List[List[float]], min_usd: float) -> Dict[str, Any]:
    out = {"bids": [], "asks": []}
    for p, q in bids:
        notional = float(p) * float(q)
        if notional >= min_usd:
            out["bids"].append({"price": float(p), "qty": float(q), "usd": notional})
    for p, q in asks:
        notional = float(p) * float(q)
        if notional >= min_usd:
            out["asks"].append({"price": float(p), "qty": float(q), "usd": notional})
    # sort by notional
    out["bids"].sort(key=lambda x: x["usd"], reverse=True)
    out["asks"].sort(key=lambda x: x["usd"], reverse=True)
    return out

def ob_kraken(symbol: str, depth: int=DEPTH) -> Optional[Dict[str, Any]]:
    pair = f"{symbol.upper()}USD"
    url = f"https://api.kraken.com/0/public/Depth?pair={pair}&count={depth}"
    r = requests.get(url, timeout=10)
    j = r.json()
    if j.get("error"):
        return None
    res = j.get("result", {})
    if not res: return None
    _, v = next(iter(res.items()))
    # Kraken returns [["price","qty","ts"], ...] as strings; cast to floats
    bids = [[float(p), float(q)] for p, q, *_ in v.get("bids", [])]
    asks = [[float(p), float(q)] for p, q, *_ in v.get("asks", [])]
    return {"venue": "kraken", "bids": bids, "asks": asks}

def ob_gateio(symbol: str, depth: int=DEPTH) -> Optional[Dict[str, Any]]:
    pair = f"{symbol.upper()}_USDT"
    url = f"https://api.gateio.ws/api/v4/spot/order_book?currency_pair={pair}&limit={depth}"  # asks descending per docs; we’ll normalize
    r = requests.get(url, timeout=10)
    if r.status_code != 200:
        return None
    j = r.json()
    # returns bids/asks as [["price","size"], ...] strings
    bids = [[float(p), float(q)] for p, q in j.get("bids", [])]
    asks = [[float(p), float(q)] for p, q in j.get("asks", [])]
    # ensure asks ascending by price
    asks.sort(key=lambda x: x[0])
    # ensure bids descending by price
    bids.sort(key=lambda x: -x[0])
    return {"venue": "gateio", "bids": bids, "asks": asks}

BOOK_SOURCES = [ob_kraken, ob_gateio]

def get_books(symbol: str, depth:int=DEPTH) -> Optional[Dict[str, Any]]:
    books = []
    for fn in BOOK_SOURCES:
        try:
            b = fn(symbol, depth=depth)
            if b and b.get("bids") and b.get("asks"):
                books.append(b)
        except Exception:
            continue
    if not books:
        return None
    # merge books (simple concat)
    merged_bids = []
    merged_asks = []
    for b in books:
        merged_bids.extend(b["bids"])
        merged_asks.extend(b["asks"])
    # keep only top N by price proximity
    merged_bids.sort(key=lambda x: -x[0])
    merged_asks.sort(key=lambda x: x[0])
    merged_bids = merged_bids[:depth]
    merged_asks = merged_asks[:depth]
    return {"venues": [b["venue"] for b in books], "bids": merged_bids, "asks": merged_asks}

# ---------- Flask app ----------
app = Flask(__name__)
CORS(app)

@app.route("/", methods=["GET"])
def root():
    return ok({"message": "Chenda Public Backend (Binance-free)", "symbols": SYMBOLS})

@app.route("/signal", methods=["GET"])
def signal():
    try:
        min_usd = float(request.args.get("min_usd", DEFAULT_MIN_USD))
        out = []
        for sym in SYMBOLS:
            px = get_price(sym)
            if not px:
                out.append({"symbol": sym, "error": "no_price"})
                continue
            book = get_books(sym, depth=DEPTH)
            whales = _whale_levels_from_book(book["bids"], book["asks"], min_usd) if book else {"bids": [], "asks": []}
            out.append({
                "symbol": sym,
                "price": px["price"],
                "price_venue": px["venue"],
                "book_venues": (book or {}).get("venues", []),
                "whales": whales,
                "ts": now_ts()
            })
        return ok(out)
    except Exception as e:
        return err(f"signal_error: {e}", 503)

@app.route("/books", methods=["GET"])
def books():
    try:
        sym = (request.args.get("symbol") or "XRP").upper()
        min_usd = float(request.args.get("min_usd", DEFAULT_MIN_USD))
        book = get_books(sym, depth=DEPTH)
        if not book:
            return err(f"no_orderbook_for_{sym}", 404)
        whales = _whale_levels_from_book(book["bids"], book["asks"], min_usd)
        data = {"symbol": sym, "venues": book["venues"], "bids": book["bids"], "asks": book["asks"], "whales": whales}
        return ok(data)
    except Exception as e:
        return err(f"books_error: {e}", 500)

if __name__ == "__main__":
    port = int(os.getenv("PORT", str(PORT)))
    app.run(host="0.0.0.0", port=port)
