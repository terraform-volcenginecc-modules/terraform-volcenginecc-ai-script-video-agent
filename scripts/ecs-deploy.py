#!/usr/bin/env python3
"""Root-only cloud-init installer."""
import hashlib
import importlib.util
import base64
import gzip
import io
import json
from pathlib import Path, PurePosixPath
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request

ROOT = Path("/opt/ai-animation")
MAX_ENCODED = 10 * 1024 * 1024
CHUNK_SIZE = 16 * 1024


def validate_native_files(names):
    common = ("backend/app/api.py", "backend/app/worker.py", "backend/requirements.txt",
              "scripts/ecs-native.py", "scripts/ecs-public-web.py")
    if "scripts/安装ComfyUI.py" in names and "scripts/install-comfyui.py" not in names:
        layout = ("frontend/登录.html", "frontend/comfyui扩展/工作室.js",
                  "backend/comfyui扩展.py", "开源组件.json", "scripts/安装ComfyUI.py",
                  "frontend/nginx.conf", "frontend/nginx-public.conf")
    elif "frontend/comfyui-extension/studio.js" in names and "frontend/comfyui-extension/auth.js" not in names:
        layout = ("frontend/login/index.html", "frontend/login/login.js",
                  "frontend/comfyui-extension/studio.js", "frontend/comfyui-extension/studio.css",
                  "backend/comfyui_extension.py", "scripts/comfyui-release.json",
                  "scripts/install-comfyui.py", "scripts/comfyui-requirements.txt",
                  "frontend/nginx/studio.conf", "frontend/nginx/public.conf")
    else:
        layout = ("frontend/login/index.html", "frontend/login/login.js",
                  "frontend/comfyui-extension/auth.js", "workflows/ark-text-to-video.json",
                  "backend/comfyui_extension.py", "scripts/comfyui-release.json",
                  "scripts/install-comfyui.py", "scripts/comfyui-requirements.txt",
                  "frontend/nginx/studio.conf", "frontend/nginx/public.conf")
    for name in (*common, *layout):
        if name not in names:
            raise ValueError(f"Required native release file missing: {name}")


