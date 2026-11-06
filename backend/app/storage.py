"""Durable background TOS backup, independent of paid model jobs."""
import logging
import threading
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from .config import runtime, secret
from .db import Asset, AssetBackup, Session, now, uid
from .diagnostics import identifier
from .media import digest, path_for

log = logging.getLogger("storage")
LEASE_SECONDS = 180


def settings():
    config = runtime()
    storage = config.get("storage", {})
    if not storage.get("auto_upload", False):
        return None
    region = storage.get("region") or config.get("infrastructure", {}).get("region")
    endpoint = storage.get("intranet_endpoint") or storage.get("extranet_endpoint")
    if not endpoint and region:
        endpoint = f"tos-{region}.volces.com"
    return {
        "bucket": storage.get("bucket"), "region": region, "endpoint": endpoint,
        "role_name": config.get("infrastructure", {}).get("role_name")
        or config.get("infrastructure", {}).get("role_trn", "").rsplit("/", 1)[-1],
    }


def client_for(config):
    import tos
    from tos.credential import EcsCredentialsProvider
    if not config["bucket"] or not config["region"] or not config["endpoint"]:
        raise ValueError("TOS 配置缺少桶名、地域或地址")
    endpoint = config["endpoint"]
    if "://" not in endpoint:
        endpoint = "https://" + endpoint
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query:
        raise ValueError("TOS 地址须使用 HTTPS")
    options = dict(endpoint=endpoint, region=config["region"], connection_time=10,
                   socket_timeout=60, request_timeout=120, max_retry_count=2,
                   enable_crc=True, follow_redirect_times=0)
    ak, sk = secret("tos_access_key"), secret("tos_secret_key")
    if ak and sk:
        return tos.TosClientV2(ak=ak, sk=sk, security_token=secret("tos_session_token") or None, **options)
    if not config["role_name"]:
        raise ValueError("未配置 TOS 实例角色或访问密钥文件")
    return tos.TosClientV2(credentials_provider=EcsCredentialsProvider(config["role_name"]), **options)


def key_for(asset):
    mime = asset.metadata_json.get("mime_type", "image/png") if asset.kind == "image" else "video/mp4"
    filename = "frame.jpg" if mime == "image/jpeg" else "frame.png" if asset.kind == "image" else "video.mp4"
    return f"media/{asset.project_id}/{asset.id}/{filename}"


def enqueue_missing(config):
    # Scan committed assets only. Concurrent API/Worker creation is safe and
    # a restart discovers assets created before a previous scan or deployment.
    with Session.begin() as db:
        assets = list(db.scalars(select(Asset).outerjoin(AssetBackup).where(
            AssetBackup.asset_id.is_(None)).order_by(Asset.id).limit(100)))
        for asset in assets:
            db.execute(insert(AssetBackup).values(
                asset_id=asset.id, bucket=config["bucket"] or "", object_key=key_for(asset),
                status="queued", result={}, attempts=0, next_run=now(),
            ).on_conflict_do_nothing(index_elements=["asset_id"]))
    return len(assets)


def claim():
    with Session.begin() as db:
        db.execute(update(AssetBackup).where(
            AssetBackup.status == "uploading", AssetBackup.lease_until < now()
        ).values(status="queued", owner=None, lease_until=None))
        row = db.scalar(select(AssetBackup).where(
            AssetBackup.status.in_(["queued", "failed"]), AssetBackup.next_run <= now()
        ).order_by(AssetBackup.next_run).with_for_update(skip_locked=True).limit(1))
        if not row:
            return None
        row.status = "uploading"
        row.owner = uid()
        row.lease_until = now() + timedelta(seconds=LEASE_SECONDS)
        return row


def heartbeat(backup, stopped):
    while not stopped.wait(30):
        try:
            with Session.begin() as db:
                renewed = db.execute(update(AssetBackup).where(
                    AssetBackup.asset_id == backup.asset_id, AssetBackup.owner == backup.owner,
                    AssetBackup.status == "uploading", AssetBackup.lease_until > now()
                ).values(lease_until=now() + timedelta(seconds=LEASE_SECONDS)))
            if not renewed.rowcount:
                return
        except Exception as error:
            log.warning("backup heartbeat failed (%s)", type(error).__name__)


