# Patched main.py (config fallback)
This `main.py` adds a robust `_load_cfg()` so the app never crashes if `config.json` is missing.

Order of config loading:
1) Env var `CHENDA_CFG_JSON` (JSON string)
2) File `config.json` (path override via `CFG_PATH` env var)
3) Built-in defaults (Kraken-only)

## How to use
- Replace your repo's `main.py` with this one, commit & push.
- EITHER add `config.json` to your repo, OR set the env var in Render:
  Key: `CHENDA_CFG_JSON`, Value: (paste JSON)

Optional: rename `config.sample.json` to `config.json` and commit it.