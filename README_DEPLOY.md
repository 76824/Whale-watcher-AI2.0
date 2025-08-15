# Whale Watcher AI · Chenda (Kraken-only)

This repo includes a **Kraken-only** backend (`main.py`, Flask) and a simple frontend (`index.html`) that reads `/signal`. Binance is not shown on the page.

## Files
- `main.py` — Flask backend exposing:
  - `GET /signal?min_usd=200000` — list of objects: `{symbol, price, price_venue, whales:{bids,asks}, ...}`
  - `GET /books?symbol=XRP&min_usd=200000` — raw merged book + whale levels
  - `GET /` — info & symbols
- `index.html` — Kraken-only UI (no Binance column)
- `live-signals.js` — fetches `/signal`, renders symbols, top whale levels, and JSON snapshot
- `style.css` — optional styles (you can replace with your theme)
- `firebase-config.js` — stub (not required)
- `requirements.txt`, `render.yaml` — deploy on Render via GitHub

## Deploy on Render (fresh project)

1. Create a **new GitHub repo** with all these files.
2. On **Render.com → New → Blueprint** and select your repo (uses `render.yaml`).
3. Wait for build & deploy to finish.
4. Test your backend:
   - `https://<your-render-app>.onrender.com/signal`
   - `https://<your-render-app>.onrender.com/books?symbol=XRP`
5. Open `index.html` (host anywhere, e.g., Firebase Hosting, GitHub Pages, Render static site).
   - In `index.html`, set:
     ```html
     <script>
       window.CHENDA_SIGNAL_URL = "https://<your-render-app>.onrender.com";
     </script>
     ```

## Optional: Firebase Hosting for the page
Deploy `index.html`, `live-signals.js`, `style.css`, `firebase-config.js` with:
```bash
firebase deploy --only hosting
```
(Or serve the static files from any static host.)

## Local dev (optional)
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
# Visit http://localhost:10000/signal
# For the page, set window.CHENDA_SIGNAL_URL = "http://localhost:10000"
```

## Notes
- `/signal` aggregates prices from Kraken/Coinbase/KuCoin/Bitstamp/Coingecko and order books from Kraken & Gate.io (no API keys needed).
- The UI hides Binance entirely.