def upload_file(client, bucket, key, path, mime, asset_id):
    from tos.enum import ACLType
    if not path.is_file():
        raise FileNotFoundError("素材文件不存在")
    sha256, size = digest(path), path.stat().st_size
    try:
        head = client.head_object(bucket, key)
    except Exception as error:
        # Only a confirmed missing object permits PUT; auth/network failures
        # must not be mistaken for a missing object.
        if getattr(error, "status_code", None) != 404:
            raise
    else:
        if head.meta.get("sha256") == sha256 and head.content_length == size:
            return {"key": key, "sha256": sha256, "size": size, "etag": head.etag}
    client.put_object_from_file(
        bucket, key, str(path), content_type=mime, content_length=size,
        acl=ACLType.ACL_Private, server_side_encryption="AES256",
        meta={"sha256": sha256, "asset-id": asset_id},
    )
    head = client.head_object(bucket, key)
    if head.meta.get("sha256") != sha256 or head.content_length != size:
        raise ValueError("TOS 上传校验失败")
    return {"key": key, "sha256": sha256, "size": size, "etag": head.etag}


def execute(backup, config, client_factory=client_for):
    stopped = threading.Event()
    thread = threading.Thread(target=heartbeat, args=(backup, stopped), daemon=True)
    thread.start()
    client = None
    try:
        with Session() as db:
            current = db.get(AssetBackup, backup.asset_id)
            if (not current or current.owner != backup.owner or current.status != "uploading"
                    or not current.lease_until or current.lease_until <= now()):
                return
            asset = db.get(Asset, backup.asset_id)
        client = client_factory(config)
        mime = asset.metadata_json.get("mime_type", "image/png") if asset.kind == "image" else "video/mp4"
        result = upload_file(client, backup.bucket, backup.object_key, path_for(asset.path), mime, asset.id)
        # A selected reference frame can also have a normalized source video
        # needed for exact frame preview. Back it up alongside the frame.
        if asset.metadata_json.get("source_preview_path"):
            preview_key = str(Path(backup.object_key).with_name("source-preview.mp4"))
            result["source_preview"] = upload_file(
                client, backup.bucket, preview_key,
                path_for(asset.metadata_json["source_preview_path"]), "video/mp4", asset.id,
            )
        result["uploaded_at"] = now().isoformat()
        with Session.begin() as db:
            db.execute(update(AssetBackup).where(
                AssetBackup.asset_id == backup.asset_id, AssetBackup.owner == backup.owner,
                AssetBackup.status == "uploading", AssetBackup.lease_until > now(),
            ).values(status="succeeded", result=result, error=None, owner=None, lease_until=None))
        log.info("asset %s TOS backup complete", backup.asset_id)
    except Exception as error:
        code = identifier(getattr(error, "code", None))
        request_id = identifier(getattr(error, "request_id", None))
        # SDK exception text may contain signed requests or credentials.
        message = ("本地素材文件缺失，无法备份" if isinstance(error, FileNotFoundError)
                   else "TOS 上传失败，请检查桶配置、实例角色权限和网络；后台会自动重试")
        fields = " · ".join(x for x in (code, request_id) if x)
        if fields:
            message += " · " + fields
        with Session.begin() as db:
            row = db.scalar(select(AssetBackup).where(
                AssetBackup.asset_id == backup.asset_id, AssetBackup.owner == backup.owner,
                AssetBackup.status == "uploading", AssetBackup.lease_until > now(),
            ).with_for_update())
            if row:
                row.attempts += 1
                row.status = "failed"
                row.error = message
                row.next_run = now() + timedelta(seconds=min(1800, 30 * 2 ** min(row.attempts - 1, 6)))
                row.owner = None
                row.lease_until = None
        log.warning("asset %s backup failed (%s), code=%s request_id=%s",
                    backup.asset_id, type(error).__name__, code, request_id)
    finally:
        stopped.set()
        thread.join(timeout=2)
        if client:
            try:
                client.close()
            except Exception:
                log.warning("backup client close failed")


def backup_data(db, asset_id):
    config = settings()
    if not asset_id:
        return None
    row = db.get(AssetBackup, asset_id)
    if not row:
        return {"status": "queued" if config else "disabled"}
    return {
        "status": row.status, "bucket": row.bucket, "object_key": row.object_key,
        "error": row.error, "attempts": row.attempts, "next_run": row.next_run,
        **row.result,
    }


def main(stopped):
    while not stopped.is_set():
        try:
            config = settings()
            if config:
                enqueue_missing(config)
                backup = claim()
                if backup:
                    execute(backup, config)
                    continue
        except Exception as error:
            log.warning("backup queue unavailable (%s)", type(error).__name__)
        stopped.wait(2)
