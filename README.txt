
# Chenda Backend v3 (Kraken only)

Endpoints:
- `GET /status` health + universe
- `GET /symbols` discovered symbols with 24h change
- `GET /signal` live snapshot with whales + reasons + entries
- `GET /scan` ranked candidates (momentum + whale pressure)
- `POST /chat` { "message": "why hold xrp" } -> text reply

## Render deploy
Build command: `pip install -r requirements.txt`
Start command: `gunicorn -w 1 -b 0.0.0.0:$PORT main:app`

Env vars:
- `CORS_ALLOW_ORIGINS=https://<your-firebase>.web.app,https://<your-firebase>.firebaseapp.com`
- `CHENDA_SYMBOLS` (optional, comma separated)
- `MIN_WHALE_USD` (optional)
- `SNAPSHOT_TTL_SEC` (optional)

## Notes
- Uses Kraken public API (no keys).
- Auto-discovers USD/USDT pairs and ranks by 24h absolute change.
- Whale pressure = net USD of big bids vs asks (>= min_whale_usd).
- Momentum = 5‑minute % change from 1‑minute OHLC.
- Entry plan generated from ATR‑like volatility.
