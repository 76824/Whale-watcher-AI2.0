Chenda Kraken Radar — quick start

Backend (Render or local):
  1) cd backend
  2) If local: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
  3) python main.py
  4) Environment overrides (optional):
       UNIVERSE="BTC,ETH,SOL,XRP,ADA,DOGE"
       QUOTE_PREFERENCES="USD,USDT,EUR"
       DEPTH_LEVELS=10
       WHALE_USD_FLOOR=150000

Render:
  - Create a Python Web Service, repo or upload these backend files.
  - Start command: gunicorn -w 1 -b 0.0.0.0:$PORT main:app

Frontend (Firebase Hosting):
  1) cd frontend
  2) Edit public/config.json if your backend runs on Render:
       {"backend_base_url": "https://<your-render>.onrender.com"}
  3) firebase deploy --only hosting

Common gotchas
  - If your browser console shows "Unexpected token '<' at live-signals.js:1",
    your hosting is serving index.html instead of the JS file. Ensure the file
    exists at /public/live-signals.js and do NOT add SPA rewrites.
