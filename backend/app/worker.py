import logging
import random
import threading
import time
from datetime import timedelta

from pathlib import Path

from sqlalchemy import select, update

from .config import POLL_SECONDS, validate_video_config
from .db import Asset, Job, Project, Session, Shot, migrate, now, uid
from .job_policy import SCRIPT_MAX_RETRIES, script_deadline, timeout_script
from .media import compose, download, image_info, path_for, probe, reference_frame
from .provider import ProviderError, script_provider, video_provider
from .schemas import Script
from .storage import local_asset

log = logging.getLogger("worker")
LEASE_SECONDS = 60


def claim():
    with Session.begin() as db:
        # Includes tasks from older releases and tasks left running across a restart.
        for script in db.scalars(select(Job).where(
            Job.kind == "script", Job.status.in_(["queued", "waiting", "running"])
        ).with_for_update(skip_locked=True)):
            if script_deadline(script) <= now():
                timeout_script(script)
            elif script.status in ("queued", "waiting") and script.attempts > SCRIPT_MAX_RETRIES:
                script.status = "failed"
                script.owner = None
                script.lease_until = None
        db.flush()
        expired = list(db.scalars(select(Job).where(
            Job.status == "running", Job.lease_until < now()).with_for_update(skip_locked=True)))
        for job in expired:
            if job.kind == "video" and job.phase == "submitting" and not job.external_id:
                job.status = "uncertain"
                job.error = "Worker 在提交期间中断，请先到对应视频服务核对任务 ID；不会自动重提"
                job.error_details = None
            else:
                job.status = "queued"
            job.owner = None
            job.lease_until = None
        db.flush()
        job = db.scalar(select(Job).where(
            Job.status.in_(["queued", "waiting"]), Job.next_run <= now()
        ).order_by(Job.next_run, Job.created_at).with_for_update(skip_locked=True).limit(1))
        if not job:
            return None
        job.owner = uid()
        job.status = "running"
        job.lease_until = now() + timedelta(seconds=LEASE_SECONDS)
        return job


class LostLease(Exception):
    pass


def owned(db, job):
    row = db.scalar(select(Job).where(Job.id == job.id).with_for_update())
    if not row or row.owner != job.owner or row.status != "running" or not row.lease_until or row.lease_until <= now():
        raise LostLease()
    return row


def save(job, fn):
    with Session.begin() as db:
        # Same lock order as API mutations.
        db.scalar(select(Project).where(Project.id == job.project_id).with_for_update())
        row = owned(db, job)
        fn(db, row)


def heartbeat(job, stopped):
    while not stopped.wait(15):
        try:
            with Session.begin() as db:
                values = {"lease_until": now() + timedelta(seconds=LEASE_SECONDS)}
                expired = job.kind == "script" and script_deadline(job) <= now()
                if expired:
                    values = {"status": "failed", "phase": "timeout",
                              "error": "剧本任务超过 10 分钟总时限，已停止重试；请调整设置后重新生成",
                              "error_details": None,
                              "owner": None, "lease_until": None}
                result = db.execute(update(Job).where(
                    Job.id == job.id, Job.owner == job.owner, Job.status == "running",
                    Job.lease_until > now()
                ).values(**values))
            if expired or not result.rowcount:
                return
            Path("/tmp/worker-heartbeat").touch()
        except Exception:
            log.warning("heartbeat failed for job %s", job.id)


def finish(row):
    row.status = "succeeded"
    row.phase = "done"
    row.error = None
    row.error_details = None
    row.owner = None
    row.lease_until = None


def wait(row, seconds):
    row.status = "waiting"
    row.next_run = now() + timedelta(seconds=seconds)
    row.owner = None
    row.lease_until = None


