#!/usr/bin/env python3
"""Configure public HTTP/HTTPS; retain supplied TLS certificates across restarts."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile


def configure(config_path, nginx_root=Path("/etc/nginx")):
    tls = nginx_root / "ssl/ai-animation"
    tls.mkdir(parents=True, exist_ok=True, mode=0o700)
    cert, key = tls / "tls.crt", tls / "tls.key"
    if cert.exists() != key.exists():
        raise ValueError("TLS certificate and private key must both exist or both be absent")
    if not cert.exists():
        previous_umask = os.umask(0o077)
        try:
            with tempfile.TemporaryDirectory(dir=tls) as directory:
                temporary = Path(directory)
                subprocess.run([
                    "openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048",
                    "-days", "365", "-subj", "/CN=ai-animation.local",
                    "-addext", "subjectAltName=DNS:ai-animation.local,DNS:localhost,IP:127.0.0.1",
                    "-keyout", str(temporary / "tls.key"), "-out", str(temporary / "tls.crt"),
                ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                (temporary / "tls.key").chmod(0o600)
                (temporary / "tls.crt").chmod(0o644)
                (temporary / "tls.key").replace(key)
                (temporary / "tls.crt").replace(cert)
        finally:
            os.umask(previous_umask)
    key.chmod(0o600)
    content = config_path.read_text().replace("/etc/nginx/ssl/ai-animation", str(tls))
    available = nginx_root / "sites-available/ai-animation-public"
    temporary = available.with_name(available.name + ".tmp")
    temporary.write_text(content)
    temporary.chmod(0o644)
    temporary.replace(available)
    enabled = nginx_root / "sites-enabled/ai-animation-public"
    if enabled.is_symlink():
        if enabled.resolve() != available.resolve():
            raise ValueError("Public Nginx site points to an unexpected configuration")
    elif enabled.exists():
        raise ValueError("Public Nginx enabled site must be a symlink")
    else:
        enabled.symlink_to(available)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path,
                        default=Path(__file__).resolve().parents[1] / "frontend/nginx-public.conf")
    args = parser.parse_args()
    configure(args.config)
    subprocess.run(["nginx", "-t"], check=True)
    subprocess.run(["systemctl", "reload-or-restart", "nginx.service"], check=True)
