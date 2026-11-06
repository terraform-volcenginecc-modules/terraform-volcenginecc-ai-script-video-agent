#!/usr/bin/env python3
"""Offline native orchestration checks; no package installs or host service changes."""
import importlib.util
import io
from pathlib import Path
import tempfile
import types

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("native", PROJECT / "scripts/ecs-native.py")
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


def verify():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        native.ROOT = root
        native.SYSTEMD = root / "systemd"
        native.NGINX = root / "nginx"
        for name in ("secrets", "config", "releases/revision/backend",
                     "releases/revision/frontend", "releases/revision/scripts", "systemd",
                     "nginx/sites-available", "nginx/sites-enabled"):
            (root / name).mkdir(parents=True, exist_ok=True)
        release = root / "releases/revision"
        (release / "frontend/nginx.conf").write_text((PROJECT / "frontend/nginx.conf").read_text())
        (release / "frontend/nginx-public.conf").write_text((PROJECT / "frontend/nginx-public.conf").read_text())
        (release / "scripts/ecs-public-web.py").write_text((PROJECT / "scripts/ecs-public-web.py").read_text())
        (root / "secrets/postgres_password").write_text("a" * 64 + "\n")
        (root / "nginx/sites-enabled/default").symlink_to("/nonexistent-default")
        commands = []
        database_exists = False
        def run(args, **kwargs):
            commands.append((args, kwargs))
            if args[0] == "id":
                return types.SimpleNamespace(returncode=1)
            if args[:3] == ("python3", "-m", "venv"):
                target = Path(args[3]) / "bin"
                target.mkdir(parents=True, exist_ok=True)
                executable = target / "python"
                executable.write_text("#!/bin/sh\n")
                executable.chmod(0o755)
            if "install" == args[0]:
                for name in args[-2:]:
                    Path(name).mkdir(parents=True, exist_ok=True)
            if "-At" in args:
                return types.SimpleNamespace(returncode=0, stdout="1\n" if database_exists else "")
            return types.SimpleNamespace(returncode=0, stdout="123\n")
        native.subprocess = types.SimpleNamespace(run=run, DEVNULL=-3)
        class Response(io.BytesIO):
            status = 200
        native.urllib.request = types.SimpleNamespace(urlopen=lambda *a, **k: Response(b'{"status":"ok"}'))
        native.worker_ready = lambda pid: pid == "123"
        native.install(release)
        assert not any(c[0][0] in {"docker", "npm"} for c in commands)
        assert any("createdb" in c[0] for c in commands)
        sql_call = next(c for c in commands if "input" in c[1])
        assert "a" * 64 in sql_call[1]["input"]
        assert all("a" * 64 not in " ".join(c[0]) for c in commands)
        assert not (root / "nginx/sites-enabled/default").is_symlink()
        nginx = (root / "nginx/sites-available/ai-animation").read_text()
        assert "listen 127.0.0.1:8080;" in nginx and "http://127.0.0.1:8000" in nginx
        assert str(release / "frontend/dist") in nginx and "client_max_body_size 41m" in nginx
        public = (root / "nginx/sites-available/ai-animation-public").read_text()
        assert "listen 0.0.0.0:80;" in public and "listen 0.0.0.0:443 ssl;" in public
        assert "http://127.0.0.1:8080" in public and "client_max_body_size 41m" in public
        tls = root / "nginx/ssl/ai-animation"
        assert (tls / "tls.key").stat().st_mode & 0o777 == 0o600
        original_key = (tls / "tls.key").read_bytes()
        for kind in ("api", "worker"):
            unit = (native.SYSTEMD / f"ai-animation-{kind}.service").read_text()
            assert "User=ai-animation" in unit and "Restart=always" in unit
            assert "LoadCredential=ark_api_key:" in unit and "ARK_API_KEY_FILE=%d/ark_api_key" in unit
            assert "DATABASE_HOST=127.0.0.1" in unit and "ProtectSystem=strict" in unit
        assert (root / "native-release").read_text() == "revision\n"
        count = len(commands)
        database_exists = True
        native.install(release)
        repeated = commands[count:]
        assert not any(c[0][0] == "python3" or "createdb" in c[0] for c in repeated)
        assert (release / ".venv/bin/python").stat().st_mode & 0o111
        assert (tls / "tls.key").read_bytes() == original_key
        native.worker_ready = lambda pid: False
        native.time = types.SimpleNamespace(monotonic=iter([0, 121]).__next__, sleep=lambda n: None)
        try:
            native.install(release)
            raise AssertionError("Missing worker heartbeat accepted")
        except RuntimeError as error:
            assert "health check timed out" in str(error)
        print("12 native orchestration checks passed: install, reuse, SQL secrecy, loopback, uploads, credentials, supervision, executable permissions, worker readiness, public HTTP/HTTPS, TLS key permissions, certificate reuse")


if __name__ == "__main__":
    verify()
