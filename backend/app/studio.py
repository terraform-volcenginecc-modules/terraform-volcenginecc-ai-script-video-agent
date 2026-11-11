"""ComfyUI 节点的持久任务入口，复用原队列、恢复和私有 TOS 归档。"""
import json
import uuid

from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from . import api
from .config import models
from .db import AssetBackup, Job, Project, Session, Shot
from .schemas import ProjectInput, VideoSettings
from .storage import local_asset, media_ready


ARK_VIDEO_MODELS = (
    "doubao-seedance-2-0-260128",
    "doubao-seedance-2-0-fast-260128",
    "doubao-seedance-2-0-mini-260615",
)


class ArkVideoInput(BaseModel):
    model_config = {"extra": "forbid"}
    prompt: str = Field(min_length=1, max_length=6000)
    model: str
    duration: int = Field(default=4, ge=4, le=15)
    ratio: str = "16:9"
    resolution: str = "720p"
    generate_audio: bool = True
    watermark: bool = False
    generation_id: str = Field(default="1", min_length=1, max_length=100)


@api.app.get("/api/ark/video-models", dependencies=[Depends(api.authenticated)])
def ark_video_models():
    return {"models": list(ARK_VIDEO_MODELS), "ratios": list(api.SIZES)}


@api.app.post("/api/ark/videos", dependencies=[Depends(api.authenticated)])
def generate_ark_video(body: ArkVideoInput):
    if body.model not in ARK_VIDEO_MODELS or body.ratio not in api.SIZES or not body.prompt.strip():
        raise HTTPException(422, "请选择支持的 Seedance 模型、画幅并填写提示词")
    try:
        settings = VideoSettings(resolution=body.resolution, generate_audio=body.generate_audio,
                                 watermark=body.watermark).model_dump()
        api.validate_settings(body.model, settings, provider="ark")
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    identity = json.dumps(body.model_dump(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    project_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "ark-text-video:" + identity))
    job_id = str(uuid.uuid5(uuid.UUID(project_id), "video"))
    with Session.begin() as db:
        if db.bind.dialect.name == "postgresql":
            from sqlalchemy import text
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                       {"key": uuid.UUID(project_id).int % (2 ** 63 - 1)})
        existing = db.get(Job, job_id)
        if not existing:
            spec = {"duration": body.duration, "scene": "方舟文生视频", "characters": [],
                    "action": body.prompt[:2000], "camera": "按提示词生成"}
            project = Project(id=project_id, name="ComfyUI · " + body.generation_id[:85],
                              brief=body.prompt, duration=body.duration, ratio=body.ratio,
                              script_model=models()["script_model_id"], video_model=body.model,
                              video_settings=settings,
                              script={"title": "方舟文生视频", "synopsis": body.prompt[:3000],
                                      "style_bible": "按提示词生成", "characters": [], "shots": [spec]})
            db.add(project)
            db.flush()
            shot = Shot(id=str(uuid.uuid5(uuid.UUID(project_id), "shot")),
                        project_id=project_id, position=1, spec=spec, status="queued")
            db.add(shot)
            db.flush()
            db.add(Job(id=job_id, project_id=project_id, shot_id=shot.id, kind="video",
                       snapshot={**api.snapshot(project), "shot": spec, "prompt": body.prompt,
                                 "generation_config": {"mode": "text"}, "reference": None,
                                 "references": [], "video_provider": "ark", "direct_ark": True}))
    return ark_video_state(job_id)


def ark_video_job(db, job_id):
    job = db.get(Job, job_id)
    if not job or job.kind != "video" or not job.snapshot.get("direct_ark"):
        raise HTTPException(404, "方舟视频任务不存在")
    return job


@api.app.get("/api/ark/videos/{job_id}", dependencies=[Depends(api.authenticated)])
def ark_video_state(job_id: str):
    with Session() as db:
        job = ark_video_job(db, job_id)
        data = api.job_data(job)
        if job.status == "succeeded":
            asset_id = job.snapshot.get("output_asset_id")
            data["asset_id"] = asset_id
            if not media_ready(db, asset_id):
                backup = db.get(AssetBackup, asset_id)
                data["status"] = "upload_failed" if backup and backup.status == "failed" else "uploading"
                data["error"] = backup.error if backup and backup.status == "failed" else None
        return data


@api.app.get("/api/ark/videos/{job_id}/content", dependencies=[Depends(api.authenticated)])
def ark_video_content(job_id: str):
    with Session() as db:
        job = ark_video_job(db, job_id)
        asset_id = job.snapshot.get("output_asset_id")
        if job.status != "succeeded" or not media_ready(db, asset_id):
            raise HTTPException(409, "视频尚未完成生成和 TOS 归档")
        asset = db.get(api.Asset, asset_id)
        try:
            path = local_asset(db, asset)
        except (ValueError, OSError) as error:
            raise HTTPException(409, str(error)) from None
        from .media import digest
        return FileResponse(path, media_type="video/mp4",
                            headers={"X-Content-SHA256": digest(path)})


