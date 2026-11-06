#!/usr/bin/env bash
set -euo pipefail
ROOT="$(dirname "$(dirname "$(realpath "$0")")")"
umask 077
mkdir -p "$ROOT/secrets" "$ROOT/config" "$ROOT/work" "$ROOT/data" "$ROOT/.local"
for name in postgres_password owner_password session_key; do
  if [ ! -s "$ROOT/secrets/$name" ]; then
    openssl rand -hex 32 > "$ROOT/secrets/$name"
  fi
done
touch "$ROOT/secrets/ark_api_key"
chmod 600 "$ROOT"/secrets/*
if [ ! -f "$ROOT/config/runtime.json" ]; then
  cat > "$ROOT/config/runtime.json" <<'JSON'
{
  "models": {
    "ark_base_url": "https://ark.cn-beijing.volces.com/api/v3",
    "script_model_id": "doubao-seed-2-1-pro-260628",
    "video_model_id": "doubao-seedance-2-0-260128",
    "api_key_file": "/run/secrets/ark_api_key",
    "project_override": true
  },
  "storage": {"backend": "local"},
  "worker": {"queue_backend": "postgresql", "composition_workers": 1}
}
JSON
fi
printf '配置已准备。登录密码：secrets/owner_password；方舟密钥：secrets/ark_api_key。\n'
printf '启动：docker compose -f compose.yaml -f compose.dev.yaml up -d --build --wait\n'
