#!/usr/bin/env python3
"""Produce a reproducible application + Terraform source bundle, without local state."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import subprocess

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {
    "node_modules", "__pycache__", ".pytest_cache", ".venv", ".git",
    ".terraform", ".local", "secrets", "data", "work", "releases", "tests",
}
TOP_FILES = {
    "compose.yaml", "compose.dev.yaml", ".dockerignore", ".env.example",
    "README.md", "README.zh-CN.md", ".gitignore", ".gitattributes",
    "versions.tf", "providers.tf", "variables.tf", "main.tf",
    "bootstrap.tf", "outputs.tf", "delivery.tf",
}


def package(version, output):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", version):
        raise ValueError("Version must contain only letters, digits, dot, dash or underscore")
    if not (ROOT / "frontend/dist/index.html").is_file():
        raise ValueError("Native release requires prebuilt frontend/dist")
    paths = [ROOT / name for name in TOP_FILES if (ROOT / name).is_file()]
    for directory in ("backend", "frontend", "scripts"):
        for path in (ROOT / directory).rglob("*"):
            relative = path.relative_to(ROOT)
            if any(part in EXCLUDED or part.startswith(".env") for part in relative.parts):
                continue
            if path.is_symlink():
                raise ValueError(f"Symlink is not allowed: {relative}")
            if path.is_file():
                if path.suffix in {".pyc", ".log", ".tsbuildinfo"} or ".tfstate" in path.name or path.name.endswith(".tfvars"):
                    continue
                paths.append(path)
    files = {}
    for path in sorted(paths):
        name = path.relative_to(ROOT).as_posix()
        # Fail closed on unexpected local credential files inside source directories.
        if any(word in path.name.lower() for word in ("credential", "private-key", "id_rsa", "id_ed25519")):
            raise ValueError(f"Possible credential file: {name}")
        files[name] = path.read_bytes()
    manifest = {
        "format": 1, "version": version,
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
    }
    files["release-manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    output.mkdir(parents=True, exist_ok=True)
    staging = output / f".{version}.tmp"
    try:
        with staging.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as archive:
                for name, data in sorted(files.items()):
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    info.mode = 0o755 if name.endswith(".sh") else 0o644
                    archive.addfile(info, io.BytesIO(data))
        digest = hashlib.sha256(staging.read_bytes()).hexdigest()
        target = output / f"ai-animation-{version}-{digest[:16]}.tar.gz"
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError("Refusing to overwrite a different release")
        staging.replace(target)
    finally:
        staging.unlink(missing_ok=True)
    (output / f"{target.name}.sha256").write_text(f"{digest}  {target.name}\n")
    metadata = {
        "version": version, "filename": target.name, "sha256": digest,
        "object_key": f"releases/{version}/{digest}/{target.name}",
        "file_count": len(files), "size_bytes": target.stat().st_size,
    }
    (output / f"{target.name}.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (output / f"{target.name}.tfvars.example").write_text(
        '# Merge this block into terraform.tfvars after uploading the archive.\n'
        'source_release = {\n'
        '  bucket     = "your-existing-delivery-bucket"\n'
        '  region     = "cn-beijing"\n'
        '  endpoint   = "https://tos-cn-beijing.volces.com"\n'
        f'  object_key = "{metadata["object_key"]}"\n'
        f'  sha256     = "{digest}"\n'
        '  # version_id = "TOS object version ID"\n'
        '}\n'
    )
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "releases")
    args = parser.parse_args()
    # Build on the publisher's machine. Stack users and ECS need no Node/npm.
    subprocess.run(["npm", "--prefix", str(ROOT / "frontend"), "run", "build"], check=True)
    package(args.version, args.output.resolve())
