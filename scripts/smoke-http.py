"""Check the running Web/API entrypoint without model calls or mutations."""
import argparse
from pathlib import Path

import httpx

parser = argparse.ArgumentParser()
parser.add_argument("--url", default="http://127.0.0.1:8080")
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
with httpx.Client(base_url=args.url, timeout=10) as client:
    assert client.get("/").status_code == 200
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/api/projects").status_code == 401
    response = client.post("/api/login",
        json={"password": (root / "secrets/owner_password").read_text().strip()},
        headers={"Origin": args.url})
    assert response.status_code == 200, f"登录返回 {response.status_code}"
    client.headers["X-CSRF-Token"] = response.json()["csrf"]
    config = client.get("/api/config")
    assert config.status_code == 200 and "key_configured" in config.json()
    assert "api_key" not in config.text.lower()
    assert client.get("/api/projects").status_code == 200
    assert client.post("/api/logout", json={}).status_code == 200
    assert client.get("/api/session").status_code == 401
print("HTTP 入口、登录、CSRF 会话、退出与健康检查通过；未调用模型。")
