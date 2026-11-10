"""ComfyUI 节点的持久任务入口，复用原队列、恢复和私有 TOS 归档。"""
import uuid

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from . import api
from .config import models
from .db import AssetBackup, Job, Project, Session, Shot
from .schemas import ProjectInput
from .storage import media_ready


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
            return {"status": blocked.status, "error": blocked.error or "请在任务面板恢复原任务",
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
