#!/usr/bin/env bash
# Foreground fallback when Docker is unavailable. Requires an existing local PostgreSQL.
set -euo pipefail
ROOT="$(dirname "$(dirname "$(realpath "$0")")")"
bash "$ROOT/scripts/setup-local.sh"
export OWNER_PASSWORD_FILE="$ROOT/secrets/owner_password"
export SESSION_KEY_FILE="$ROOT/secrets/session_key"
export ARK_API_KEY_FILE="$ROOT/secrets/ark_api_key"
export RUNTIME_CONFIG="$ROOT/config/runtime.json"
export MEDIA_ROOT="$ROOT/work/media"
export PYTHONPATH="$ROOT/backend"
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  python3 -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/pip" install --index-url https://mirrors.ivolces.com/pypi/simple/ -r "$ROOT/backend/requirements.txt"
fi
export ROOT
if [ -z "${DATABASE_URL:-}" ]; then
  export DATABASE_URL="$("$ROOT/.venv/bin/python" -c 'import os,pathlib,urllib.parse; p=pathlib.Path(os.environ["ROOT"])/"secrets/postgres_password"; print("postgresql+psycopg://animation_local:"+urllib.parse.quote(p.read_text().strip(),safe="")+"@127.0.0.1:5432/animation_local")')"
fi
npm --prefix "$ROOT/frontend" ci
"$ROOT/.venv/bin/python" -m uvicorn app.api:app --host 127.0.0.1 --port 8000 &
API_PID=$!
"$ROOT/.venv/bin/python" -m app.worker &
WORKER_PID=$!
cleanup() { kill "$API_PID" "$WORKER_PID" "${WEB_PID:-}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM
npm --prefix "$ROOT/frontend" run dev &
WEB_PID=$!
wait -n "$API_PID" "$WORKER_PID" "$WEB_PID"
