
import os, time, math, json
from typing import Dict, List, Tuple
import requests
from flask import Flask, jsonify, request, make_response
from flask_cors import CORS

# --- Config ---
DEFAULT_UNIVERSE = os.environ.get("CHENDA_UNIVERSE", "XRP,SOL,LINK,ADA,DOGE,ETH,BTC,MATIC,AVAX,DOT,ATOM,OP").split(",")
VENUE = "kraken"

# Kraken pair mapping (base USD)
KRAKEN_PAIRS = {
    "BTC": "XXBTZUSD",
    "ETH": "XETHZUSD",
    "XRP": "XXRPZUSD",
    "SOL": "SOLUSD",
    "LINK": "LINKUSD",
    "ADA": "ADAUSD",
    "DOGE": "DOGEUSD",
    "MATIC": "MATICUSD",
    "AVAX": "AVAXUSD",
    "DOT": "DOTUSD",
    "ATOM": "ATOMUSD",
    "OP": "OPUSD",
}

PAIR_TO_SYMBOL = {v:k for k,v in KRAKEN_PAIRS.items()}

def ts_ms() -> int:
    return int(time.time() * 1000)

def fetch_tickers(symbols: List[str]) -> Dict[str, float]:
    pairs = [KRAKEN_PAIRS[s] for s in symbols if s in KRAKEN_PAIRS]
    if not pairs:
        return {}
    url = "https://api.kraken.com/0/public/Ticker"
    r = requests.get(url, params={"pair": ",".join(pairs)}, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(",".join(data["error"]))
    result = {}
    for pair, payload in data["result"].items():
        last = float(payload["c"][0])
        sym = PAIR_TO_SYMBOL.get(pair, pair)
        result[sym] = last
    return result

def fetch_orderbook(symbol: str, depth: int = 20) -> Tuple[List[Tuple[float,float]], List[Tuple[float,float]]]:
    pair = KRAKEN_PAIRS.get(symbol)
    if not pair:
        return [], []
    url = "https://api.kraken.com/0/public/Depth"
    r = requests.get(url, params={"pair": pair, "count": depth}, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(",".join(data["error"]))
    book = data["result"][pair]
    bids = [(float(p), float(q)) for p,q,_ in book["bids"]]
    asks = [(float(p), float(q)) for p,q,_ in book["asks"]]
    return bids, asks

def to_levels(side: List[Tuple[float,float]], take=5) -> List[Dict]:
    out = []
    for p,q in side[:take]:
        out.append({"price": round(p,8), "qty": round(q,6), "usd": round(p*q,2)})
    return out

def whale_pressure(bids, asks, take=5):
    buy = sum(p*q for p,q in bids[:take])
    sell = sum(p*q for p,q in asks[:take])
    total = buy + sell or 1.0
    imbalance = (buy - sell) / total
    return buy, sell, imbalance

def bias_from(imbalance: float) -> Tuple[str, str]:
    # simple rule-of-thumb explanation
    if imbalance > 0.15:
        return "BUY", "Bid liquidity outweighs asks (>15%)."
    if imbalance < -0.15:
        return "SELL", "Ask liquidity outweighs bids (>15%)."
    return "HOLD", "Order-book is balanced (±15%)."

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

@app.after_request
def add_no_cache(resp):
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp

@app.get("/status")
def status():
    return jsonify({
        "ok": True,
        "service": "chenda",
        "venue": VENUE,
        "universe": DEFAULT_UNIVERSE,
        "ts": ts_ms(),
    })

@app.get("/signal")
def signal():
    # optional ?symbols=CSV override
    syms = request.args.get("symbols")
    symbols = [s.strip().upper() for s in syms.split(",")] if syms else DEFAULT_UNIVERSE
    # limit to ones we can map
    symbols = [s for s in symbols if s in KRAKEN_PAIRS]

    prices = fetch_tickers(symbols)
    rows = []
    scan = []
    for s in symbols:
        try:
            bids, asks = fetch_orderbook(s, depth=20)
        except Exception as e:
            bids, asks = [], []
        price = prices.get(s)
        buy_usd, sell_usd, imbal = whale_pressure(bids, asks, take=5) if bids and asks else (0,0,0)
        bias, why = bias_from(imbal)
        rows.append({
            "symbol": s,
            "price": price,
            "price_venue": VENUE,
            "ts": ts_ms(),
            "whales": {
                "bids": to_levels(bids, take=5),
                "asks": to_levels(asks, take=5),
            },
            "buy_usd": round(buy_usd,2),
            "sell_usd": round(sell_usd,2),
            "imbalance": round(imbal,4),
            "bias": bias,
            "why": why,
        })
        scan.append({
            "symbol": s,
            "price": price,
            "whale_usd": round(buy_usd+sell_usd,2),
            "bias": bias,
            "imbalance": round(imbal,4),
        })

    scan = sorted([r for r in scan if r["price"]], key=lambda r: abs(r["imbalance"])* (r["whale_usd"] or 1), reverse=True)[:12]

    return jsonify({
        "ok": True,
        "venue": VENUE,
        "data": rows,
        "scan_top": scan,
        "ts": ts_ms(),
    })

@app.post("/chat")
def chat():
    payload = request.get_json(silent=True) or {}
    q = (payload.get("q") or "").lower()
    # Very lightweight rule-based explanations using the latest /signal snapshot
    try:
        snap = json.loads(signal().get_data(as_text=True))
        by_symbol = {r["symbol"]: r for r in snap.get("data",[])}
    except Exception:
        by_symbol = {}

    def explain(sym):
        r = by_symbol.get(sym.upper())
        if not r:
            return f"I don't have fresh data for {sym} yet. Try symbols like {', '.join(list(by_symbol.keys())[:6])}."
        reason = r["why"]
        imbal = r["imbalance"]
        whale = r["buy_usd"] + r["sell_usd"]
        return (f"{sym}: {r['bias']} at ~{r['price']} USD. "
                f"Imbalance {imbal:+.2%}, whale liquidity ≈ ${whale:,.0f}. {reason} "
                "Use limit orders and risk controls; crypto is volatile.")

    # quick intents
    for sym in list(by_symbol.keys()):
        if sym.lower() in q:
            return jsonify({"ok": True, "reply": explain(sym)})
    if any(w in q for w in ["top", "scan", "gainer", "spike"]):
        tops = sorted(by_symbol.values(), key=lambda r: abs(r["imbalance"])*(r["buy_usd"]+r["sell_usd"] or 1), reverse=True)[:5]
        txt = "; ".join([f"{r['symbol']} {r['bias']} ({r['imbalance']:+.1%})" for r in tops])
        return jsonify({"ok": True, "reply": f"Stronger order-book signals: {txt}."})

    # fallback generic
    if by_symbol:
        universe = ", ".join(sorted(by_symbol.keys()))
        return jsonify({"ok": True, "reply": f"Ask about a symbol (e.g., 'Why {list(by_symbol.keys())[0]}?'). Tracked: {universe}."})
    return jsonify({"ok": True, "reply": "No snapshot yet. Try again in a moment."})

# health/root
@app.get("/")
def root():
    return make_response("OK", 200)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