class NativeProject(BaseModel):
    项目名称: str = Field(min_length=1, max_length=100)
    创作轮次: str = Field(min_length=1, max_length=100)
    创作要求: str = Field(min_length=5, max_length=6000)
    风格: str = Field(default="", max_length=2000)
    角色: str = Field(default="", max_length=3000)
    目标时长: int = Field(default=30, ge=4, le=180)
    画幅: str = "16:9"
    已有项目编号: str = Field(default="", max_length=36)


@api.app.post("/api/native/projects", dependencies=[Depends(api.authenticated)])
def project(body: NativeProject):
    if body.已有项目编号:
        with Session() as db:
            api.project_locked(db, body.已有项目编号)
            return {"id": body.已有项目编号}
    config = models()
    try:
        values = ProjectInput(name=body.项目名称, brief=body.创作要求, style=body.风格,
                              characters=body.角色, duration=body.目标时长, ratio=body.画幅,
                              script_model=config["script_model_id"], video_model=config["video_model_id"])
        api.validate_settings(values.video_model, values.video_settings.model_dump())
    except ValueError:
        raise HTTPException(422, "项目画幅、模型或视频规格无效，请检查节点输入") from None
    project_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"animation:{body.项目名称}:{body.创作轮次}"))
    with Session.begin() as db:
        # PostgreSQL 上先串行同一项目的首次创建，避免并发排队重复生成。
        if db.bind.dialect.name == "postgresql":
            from sqlalchemy import text
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                       {"key": uuid.UUID(project_id).int % (2 ** 63 - 1)})
        existing = db.get(Project, project_id)
        expected = values.model_dump()
        if existing:
            if existing.archived or any(getattr(existing, key) != value for key, value in expected.items()):
                raise HTTPException(409, "此轮次已存在且参数不同，请更换创作轮次；原任务不会自动重提")
        else:
            db.add(Project(id=project_id, **expected))
        return {"id": project_id}


def state(project_id, stage):
    if stage not in ("script", "video", "compose"):
        raise HTTPException(404, "阶段不存在")
    with Session() as db:
        project = api.project_locked(db, project_id)
        jobs = list(db.scalars(select(Job).where(Job.project_id == project_id, Job.kind == stage)
                               .order_by(Job.created_at.desc())))
        if stage == "video":
            shots = list(db.scalars(select(Shot).where(Shot.project_id == project_id).order_by(Shot.position)))
            latest = {}
            for job in jobs:
                latest.setdefault(job.shot_id, job)
            jobs = list(latest.values())
            assets = [shot.asset_id for shot in shots]
            complete = bool(shots) and all(shot.status == "succeeded" and shot.asset_id for shot in shots)
        else:
            jobs = jobs[:1]
            assets = [project.export_asset] if stage == "compose" else []
            complete = bool(project.export_asset) if stage == "compose" else bool(project.script)
        blocked = next((job for job in jobs if job.status in ("failed", "uncertain", "cancelled")), None)
        if blocked:
            return {"status": blocked.status, "error": blocked.error or "请通过业务任务接口恢复原任务",
                    "job_id": blocked.id}
        if complete and all(media_ready(db, asset) for asset in assets):
            return {"status": "succeeded", "project_id": project_id, "assets": assets,
                    "script": project.script if stage == "script" else None}
        failed_upload = next((row for asset in assets if asset
                              and (row := db.get(AssetBackup, asset)) and row.status == "failed"), None)
        if failed_upload:
            return {"status": "failed", "error": "TOS 上传失败，请只重试上传", "asset_id": failed_upload.asset_id}
        return {"status": "waiting" if complete or jobs else "missing", "project_id": project_id}


@api.app.get("/api/native/projects/{project_id}/stages/{stage}", dependencies=[Depends(api.authenticated)])
def get_stage(project_id: str, stage: str):
    return state(project_id, stage)


@api.app.post("/api/native/projects/{project_id}/stages/{stage}", dependencies=[Depends(api.authenticated)])
def start_stage(project_id: str, stage: str):
    current = state(project_id, stage)
    if current["status"] != "missing":
        return current
    with Session.begin() as db:
        project = api.project_locked(db, project_id)
        api.idle(db, project_id)
        # 在项目锁内再次查询，确保重复节点执行仍复用原任务。
        if db.scalar(select(Job.id).where(Job.project_id == project_id, Job.kind == stage).limit(1)):
            return {"status": "waiting", "project_id": project_id}
        if stage == "script":
            db.add(Job(project_id=project_id, kind="script", snapshot=api.snapshot(project)))
        elif stage == "video":
            shots = list(db.scalars(select(Shot).where(Shot.project_id == project_id).order_by(Shot.position)))
            if not shots:
                raise HTTPException(409, "请先生成并审阅剧本")
            for shot in shots:
                if api.needs_generation(db, shot, project):
                    api.video_job(db, project, shot)
        else:
            api.queue_composition(db, project)
    return {"status": "waiting", "project_id": project_id}