def install_native(release):
    spec = importlib.util.spec_from_file_location("ecs_native", release / "scripts/ecs-native.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.install(release)


def atomic_write(path, data, mode=0o600):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(data)
    temporary.chmod(mode)
    temporary.replace(path)


def extract_release(archive, destination):
    """Accept only ordinary files; verify the full manifest before any builds."""
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        names = set()
        if len(members) > 10000 or sum(m.size for m in members) > 100 * 1024 * 1024:
            raise ValueError("Release exceeds source bundle limits")
        for member in members:
            name = PurePosixPath(member.name)
            if (not member.isfile() or name.is_absolute() or ".." in name.parts
                    or "\\" in member.name or member.name != name.as_posix()
                    or member.name in names):
                raise ValueError("Unsafe archive member")
            names.add(member.name)
        bundle.extractall(destination, filter="data")
    manifest = json.loads((destination / "release-manifest.json").read_text())
    if manifest["format"] != 1 or names != set(manifest["files"]) | {"release-manifest.json"}:
        raise ValueError("Release manifest does not match archive")
    for name, digest in manifest["files"].items():
        if hashlib.sha256((destination / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Release file checksum mismatch: {name}")
    validate_native_files(manifest["files"])
    return manifest


def download(client, config, key, target, version_id=None, max_bytes=100 * 1024 * 1024):
    # Bound both the download and unpacked source size.
    response = client.head_object(config["bucket"], key, version_id=version_id)
    if response.content_length > max_bytes:
        raise ValueError("TOS object exceeds size limit")
    client.get_object_to_file(config["bucket"], key, str(target), version_id=version_id)
    target.chmod(0o600)


def extract_public_payload(encoded, destination, expected):
    with gzip.GzipFile(fileobj=io.BytesIO(base64.b64decode(encoded, validate=True))) as compressed:
        data = compressed.read(100 * 1024 * 1024 + 1)
    if len(data) > 100 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Public source payload size or checksum mismatch")
    files = json.loads(data)
    if not isinstance(files, dict) or len(files) > 10000:
        raise ValueError("Invalid public source payload")
    for name, value in files.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name or name != relative.as_posix():
            raise ValueError("Invalid public source path")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(value, validate=True))
    validate_native_files(files)


def public_download(url, limit):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Expected unsigned HTTPS public source URL")
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Source download exceeds limit")
    return data


def download_public_parts(config):
    url = config["source_url"]
    raw = public_download(url, CHUNK_SIZE)
    if hashlib.sha256(raw).hexdigest() != config["manifest_sha256"]:
        raise ValueError("Source manifest SHA256 mismatch")
    manifest = json.loads(raw)
    if (not isinstance(manifest, dict) or manifest.get("format") != 1
            or manifest.get("payload_format") != "base64gzip_json"
            or manifest.get("sha256") != config["sha256"]
            or not urllib.parse.urlsplit(url).path.endswith("/manifest.json")):
        raise ValueError("Invalid source manifest")
    size, count, chunk = (manifest.get(key) for key in ("encoded_size", "chunk_count", "chunk_size"))
    if (any(type(value) is not int for value in (size, count, chunk))
            or chunk != CHUNK_SIZE or not 0 < size <= MAX_ENCODED
            or count != (size + chunk - 1) // chunk):
        raise ValueError("Invalid source part limits")
    parts = []
    for index in range(count):
        expected_size = min(chunk, size - index * chunk)
        part_url = urllib.parse.urljoin(url, f"parts/part-{index:04d}.txt")
        part = public_download(part_url, expected_size)
        if len(part) != expected_size:
            raise ValueError("Source part size mismatch")
        parts.append(part)
    encoded = b"".join(parts)
    if hashlib.sha256(encoded).hexdigest() != manifest.get("encoded_sha256"):
        raise ValueError("Source parts SHA256 mismatch")
    return encoded


def main():
    config = json.loads((ROOT / "config/deployment.json").read_text())
    digest = config["sha256"]
    status = ROOT / "deployment-status.json"
    phase = "initializing"
    def report(phase):
        atomic_write(status, (json.dumps({
            "phase": phase, "sha256": digest, "object_key": config["object_key"],
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }) + "\n").encode(), 0o644)
    def client_for(reference):
        from tos import TosClientV2
        from tos.credential import EcsCredentialsProvider
        return TosClientV2(
            endpoint=reference["endpoint"], region=reference["region"],
            credentials_provider=EcsCredentialsProvider(config["role_name"]),
            max_retry_count=5,
        )
    try:
        phase = "downloading"
        report(phase)
        releases = ROOT / "releases"
        releases.mkdir(mode=0o750, exist_ok=True)
        release = releases / digest
        if not release.exists():
            with tempfile.TemporaryDirectory(dir=releases) as directory:
                temp = Path(directory)
                extracted = temp / "source"
                extracted.mkdir()
                if config.get("payload_format") in ("base64gzip_json", "base64gzip_json_chunks"):
                    encoded = (download_public_parts(config)
                               if config["payload_format"] == "base64gzip_json_chunks"
                               else public_download(config["source_url"], MAX_ENCODED))
                    extract_public_payload(encoded, extracted, digest)
                else:
                    archive = temp / "source.tar.gz"
                    download(client_for(config), config, config["object_key"], archive, config.get("version_id"))
                    if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
                        raise ValueError("Source archive SHA256 mismatch")
                    extract_release(archive, extracted)
                extracted.rename(release)
        # The required key is written by cloud-init to a root-only file.
        phase = "configuring_secret"
        report(phase)
        secret = ROOT / "secrets/ark_api_key"
        data = secret.read_bytes().strip() if secret.is_file() else b""
        if not data or b"\n" in data or b"\r" in data or b"\x00" in data:
            raise ValueError("Required Ark API key is missing or invalid")
        data.decode("utf-8")
        phase = "starting"
        report(phase)
        install_native(release)
        report("ready")
    except Exception:
        report(f"failed_{phase}")
        # Detailed SDK exceptions may contain authorization headers; do not print them.
        print("Deployment failed; inspect the phase and build logs. Check TOS references, role permissions and SHA256.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
