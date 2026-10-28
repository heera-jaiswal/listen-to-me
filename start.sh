#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Creating Python environment…"
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

PORT="${LTM_PORT:-8765}"
(sleep 2 && open "http://localhost:${PORT}" >/dev/null 2>&1 || true) &
exec .venv/bin/python server.py