def execute(job, provider_factory=None, downloader=None):
    provider = None
    stopped = threading.Event()
    thread = threading.Thread(target=heartbeat, args=(job, stopped), daemon=True)
    thread.start()
    try:
        if job.kind == "compose":
            with Session() as db:
                sources = [local_asset(db, db.get(Asset, asset_id)) for asset_id in job.snapshot["assets"]]
            relative = f"{job.project_id}/exports/{job.id}/{job.owner}/film.mp4"
            target = path_for(relative)
            settings = job.snapshot.get("video_settings", {})
            compose(sources, target, job.snapshot["ratio"], job.snapshot.get("cuts"),
                    settings.get("resolution", "720p"), settings.get("generate_audio", True))
            duration, metadata = probe(target)

            def composed(db, row):
                asset = Asset(project_id=row.project_id, path=relative, duration=duration, metadata_json=metadata)
                db.add(asset)
                db.flush()
                db.get(Project, row.project_id).export_asset = asset.id
                finish(row)
            save(job, composed)
            return

        # Construct provider before persisting submitting: missing key cannot incur a charge.
        provider = (provider_factory() if provider_factory else
                    script_provider() if job.kind == "script" else video_provider(job.snapshot))
        if job.kind == "script":
            script = Script.model_validate(provider.script({
                **job.snapshot, "_script_deadline": script_deadline(job).isoformat()
            })).model_dump()

            def scripted(db, row):
                if script_deadline(row) <= now():
                    timeout_script(row)
                    return
                # 保护旧版本遗留或异常恢复的活跃视频任务，不移除其分镜关联。
                if db.scalar(select(Job.id).where(
                    Job.project_id == row.project_id, Job.id != row.id,
                    Job.shot_id.is_not(None),
                    Job.status.in_(["queued", "running", "waiting", "uncertain"]),
                ).limit(1)):
                    raise ValueError("项目仍有未完成的镜头任务，请先处理后再生成剧本")
                # Keep old assets for historical downloads; replace the active storyboard.
                db.execute(update(Job).where(Job.project_id == row.project_id, Job.id != row.id,
                                             Job.shot_id.is_not(None)).values(shot_id=None))
                for old in db.scalars(select(Shot).where(Shot.project_id == row.project_id)):
                    db.delete(old)
                db.flush()
                project = db.get(Project, row.project_id)
                project.script = script
                project.export_asset = None
                for index, spec in enumerate(script["shots"]):
                    db.add(Shot(project_id=row.project_id, position=index + 1, spec=spec))
                finish(row)
            save(job, scripted)
        elif job.kind == "video":
            if not job.external_id:
                if job.phase == "submitting":
                    raise ProviderError("视频提交结果需要人工核对", uncertain=True)
                save(job, lambda db, row: setattr(row, "phase", "submitting"))
                nonce = job.snapshot.get("comfyui_submission_nonce", "0")
                external_id = provider.submit({**job.snapshot, "submission_key": f"{job.id}-{nonce}-{job.attempts}"})

                def submitted(db, row):
                    row.external_id = external_id
                    if getattr(provider, "job_url", None):
                        row.snapshot = {**row.snapshot, "comfyui_job_url": provider.job_url}
                    row.phase = "polling"
                    wait(row, POLL_SECONDS)
                save(job, submitted)
                return
            result = provider.poll(job.external_id)
            if result is None:
                save(job, lambda db, row: wait(row, POLL_SECONDS))
                return
            url = result["video_url"] if isinstance(result, dict) else result
            relative = f"{job.project_id}/shots/{job.id}/{job.owner}/video.mp4"
            target = path_for(relative)
            (downloader or getattr(provider, "download", download))(url, target)
            duration, metadata = probe(target)
            metadata["generation_config"] = job.snapshot.get("generation_config", {"mode": "text"})
            metadata["video_model"] = job.snapshot["video_model"]
            metadata["video_provider"] = job.snapshot.get("video_provider", "ark")
            metadata["ratio"] = job.snapshot["ratio"]
            metadata["video_settings"] = job.snapshot.get("video_settings", {
                "resolution": "720p", "generate_audio": True, "watermark": False})
            tail_relative = None
            tail_metadata = None
            if isinstance(result, dict):
                metadata["usage"] = result.get("usage", {})
                metadata["model"] = result.get("model")
                tail_relative = str(Path(relative).with_name("last-frame.png"))
                tail_path = path_for(tail_relative)
                if result.get("last_frame_url"):
                    download(result["last_frame_url"], tail_path, image=True)
                    tail_metadata = image_info(tail_path)
                    tail_metadata["origin"] = "model_last_frame"
                else:
                    tail_metadata, _ = reference_frame(target, tail_path, job.snapshot["ratio"])
                    tail_metadata["origin"] = "decoded_last_frame"

            def archived(db, row):
                asset = Asset(project_id=row.project_id, path=relative, duration=duration, metadata_json=metadata)
                db.add(asset)
                db.flush()
                if tail_relative:
                    tail = Asset(project_id=row.project_id, kind="image", path=tail_relative,
                                 duration=None, metadata_json={**tail_metadata, "source_asset_id": asset.id,
                                                              "source_job_id": row.id})
                    db.add(tail)
                    db.flush()
                    asset.metadata_json = {**metadata, "last_frame_asset_id": tail.id}
                shot = db.get(Shot, row.shot_id)
                shot.asset_id = asset.id
                shot.status = "succeeded"
                shot.edit_config = {}
                finish(row)
            save(job, archived)
        else:
            raise ValueError("未知任务类型")
    except LostLease:
        log.warning("lease lost for job %s; stale output ignored", job.id)
    except Exception as error:
        def failed(db, row):
            row.attempts += 1
            if row.kind == "script" and script_deadline(row) <= now():
                timeout_script(row)
                return
            # Any unexpected failure after POST started must also be treated as uncertain.
            uncertain = row.kind == "video" and row.phase == "submitting" and not row.external_id
            if isinstance(error, ProviderError):
                uncertain = error.uncertain
                if row.phase == "submitting" and not uncertain:
                    row.phase = "pending"
                if error.terminal:
                    row.phase = "rejected"
                row.error = str(error)
                row.error_details = error.details
                retry_limit = SCRIPT_MAX_RETRIES if row.kind == "script" else 8
                if error.retryable and row.attempts <= retry_limit and not uncertain:
                    wait(row, min(300, 2 ** row.attempts + random.randrange(5)))
                    return
            else:
                row.error = "本地处理失败，请检查 Worker、素材与磁盘后恢复任务"
                row.error_details = None
            row.status = "uncertain" if uncertain else "failed"
            row.owner = None
            row.lease_until = None
            if row.shot_id:
                db.get(Shot, row.shot_id).status = row.status
        try:
            save(job, failed)
        except LostLease:
            pass
        log.warning("job %s did not complete (%s)", job.id, type(error).__name__)
        if isinstance(error, ProviderError) and error.details:
            log.warning("job %s provider diagnostics: %s", job.id, error.details)
    finally:
        stopped.set()
        thread.join(timeout=2)
        if provider:
            provider.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    validate_video_config()
    migrate()
    from .storage import main as backup_loop
    backup_stopped = threading.Event()
    threading.Thread(target=backup_loop, args=(backup_stopped,), daemon=True).start()
    Path("/tmp/worker-heartbeat").touch()
    log.info("Worker 已就绪，使用 PostgreSQL 队列和已配置的视频 Provider")
    while True:
        try:
            job = claim()
            if job:
                execute(job)
            else:
                time.sleep(1)
            Path("/tmp/worker-heartbeat").touch()
        except Exception as error:
            log.warning("queue unavailable (%s)", type(error).__name__)
            time.sleep(3)


if __name__ == "__main__":
    main()
