#!/usr/bin/env bash
set -euo pipefail
ROOT="$(dirname "$(dirname "$(realpath "$0")")")"
export ROOT
export PYTHONPATH="$ROOT/backend"
export OWNER_PASSWORD_FILE="$ROOT/secrets/owner_password"
export SESSION_KEY_FILE="$ROOT/secrets/session_key"
export ARK_API_KEY_FILE="$ROOT/secrets/ark_api_key"
export RUNTIME_CONFIG="$ROOT/config/runtime.json"
export MEDIA_ROOT="$ROOT/.local/verification-media"
export DATABASE_URL="${TEST_DATABASE_URL:-$("$ROOT/.venv/bin/python" -c 'import os,pathlib,urllib.parse; p=pathlib.Path(os.environ["ROOT"])/"secrets/postgres_password"; print("postgresql+psycopg://animation_local:"+urllib.parse.quote(p.read_text().strip(),safe="")+"@127.0.0.1:5432/animation_test")')}"
mkdir -p "$MEDIA_ROOT"
"$ROOT/.venv/bin/python" -m pytest "$ROOT/backend/tests" -q
npm --prefix "$ROOT/frontend" run build
docker compose --project-directory "$ROOT" -f "$ROOT/compose.yaml" -f "$ROOT/compose.dev.yaml" config --quiet
printf '离线闭环验证通过：隔离 PostgreSQL + 注入测试模型 + 真实 FFmpeg；没有调用方舟。\n'
