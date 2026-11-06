"""Durable backup checks with an in-memory TOS transport; no cloud writes."""
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from test_flow import clean, client, clips, generate_story, project, process_all
from app import storage
from app.db import Asset, AssetBackup, Job, Session, migrate, now, uid
from app.media import path_for

CONFIG = {"bucket": "script-video", "region": "cn-beijing", "endpoint": "tos-cn-beijing.volces.com",
          "role_name": "fixture-role"}


class CloudError(Exception):
    def __init__(self, status=404):
        super().__init__("fixture-private-secret signed-url must not appear")
        self.status_code = status
        self.code = "NoSuchKey" if status == 404 else "AccessDenied"
        self.request_id = "req-backup"


class TOS:
    def __init__(self):
        self.objects = {}
        self.puts = []
        self.fail_after_put = False
        self.closed = 0
    def close(self):
        self.closed += 1
    def head_object(self, bucket, key):
        if (bucket, key) not in self.objects:
            raise CloudError()
        item = self.objects[bucket, key]
        return SimpleNamespace(meta=item["meta"], content_length=len(item["data"]), etag="fixture-etag")
    def put_object_from_file(self, bucket, key, file_path, **kwargs):
        from tos.enum import ACLType
        assert kwargs["acl"] == ACLType.ACL_Private
        assert kwargs["server_side_encryption"] == "AES256"
        data = Path(file_path).read_bytes()
        assert kwargs["meta"]["sha256"] == hashlib.sha256(data).hexdigest()
        self.puts.append((bucket, key, kwargs["content_type"]))
        self.objects[bucket, key] = {"data": data, "meta": kwargs["meta"]}
        if self.fail_after_put:
            self.fail_after_put = False
            raise ConnectionError("fixture-private-secret response lost")


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(storage, "runtime", lambda: {
        "storage": {**CONFIG, "auto_upload": True},
        "infrastructure": {"role_name": CONFIG["role_name"]},
    })
    return CONFIG


def asset(pid, kind="video", preview=False):
    asset_id = uid()
    relative = f"{pid}/backup-fixture/{asset_id}/asset"
    target = path_for(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"video-or-image-fixture")
    metadata = {"mime_type": "image/jpeg"} if kind == "image" else {}
    if preview:
        preview_path = str(Path(relative).with_name("source-preview.mp4"))
        path_for(preview_path).write_bytes(b"normalized-preview-fixture")
        metadata["source_preview_path"] = preview_path
    with Session.begin() as db:
        db.add(Asset(id=asset_id, project_id=pid, kind=kind, path=relative, metadata_json=metadata))
    return asset_id


def test_discovery_private_upload_and_reference_preview(client, enabled):
    pid = project(client)
    video_id, frame_id = asset(pid), asset(pid, "image", preview=True)
    assert storage.enqueue_missing(CONFIG) == 2
    assert storage.enqueue_missing(CONFIG) == 0
    cloud = TOS()
    for _ in range(2):
        storage.execute(storage.claim(), CONFIG, lambda config: cloud)
    assert storage.claim() is None
    assert len(cloud.puts) == 3 and cloud.closed == 2
    assert all(bucket == "script-video" and key.startswith(f"media/{pid}/") for bucket, key, _ in cloud.puts)
    frame = client.get(f"/api/assets/{frame_id}/info").json()
    assert frame["backup"]["status"] == "succeeded"
    assert frame["backup"]["object_key"].endswith("frame.jpg")
    assert frame["backup"]["source_preview"]["key"].endswith("source-preview.mp4")
    with Session() as db:
        assert db.get(AssetBackup, video_id).result["sha256"]
        assert path_for(db.get(Asset, video_id).path).is_file()
    # A forced recovery reuses the existing object without creating another version.
    with Session.begin() as db:
        db.get(AssetBackup, video_id).status = "queued"
    storage.execute(storage.claim(), CONFIG, lambda config: cloud)
    assert len(cloud.puts) == 3


def test_uncertain_upload_retries_same_object_without_model_call(client, enabled, caplog):
    pid = project(client)
    generate_story(client, pid)
    asset_id = asset(pid)
    storage.enqueue_missing(CONFIG)
    cloud = TOS()
    cloud.fail_after_put = True
    storage.execute(storage.claim(), CONFIG, lambda config: cloud)
    with Session.begin() as db:
        row = db.get(AssetBackup, asset_id)
        assert row.status == "failed" and row.attempts == 1
        assert row.next_run > now()
        row.next_run = now()
    assert "fixture-private-secret" not in caplog.text
    storage.execute(storage.claim(), CONFIG, lambda config: cloud)
    assert len(cloud.puts) == 1
    with Session() as db:
        assert db.get(AssetBackup, asset_id).status == "succeeded"
        assert all(job.status == "succeeded" for job in db.scalars(select(Job)))


