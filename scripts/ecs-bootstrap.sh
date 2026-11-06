#!/bin/bash
set -euo pipefail
umask 077
install -d -m 0700 /opt/ai-animation/bootstrap
if [ ! -x /opt/ai-animation/bootstrap/venv/bin/python ]; then
  python3 -m venv /opt/ai-animation/bootstrap/venv
fi
needs_sdk=$(/opt/ai-animation/bootstrap/venv/bin/python -c 'import json; c=json.load(open("/opt/ai-animation/config/deployment.json")); print(int(c.get("payload_format") not in ("base64gzip_json", "base64gzip_json_chunks") or bool(c.get("secret_source")) or bool(c.get("ark_secret_object_key"))))')
if [ "$needs_sdk" = "1" ] && ! /opt/ai-animation/bootstrap/venv/bin/python -c 'import tos; from tos.credential import EcsCredentialsProvider' >/dev/null 2>&1; then
  /opt/ai-animation/bootstrap/venv/bin/pip install --index-url https://mirrors.ivolces.com/pypi/simple/ --disable-pip-version-check 'tos==2.8.7'
fi
exec /opt/ai-animation/bootstrap/venv/bin/python /usr/local/lib/ai-animation-deploy.py
