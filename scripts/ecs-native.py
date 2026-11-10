#!/usr/bin/env python3
"""在 Ubuntu 24.04 安装官方 ComfyUI 与独立业务适配服务。"""
import os
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import time
import urllib.request
from urllib.parse import urlsplit

ROOT = Path("/opt/ai-animation")
SYSTEMD = Path("/etc/systemd/system")
NGINX = Path("/etc/nginx")


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def write(path, content, mode=0o644):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content)
    temporary.chmod(mode)
    temporary.replace(path)


def worker_ready(pid):
    heartbeat = Path(f"/proc/{pid}/root/tmp/worker-heartbeat")
    return heartbeat.is_file() and time.time() - heartbeat.stat().st_mtime <= 60


def service(release, kind):
    command = "-m uvicorn app.api:app --host 127.0.0.1 --port 8000" if kind == "api" else "-m app.worker"
    working = release / "backend"
    python = release / ".venv/bin/python"
    if kind == "comfyui":
        working = release / "ComfyUI"
        python = release / ".comfyui-venv/bin/python"
        command = ("main.py --cpu --listen 127.0.0.1 --port 8188 --disable-auto-launch "
                   "--disable-all-custom-nodes --whitelist-custom-nodes ai_animation "
                   "--disable-partner-nodes --user-directory /opt/ai-animation/work/comfyui/user "
                   "--input-directory /opt/ai-animation/work/comfyui/input "
                   "--output-directory /opt/ai-animation/work/comfyui/output "
                   "--temp-directory /opt/ai-animation/work/comfyui/temp "
                   "--database-url sqlite:////opt/ai-animation/work/comfyui/comfyui.db")
    names = ["postgres_password", "owner_password", "session_key", "ark_api_key"]
    runtime = json.loads((ROOT / "config/runtime.json").read_text())
    if runtime.get("comfyui"):
        names.append("comfyui_api_key")
    credentials = "\n".join(
        f"LoadCredential={name}:{ROOT}/secrets/{name}\nEnvironment={name.upper()}_FILE=%d/{name}"
        for name in names
    )
    return f"""[Unit]
Description=AI animation {kind}
After=network-online.target postgresql.service
Wants=network-online.target postgresql.service
StartLimitIntervalSec=0

[Service]
User=ai-animation
Group=ai-animation
WorkingDirectory={working}
Environment=DATABASE_HOST=127.0.0.1
Environment=RUNTIME_CONFIG={ROOT}/config/runtime.json
Environment=MEDIA_ROOT={ROOT}/work/media
Environment=PYTHONUNBUFFERED=1
{credentials}
ExecStart={python} {command}
Restart=always
RestartSec=5
TimeoutStopSec=120
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={ROOT}/work

[Install]
WantedBy=multi-user.target
"""


