# Backend (Render)

**Start command**: `gunicorn -w 1 -b 0.0.0.0:$PORT main:app`

Env:
- `ALLOW_ORIGINS` (optional): comma-separated list; *.web.app and localhost are allowed by default.

Endpoints: `/signal`, `/healthz`, `/`.