def test_permission_failure_manual_retry_and_csrf(client, enabled, caplog):
    pid = project(client)
    asset_id = asset(pid)
    storage.enqueue_missing(CONFIG)
    class Denied(TOS):
        def head_object(self, bucket, key):
            raise CloudError(403)
    cloud = Denied()
    storage.execute(storage.claim(), CONFIG, lambda config: cloud)
    info = client.get(f"/api/assets/{asset_id}/info").json()["backup"]
    assert info["status"] == "failed" and "AccessDenied" in info["error"]
    assert not cloud.puts and "fixture-private-secret" not in caplog.text + info["error"]
    csrf = client.headers.pop("X-CSRF-Token")
    assert client.post(f"/api/assets/{asset_id}/backup/retry", json={}).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    response = client.post(f"/api/assets/{asset_id}/backup/retry", json={})
    assert response.status_code == 200 and response.json()["status"] == "queued"
    assert response.json()["error"] is None
    assert client.post("/api/assets/missing/backup/retry", json={}).status_code == 404


def test_parallel_claim_restart_and_stale_upload_fencing(client, enabled):
    pid = project(client)
    ids = [asset(pid), asset(pid)]
    storage.enqueue_missing(CONFIG)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(lambda _: storage.claim(), range(2)))
    assert {row.asset_id for row in claimed} == set(ids)
    old = claimed[0]
    with Session.begin() as db:
        db.get(AssetBackup, old.asset_id).lease_until = now() - timedelta(seconds=1)
    replacement = storage.claim()
    assert replacement.asset_id == old.asset_id and replacement.owner != old.owner
    cloud = TOS()
    storage.execute(old, CONFIG, lambda config: cloud)
    with Session() as db:
        current = db.get(AssetBackup, old.asset_id)
        assert current.status == "uploading" and current.owner == replacement.owner
    storage.execute(replacement, CONFIG, lambda config: cloud)
    assert len(cloud.puts) == 1


def test_missing_file_and_failed_remote_validation(client, enabled):
    pid = project(client)
    asset_id = asset(pid)
    storage.enqueue_missing(CONFIG)
    with Session() as db:
        path_for(db.get(Asset, asset_id).path).unlink()
    cloud = TOS()
    storage.execute(storage.claim(), CONFIG, lambda config: cloud)
    with Session.begin() as db:
        row = db.get(AssetBackup, asset_id)
        assert row.status == "failed" and "文件缺失" in row.error
        path_for(db.get(Asset, asset_id).path).write_bytes(b"restored-fixture")
        row.next_run = now()
    class Corrupt(TOS):
        def head_object(self, bucket, key):
            result = super().head_object(bucket, key)
            result.content_length += 1
            return result
    storage.execute(storage.claim(), CONFIG, lambda config: Corrupt())
    with Session() as db:
        assert db.get(AssetBackup, asset_id).status == "failed"


def test_all_generated_assets_are_discovered_without_changing_jobs(client, clips, enabled):
    pid = project(client)
    story = generate_story(client, pid)
    client.post(f"/api/projects/{pid}/videos", json={})
    process_all(clips[0])
    first, second = client.get(f"/api/projects/{pid}").json()["shots"]
    frame = client.post(f"/api/projects/{pid}/shots/{second['id']}/reference-frame", json={
        "source_shot_id": first["id"], "source_asset_id": first["asset_id"],
    })
    assert frame.status_code == 200
    client.post(f"/api/projects/{pid}/compose", json={})
    process_all(clips[0])
    assert storage.enqueue_missing(CONFIG) == 4  # two videos, selected frame, final film
    cloud = TOS()
    for _ in range(4):
        storage.execute(storage.claim(), CONFIG, lambda config: cloud)
    project_info = client.get(f"/api/projects/{pid}").json()
    assert project_info["export_backup"]["status"] == "succeeded"
    assert all(shot["backup"]["status"] == "succeeded" for shot in project_info["shots"])
    assert all(job["status"] == "succeeded" for job in project_info["jobs"])


def test_role_credentials_https_and_local_disabled(monkeypatch, client):
    monkeypatch.setattr(storage, "runtime", lambda: {"storage": {"backend": "local"}})
    assert storage.settings() is None
    pid = project(client)
    asset_id = asset(pid)
    assert client.get(f"/api/assets/{asset_id}/info").json()["backup"]["status"] == "disabled"
    assert client.post(f"/api/assets/{asset_id}/backup/retry", json={}).status_code == 409
    captured = {}
    import tos
    monkeypatch.setattr(tos, "TosClientV2", lambda **kwargs: captured.update(kwargs))
    monkeypatch.setattr(storage, "secret", lambda name: "")
    storage.client_for(CONFIG)
    assert captured["endpoint"].startswith("https://") and captured["enable_crc"]
    provider = captured["credentials_provider"]
    assert provider._ecs_url.endswith("/fixture-role")
    with pytest.raises(ValueError):
        storage.client_for({**CONFIG, "endpoint": "http://tos-cn-beijing.volces.com"})


def test_version_five_upgrade_preserves_assets(client):
    pid = project(client)
    asset_id = asset(pid)
    with Session.begin() as db:
        db.execute(text("DROP TABLE asset_backups"))
        db.execute(text("DELETE FROM schema_version WHERE version=6"))
    try:
        migrate()
        migrate()
        with Session() as db:
            assert db.get(Asset, asset_id)
            assert db.execute(text("SELECT max(version) FROM schema_version")).scalar() == 6
    finally:
        migrate()