def install(release):
    os.umask(0o022)
    # The account owns media only; source, configuration and original secrets stay root-owned.
    exists = subprocess.run(["id", "-u", "ai-animation"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL).returncode == 0
    if not exists:
        run("useradd", "--system", "--user-group", "--home-dir", str(ROOT),
            "--no-create-home", "--shell", "/usr/sbin/nologin", "ai-animation")
    ROOT.chmod(0o755)
    (ROOT / "releases").chmod(0o755)
    (ROOT / "config").chmod(0o755)
    release.chmod(0o755)
    run("install", "-d", "-o", "ai-animation", "-g", "ai-animation", "-m", "0700",
        str(ROOT / "work"), str(ROOT / "work/media"))
    # Bootstrap has a restrictive umask; extracted source must be readable by service users.
    for path in release.rglob("*"):
        if any(part in (".venv", ".comfyui-venv") for part in path.relative_to(release).parts):
            continue
        path.chmod(0o755 if path.is_dir() else 0o644)
    python = release / ".venv/bin/python"
    marker = release / ".native-installed"
    spec = importlib.util.spec_from_file_location("comfyui_installer", release / "scripts/安装ComfyUI.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    if not marker.exists():
        run("python3", "-m", "venv", str(release / ".venv"))
        installer.install_packages(python, ["-r", str(release / "backend/requirements.txt")])
        marker.touch()
    installer.install(release)
    run("install", "-d", "-o", "ai-animation", "-g", "ai-animation", "-m", "0700",
        str(ROOT / "work/comfyui"),
        *(str(ROOT / "work/comfyui" / name) for name in ("user", "input", "output", "temp")))
    # ComfyUI 使用自己的用户目录和数据库，官方源码保持只读。
    for path in (release / "ComfyUI").rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    # 安装依赖后、启动服务前校验可选视频配置；不提交任何模型任务。
    run(str(python), "-c", "from app.config import validate_video_config; validate_video_config()",
        cwd=release / "backend", env={**os.environ, "RUNTIME_CONFIG": str(ROOT / "config/runtime.json")})
    run("systemctl", "enable", "--now", "postgresql.service")
    # Password travels on stdin, never through process arguments or environment.
    password = (ROOT / "secrets/postgres_password").read_text().strip()
    if not re.fullmatch(r"[a-f0-9]{64}", password):
        raise ValueError("Expected generated PostgreSQL password")
    statement = (
        "DO $body$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='animation') "
        "THEN CREATE ROLE animation LOGIN; END IF; END $body$;\n"
        f"ALTER ROLE animation PASSWORD '{password}';\n"
    )
    run("runuser", "-u", "postgres", "--", "psql", "-q", "-v", "ON_ERROR_STOP=1",
        input=statement, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    present = run("runuser", "-u", "postgres", "--", "psql", "-At", "-v", "ON_ERROR_STOP=1",
                  "-c", "SELECT 1 FROM pg_database WHERE datname='animation'",
                  capture_output=True, text=True)
    if present.stdout.strip() != "1":
        run("runuser", "-u", "postgres", "--", "createdb", "-O", "animation", "animation")
    for kind in ("api", "worker", "comfyui"):
        write(SYSTEMD / f"ai-animation-{kind}.service", service(release, kind))
    nginx = (release / "frontend/nginx.conf").read_text()
    nginx = nginx.replace("listen 8080;", "listen 127.0.0.1:8080;")
    nginx = nginx.replace("/usr/share/nginx/html", str(release / "frontend"))
    nginx = nginx.replace("http://api:8000", "http://127.0.0.1:8000")
    nginx = nginx.replace("http://comfyui:8188", "http://127.0.0.1:8188")
    storage = json.loads((ROOT / "config/runtime.json").read_text()).get("storage", {})
    media_origin = ""
    if storage.get("auto_upload"):
        endpoint = storage.get("extranet_endpoint") or f"tos-{storage.get('region')}.volces.com"
        endpoint = urlsplit(endpoint if "://" in endpoint else "https://" + endpoint)
        bucket = storage.get("bucket", "")
        hostname = endpoint.netloc.removeprefix(bucket + ".")
        if (endpoint.scheme != "https" or not re.fullmatch(r"tos(-s3)?-[a-z0-9-]+\.volces\.com", hostname)
                or endpoint.path not in ("", "/") or endpoint.query or endpoint.fragment
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket)):
            raise ValueError("TOS 公网地址或桶名无效，无法配置素材访问策略")
        media_origin = f"https://{bucket}.{hostname}"
    nginx = nginx.replace("${MEDIA_ORIGIN}", media_origin)
    write(NGINX / "sites-available/ai-animation", nginx)
    # Ubuntu's default port-80 welcome site must not expose a second web entry.
    (NGINX / "sites-enabled/default").unlink(missing_ok=True)
    enabled = NGINX / "sites-enabled/ai-animation"
    if not enabled.is_symlink():
        enabled.symlink_to(NGINX / "sites-available/ai-animation")
    spec = importlib.util.spec_from_file_location("ecs_public_web", release / "scripts/ecs-public-web.py")
    public_web = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(public_web)
    public_web.configure(release / "frontend/nginx-public.conf", NGINX)
    run("nginx", "-t")
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "ai-animation-api.service", "ai-animation-worker.service",
        "ai-animation-comfyui.service", "nginx.service")
    run("systemctl", "restart", "ai-animation-api.service", "ai-animation-worker.service", "ai-animation-comfyui.service")
    run("systemctl", "restart", "nginx.service")
    deadline = time.monotonic() + 240
    while True:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8080/studio-api/health", timeout=3) as response:
                if response.status != 200:
                    raise RuntimeError("API not healthy")
            run("systemctl", "is-active", "--quiet", "ai-animation-worker.service")
            # PrivateTmp gives the worker its own heartbeat directory.
            pid = run("systemctl", "show", "-p", "MainPID", "--value", "ai-animation-worker.service",
                      capture_output=True, text=True).stdout.strip()
            if not worker_ready(pid):
                raise RuntimeError("Worker queue not ready")
            # 验证官方服务确实加载了业务节点，不提交模型任务。
            cookie = run(str(release / ".venv/bin/python"), "-c",
                         "from itsdangerous import URLSafeTimedSerializer; from pathlib import Path; "
                         "print(URLSafeTimedSerializer(Path('/opt/ai-animation/secrets/session_key').read_text().strip(), "
                         "salt='animation-owner-v1').dumps({'csrf':'health'}))",
                         capture_output=True, text=True).stdout.strip()
            request = urllib.request.Request("http://127.0.0.1:8188/object_info/VolcProject",
                                             headers={"Cookie": "session=" + cookie})
            with urllib.request.urlopen(request, timeout=5) as response:
                if "VolcProject" not in json.load(response):
                    raise RuntimeError("ComfyUI 业务节点未加载")
            break
        except Exception:
            if time.monotonic() >= deadline:
                raise RuntimeError("Native service health check timed out") from None
            time.sleep(2)
    write(ROOT / "native-release", release.name + "\n")
