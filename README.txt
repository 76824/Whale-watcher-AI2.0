Chenda Backend (Kraken-only)
============================
Deploy to Render:
1) Create a new Web Service from this folder.
2) Build command: pip install -r requirements.txt
3) Start command: gunicorn -w 1 -b 0.0.0.0:$PORT main:app
Optional env vars:
  CONFIG_PATH=./config.json
  SNAPSHOT_TTL_SEC=20
  CHENDA_SYMBOLS=XRP,SOL,LINK

Local test:
  python3 main.py
  curl http://localhost:5000/signal
  curl -X POST http://localhost:5000/chat -H "Content-Type: application/json" -d '{"message":"bias?"}'
