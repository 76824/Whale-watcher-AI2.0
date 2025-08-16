from __future__ import annotations
import json, os, re, time, math, random
from flask import Flask, request, jsonify, send_from_directory, make_response
from flask_cors import CORS

app = Flask(__name__, static_folder="static", static_url_path="/static")

ALLOWED_RE = re.compile(r"^https://([a-z0-9-]+\.)?web\.app$", re.IGNORECASE)
LOCAL_RE = re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$", re.IGNORECASE)
allow_from_env = {o.strip() for o in os.getenv("ALLOW_ORIGINS", "").split(",") if o.strip()}
CORS(app, resources={r"/*": {"origins": list(allow_from_env) or ["*"]}})

def _origin_ok(origin):
    if not origin: return False
    if origin in allow_from_env: return True
    if ALLOWED_RE.match(origin) or LOCAL_RE.match(origin): return True
    return False

@app.after_request
def _cors(resp):
    origin = request.headers.get("Origin")
    if _origin_ok(origin):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Methods"] = "GET,OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp

CFG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
def _load_cfg():
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"symbols": ["XRP/USD","SOL/USD","LINK/USD"], "price_venue": "kraken"}
CFG = _load_cfg()

def _ts(): return int(time.time() * 1000)
def _lvl(price, qty):
    usd = round(price*qty, 3)
    return {"price": round(price,5), "qty": round(qty,3), "usd": usd}

def _row(sym):
    base = abs(hash(sym)) % 1000 / 10.0 + 1.0
    t = time.time()
    wobble = 1.0 + 0.02 * math.sin(t/13.0) + 0.015 * math.cos(t/7.0)
    price = round(base * wobble, 5)
    random.seed(int(t//5) ^ hash(sym))
    asks = [_lvl(price + random.random()*0.02, random.uniform(5e4, 1.3e5)) for _ in range(8)]
    bids = [_lvl(price - random.random()*0.02, random.uniform(5e4, 1.3e5)) for _ in range(9)]
    return {"book_venues":["kraken"], "price":price, "price_venue":"kraken", "symbol": sym.split("/")[0], "ts":_ts(), "whales":{"asks":asks,"bids":bids}}

@app.route("/signal", methods=["GET","OPTIONS"])
def signal():
    if request.method == "OPTIONS":
        return ("", 204)
    syms = CFG.get("symbols", ["XRP/USD","SOL/USD","LINK/USD"])
    data = [_row(s) for s in syms]
    return jsonify({"data": data, "ts": _ts(), "status": "ok", "source": "mock-kraken-only"})

@app.route("/healthz")
def healthz(): return jsonify({"ok": True, "ts": _ts()})

@app.route("/")
def root():
    return make_response("""<!doctype html><title>Chenda backend</title>
<link rel='icon' href='/static/favicon.ico'>
<body style='font-family: system-ui; padding: 24px'>
  <h1>Chenda Backend — Kraken-only</h1>
  <p>Try <a href='/signal'>/signal</a> or <code>/healthz</code>.</p>
</body>""", 200)

@app.route("/favicon.ico")
def favicon():
    return send_from_directory(app.static_folder, "favicon.ico")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), debug=True)
