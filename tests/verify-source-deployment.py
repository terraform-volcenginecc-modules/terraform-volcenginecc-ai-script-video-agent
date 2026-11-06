#!/usr/bin/env python3
"""Offline release and installer verification. No Docker/cloud/model operations."""
import contextlib
import base64
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import types
import urllib.error

PROJECT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify():
    packager = load("packager", PROJECT / "scripts/package-release.py")
    deploy = load("deploy", PROJECT / "scripts/ecs-deploy.py")
    checks = []
    with tempfile.TemporaryDirectory() as directory:
        temp = Path(directory)
        with contextlib.redirect_stdout(io.StringIO()):
            packager.package("verification", temp)
            bundle = next(temp.glob("*.tar.gz"))
            original = bundle.read_bytes()
            packager.package("verification", temp)
        assert bundle.read_bytes() == original
        checks.append("reproducible archive")
        extracted = temp / "extracted"
        extracted.mkdir()
        manifest = deploy.extract_release(bundle, extracted)
        assert not any(
            part in packager.EXCLUDED or part == ".env" or part.endswith(".tfvars")
            for name in manifest["files"] for part in Path(name).parts
        )
        checks.append("manifest integrity and local-state exclusions")
        for kind in ("traversal", "symlink", "duplicate"):
            bad = temp / f"{kind}.tar.gz"
            with tarfile.open(bad, "w:gz") as archive:
                member = tarfile.TarInfo("../escape" if kind == "traversal" else "file")
                if kind == "symlink":
                    member.type = tarfile.SYMTYPE
                    member.linkname = "/etc/passwd"
                archive.addfile(member, io.BytesIO(b""))
                if kind == "duplicate":
                    archive.addfile(member, io.BytesIO(b""))
            try:
                deploy.extract_release(bad, extracted)
                raise AssertionError(f"{kind} archive accepted")
            except ValueError:
                checks.append(f"reject {kind}")
        root = temp / "runtime"
        root.mkdir()
        (root / "config").mkdir()
        (root / "secrets").mkdir(mode=0o700)
        deploy.ROOT = root
        config = {
            "sha256": hashlib.sha256(original).hexdigest(),
            "bucket": "offline-only", "region": "cn-beijing",
            "endpoint": "https://tos-cn-beijing.volces.com",
            "role_name": "offline-role", "object_key": "releases/test/source.tar.gz",
        }
        downloads = []
        class Client:
            def __init__(self, **kwargs):
                assert kwargs["credentials_provider"] == "offline-role"
            def head_object(self, bucket, key, **kwargs):
                return types.SimpleNamespace(content_length=len(original) if key.startswith("releases/") else 12)
            def get_object_to_file(self, bucket, key, target, **kwargs):
                downloads.append(key)
                if key.startswith("releases/"):
                    shutil.copyfile(bundle, target)
                else:
                    Path(target).write_bytes(b"fake-secret\n")
        sys.modules["tos"] = types.SimpleNamespace(TosClientV2=Client)
        sys.modules["tos.credential"] = types.SimpleNamespace(EcsCredentialsProvider=lambda name: name)
        commands = []
        fail_health = False
        def install_native(release):
            commands.append(release)
            if fail_health:
                raise RuntimeError("offline unhealthy native services")
        deploy.install_native = install_native
        def execute():
            (root / "config/deployment.json").write_text(json.dumps(config))
            with contextlib.redirect_stderr(io.StringIO()):
                return deploy.main()
        assert execute() == 0
        assert json.loads((root / "deployment-status.json").read_text())["phase"] == "ready_without_ark_key"
        checks.append("automatic start without Ark key")
        assert execute() == 0
        assert len(downloads) == 1
        assert commands[0] == commands[1]
        checks.append("restart reuses pinned native release")
        config["ark_secret_object_key"] = "deployment-secrets/ark_api_key"
        assert execute() == 0
        secret = root / "secrets/ark_api_key"
        assert secret.read_bytes() == b"fake-secret\n" and secret.stat().st_mode & 0o777 == 0o600
        assert json.loads((root / "deployment-status.json").read_text())["phase"] == "ready"
        checks.append("private secret download and file permissions")
        fail_health = True
        assert execute() == 1
        assert json.loads((root / "deployment-status.json").read_text())["phase"] == "failed_starting"
        checks.append("unhealthy startup is not ready")
        fail_health = False
        assert execute() == 0
        checks.append("retry after startup failure")
        config["sha256"] = "0" * 64
        commands.clear()
        assert execute() == 1 and not commands
        assert json.loads((root / "deployment-status.json").read_text())["phase"] == "failed_downloading"
        checks.append("SHA mismatch blocks native installation and startup")
        public_files = {
            name: base64.b64encode((extracted / name).read_bytes()).decode()
            for name in manifest["files"]
            if name.startswith(("backend/app/", "frontend/dist/")) or name in {
                "scripts/ecs-native.py", "scripts/ecs-public-web.py", "frontend/nginx.conf",
                "frontend/nginx-public.conf", "backend/requirements.txt"
            }
        }
        payload = json.dumps(public_files, sort_keys=True, separators=(",", ":")).encode()
        encoded = base64.b64encode(gzip.compress(payload, mtime=0))
        config.pop("ark_secret_object_key")
        config.update(
            sha256=hashlib.sha256(payload).hexdigest(),
            payload_format="base64gzip_json",
            source_url="https://offline-source.tos-cn-beijing.volces.com/releases/source.txt",
        )
        config["object_key"] = "releases/public-source.txt"
        before = len(downloads)
        saved_tos = sys.modules["tos"]
        sys.modules["tos"] = None
        deploy.urllib.request.urlopen = lambda url, timeout: io.BytesIO(encoded)
        assert execute() == 0 and len(downloads) == before
        checks.append("public source download without SDK or role credentials")
        assert (root / "releases" / config["sha256"] / "frontend/dist/index.html").is_file()
        checks.append("public payload reconstructs prebuilt native runtime")
        sys.modules["tos"] = saved_tos
        config["secret_source"] = {
            "bucket": "private-secret-only", "region": "cn-beijing",
            "endpoint": "https://tos-cn-beijing.volces.com",
            "object_key": "deployment-secrets/ark_api_key",
        }
        assert execute() == 0 and downloads[-1] == "deployment-secrets/ark_api_key"
        checks.append("public source with separately authorized private secret")
        config["sha256"] = "f" * 64
        commands.clear()
        assert execute() == 1 and not commands
        checks.append("public payload checksum mismatch blocks native installation")
        config.pop("secret_source")
        config.update(
            sha256=hashlib.sha256(payload).hexdigest(),
            payload_format="base64gzip_json_chunks",
            source_url="https://offline-source.tos-cn-beijing.volces.com/releases/test/manifest.json",
            object_key="releases/test/manifest.json",
        )
        chunk = 16384
        parts = [encoded[i:i + chunk] for i in range(0, len(encoded), chunk)]
        assert len(parts) > 2
        source_manifest = dict(
            format=1, payload_format="base64gzip_json", sha256=config["sha256"],
            encoded_sha256=hashlib.sha256(encoded).hexdigest(), encoded_size=len(encoded),
            chunk_size=chunk, chunk_count=len(parts),
        )
        part_urls = [config["source_url"].replace("manifest.json", f"parts/part-{i:04d}.txt")
                     for i in range(len(parts))]
        requested = []
        objects = {}
        def set_manifest(value):
            raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            objects[config["source_url"]] = raw
            config["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
        def reset_parts():
            objects.clear()
            objects.update(zip(part_urls, parts))
            set_manifest(source_manifest)
            requested.clear()
        def open_public(url, timeout):
            requested.append(url)
            if url not in objects:
                raise urllib.error.HTTPError(url, 404, "offline missing part", {}, None)
            return io.BytesIO(objects[url])
        deploy.urllib.request.urlopen = open_public
        sys.modules["tos"] = None
        public_release = root / "releases" / config["sha256"]
        shutil.rmtree(public_release)
        reset_parts()
        assert execute() == 0
        assert requested == [config["source_url"], *part_urls]
        for name, value in public_files.items():
            assert (public_release / name).read_bytes() == base64.b64decode(value)
        checks.append("chunked HTTPS download reconstructs every file without SDK")
        requested.clear()
        assert execute() == 0 and not requested
        checks.append("chunked restart reuses verified release")
        shutil.rmtree(public_release)
        for fault in ("missing", "reordered", "corrupt", "truncated", "oversized_part",
                      "manifest_checksum", "oversized_manifest", "invalid_json", "manifest_list",
                      "wrong_format", "wrong_payload_format", "wrong_source_checksum",
                      "wrong_encoded_checksum", "zero_size", "oversized_payload", "wrong_count",
                      "boolean_count", "wrong_chunk_size", "noninteger_size"):
            reset_parts()
            if fault == "missing":
                del objects[part_urls[1]]
            elif fault == "reordered":
                objects[part_urls[0]], objects[part_urls[1]] = parts[1], parts[0]
            elif fault == "corrupt":
                objects[part_urls[0]] = (b"A" if parts[0][:1] != b"A" else b"B") + parts[0][1:]
            elif fault == "truncated":
                objects[part_urls[-1]] = parts[-1][:-1]
            elif fault == "oversized_part":
                objects[part_urls[0]] += b"A"
            elif fault == "manifest_checksum":
                config["manifest_sha256"] = "0" * 64
            elif fault in ("oversized_manifest", "invalid_json"):
                raw = b"x" * (chunk + 1 if fault == "oversized_manifest" else 1)
                objects[config["source_url"]] = raw
                config["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
            elif fault == "manifest_list":
                set_manifest([])
            else:
                key, value = {
                    "wrong_format": ("format", 2),
                    "wrong_payload_format": ("payload_format", "other"),
                    "wrong_source_checksum": ("sha256", "0" * 64),
                    "wrong_encoded_checksum": ("encoded_sha256", "0" * 64),
                    "zero_size": ("encoded_size", 0),
                    "oversized_payload": ("encoded_size", 10 * 1024 * 1024 + 1),
                    "wrong_count": ("chunk_count", len(parts) + 1),
                    "boolean_count": ("chunk_count", True),
                    "wrong_chunk_size": ("chunk_size", 65536),
                    "noninteger_size": ("encoded_size", str(len(encoded))),
                }[fault]
                set_manifest(dict(source_manifest, **{key: value}))
            commands.clear()
            assert execute() == 1 and not commands and not public_release.exists(), fault
            assert json.loads((root / "deployment-status.json").read_text())["phase"] == "failed_downloading"
            checks.append(f"chunked {fault} blocks installation without publishing release")
        reset_parts()
        assert execute() == 0
        checks.append("chunked retry succeeds after correcting download")
        sys.modules["tos"] = saved_tos
        bad_payload = json.dumps({"../escape": ""}).encode()
        try:
            deploy.extract_public_payload(
                base64.b64encode(gzip.compress(bad_payload)), extracted,
                hashlib.sha256(bad_payload).hexdigest(),
            )
            raise AssertionError("Public path traversal accepted")
        except ValueError:
            checks.append("public payload rejects path traversal")
    print(json.dumps({"passed": len(checks), "checks": checks}, indent=2))


if __name__ == "__main__":
    verify()
