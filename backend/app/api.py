import hmac
import secrets
import time
import base64
import binascii
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, or_, select, text

from .config import COOKIE_SECURE, ark_key, models, secret, model_catalog, video_provider, comfyui_config, validate_video_config
from .db import Asset, Job, Project, Session, Shot, migrate, now, uid
from .job_policy import SCRIPT_MAX_RETRIES, SCRIPT_TIMEOUT_SECONDS, script_deadline
from .media import path_for, reference_frame, SIZES
from .storage import access_data, backup_data, local_asset, media_ready, settings as storage_settings
from .db import AssetBackup
from .continuity import generation_config, needs_generation, reference_current, reference_snapshot, references_snapshot
from .schemas import GenerationInput, ImageUpload, ProjectInput, RecoveryInput, ReferenceFrameInput, Script, ShotSpec
from .generation import build_prompt, validate_settings, video_settings, output_settings

ACTIVE = ["queued", "running", "waiting", "uncertain"]
login_attempts: dict[str, list[float]] = {}


@asynccontextmanager
async def lifespan(app):
    if not secret("owner_password") or not secret("session_key"):
        raise RuntimeError("缺少工作室密码或会话密钥，请检查服务端凭据文件")
    validate_video_config()
    migrate()
    yield


app = FastAPI(title="火山 · ComfyUI 业务适配", lifespan=lifespan, docs_url=None, redoc_url=None)


def serializer():
    return URLSafeTimedSerializer(secret("session_key"), salt="animation-owner-v1")


def authenticated(request: Request):
    try:
        data = serializer().loads(request.cookies.get("session", ""), max_age=86400)
    except (BadSignature, SignatureExpired):
        raise HTTPException(401, "请先登录")
    if request.method not in ("GET", "HEAD"):
        if not hmac.compare_digest(str(data["csrf"]), request.headers.get("x-csrf-token", "")):
            raise HTTPException(403, "登录状态已失效，请刷新页面")
    return data


@app.middleware("http")
async def security_headers(request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return Response(status_code=403)
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            return Response(status_code=415)
        limit = 41 * 1024 * 1024 if request.url.path.endswith("/reference-images") else 1024 * 1024
        size = 0
        chunks = []
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                return Response(status_code=413)
            chunks.append(chunk)
        # Cache the bounded body for FastAPI's subsequent JSON parsing.
        request._body = b"".join(chunks)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


class Login(BaseModel):
    password: str = Field(max_length=1000)


@app.post("/api/login")
def login(body: Login, request: Request, response: Response):
    address = request.client.host if request.client else "local"
    recent = [t for t in login_attempts.get(address, []) if t > time.monotonic() - 60]
    if len(recent) >= 10:
        raise HTTPException(429, "登录尝试过多，请一分钟后重试")
    recent.append(time.monotonic())
    login_attempts[address] = recent
    if not hmac.compare_digest(body.password.encode(), secret("owner_password").encode()):
        raise HTTPException(401, "密码不正确")
    csrf = secrets.token_urlsafe(32)
    response.set_cookie("session", serializer().dumps({"csrf": csrf}), httponly=True,
                        secure=COOKIE_SECURE or request.url.scheme == "https", samesite="strict", max_age=86400)
    return {"csrf": csrf}


@app.get("/api/session")
def session(data=Depends(authenticated)):
    return data


@app.post("/api/logout")
def logout(response: Response, data=Depends(authenticated)):
    response.delete_cookie("session")
    return {"ok": True}


@app.get("/api/health")
def health():
    with Session() as db:
        db.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/api/config", dependencies=[Depends(authenticated)])
def configuration():
    config = models()
    return {"script_model": config["script_model_id"], "video_model": config["video_model_id"],
            "provider": "方舟 · 剧本 / ComfyUI · 视频" if video_provider() == "comfyui" else "方舟 · 真实模型接口",
            "video_provider": video_provider(),
            "storage": "私有 TOS" if storage_settings() else "本地持久化",
            "model_catalog": model_catalog(), "ratios": list(SIZES),
            "key_configured": bool(ark_key())}


def project_locked(db, project_id, allow_archived=False):
    project = db.scalar(select(Project).where(Project.id == project_id).with_for_update())
    if not project:
        raise HTTPException(404, "项目不存在")
    if project.archived and not allow_archived:
        raise HTTPException(409, "项目已归档，请先恢复项目")
    return project


def idle(db, project_id):
    if db.scalar(select(Job.id).where(Job.project_id == project_id, Job.status.in_(ACTIVE)).limit(1)):
        raise HTTPException(409, "项目仍有执行中或待核对的任务，请先处理任务")


def shot_locked(db, project_id, shot_id):
    shot = db.get(Shot, shot_id)
    if not shot or shot.project_id != project_id:
        raise HTTPException(404, "镜头不存在")
    return shot


def job_data(job):
    data = {k: getattr(job, k) for k in (
        "id", "project_id", "shot_id", "kind", "status", "phase", "external_id", "error", "error_details", "attempts", "created_at",
        "next_run")}
    if job.kind == "script":
        data.update(max_retries=SCRIPT_MAX_RETRIES, timeout_seconds=SCRIPT_TIMEOUT_SECONDS,
                    deadline_at=script_deadline(job))
    return data


def snapshot(project):
    return {"script_model": project.script_model, "video_model": project.video_model, "ratio": project.ratio,
            "video_settings": video_settings(project),
            "project": {k: getattr(project, k) for k in ("name", "brief", "style", "characters", "duration", "ratio")},
            "script": project.script}


@app.get("/api/projects", dependencies=[Depends(authenticated)])
def list_projects(include_archived: bool = False):
    with Session() as db:
        counts = dict(db.execute(select(Job.project_id, func.count(Job.id)).where(
            Job.status.in_(ACTIVE)).group_by(Job.project_id)).all())
        query = select(Project).order_by(Project.created_at.desc(), Project.id)
        if not include_archived:
            query = query.where(Project.archived.is_(False))
        return [{"id": p.id, "name": p.name, "created_at": p.created_at, "archived": p.archived,
                 "ratio": p.ratio, "duration": p.duration, "video_model": p.video_model,
                 "active_jobs": counts.get(p.id, 0)}
                for p in db.scalars(query)]


@app.post("/api/projects", dependencies=[Depends(authenticated)])
def create_project(body: ProjectInput):
    try:
        validate_settings(body.video_model, body.video_settings.model_dump())
    except ValueError as error:
        raise HTTPException(422, str(error))
    with Session.begin() as db:
        project = Project(**body.model_dump())
        db.add(project)
        db.flush()
        return {"id": project.id}


class ArchiveInput(BaseModel):
    archived: bool


@app.put("/api/projects/{project_id}/archive", dependencies=[Depends(authenticated)])
def archive_project(project_id: str, body: ArchiveInput):
    with Session.begin() as db:
        project = project_locked(db, project_id, allow_archived=True)
        idle(db, project_id)
        project.archived = body.archived
        return {"ok": True}


@app.get("/api/projects/{project_id}", dependencies=[Depends(authenticated)])
def get_project(project_id: str):
    with Session() as db:
        project = db.get(Project, project_id)
        if not project:
            raise HTTPException(404, "项目不存在")
        data = {k: getattr(project, k) for k in (
            "id", "name", "brief", "style", "characters", "duration", "ratio", "script_model", "video_model", "script", "export_asset", "archived")}
        data["video_settings"] = video_settings(project)
        data["export_backup"] = backup_data(db, project.export_asset)
        data["export_ready"] = media_ready(db, project.export_asset)
        active_shots = {j.shot_id: j.status for j in db.scalars(
            select(Job).where(Job.project_id == project_id, Job.status.in_(ACTIVE)).order_by(Job.created_at))}
        data["shots"] = []
        for s in db.scalars(select(Shot).where(Shot.project_id == project_id).order_by(Shot.position)):
            ref = db.get(Asset, s.generation_config.get("reference_asset_id")) if s.generation_config.get("reference_asset_id") else None
            data["shots"].append({
                "id": s.id, "position": s.position, "spec": s.spec,
                "status": active_shots.get(s.id, s.status), "asset_id": s.asset_id,
                "backup": backup_data(db, s.asset_id),
                "media_ready": media_ready(db, s.asset_id),
                "generation_config": generation_config(s), "edit_config": s.edit_config,
                "reference_stale": bool(s.generation_config.get("mode") in ("first_frame", "first_last_frame") and
                                        (not ref or not reference_current(db, ref) or not reference_ratio_matches(ref, project.ratio))),
                "needs_regeneration": needs_generation(db, s, project),
            })
        data["jobs"] = [job_data(j) for j in db.scalars(
            select(Job).where(Job.project_id == project_id).order_by(Job.created_at.desc()).limit(100))]
        return data


@app.put("/api/projects/{project_id}", dependencies=[Depends(authenticated)])
def update_project(project_id: str, body: ProjectInput):
    try:
        validate_settings(body.video_model, body.video_settings.model_dump())
    except ValueError as error:
        raise HTTPException(422, str(error))
    with Session.begin() as db:
        project = project_locked(db, project_id)
        idle(db, project_id)
        if (project.ratio != body.ratio or project.video_model != body.video_model
                or output_settings(video_settings(project)) != output_settings(body.video_settings.model_dump())):
            project.export_asset = None
        duration = sum(s.spec["duration"] for s in db.scalars(select(Shot).where(Shot.project_id == project_id)))
        if duration > body.duration:
            raise HTTPException(422, "目标时长不能小于现有分镜总时长")
        for key, value in body.model_dump().items():
            setattr(project, key, value)
        return {"ok": True}


@app.post("/api/projects/{project_id}/script", dependencies=[Depends(authenticated)])
def generate_script(project_id: str):
    with Session.begin() as db:
        project = project_locked(db, project_id)
        idle(db, project_id)
        job = Job(project_id=project_id, kind="script", snapshot=snapshot(project))
        db.add(job)
        db.flush()
        return job_data(job)


@app.put("/api/projects/{project_id}/shots/{shot_id}", dependencies=[Depends(authenticated)])
def edit_shot(project_id: str, shot_id: str, body: ShotSpec):
    with Session.begin() as db:
        project = project_locked(db, project_id)
        idle(db, project_id)
        shot = shot_locked(db, project_id, shot_id)
        specs = [body.model_dump() if s.id == shot_id else s.spec for s in db.scalars(
            select(Shot).where(Shot.project_id == project_id).order_by(Shot.position))]
        try:
            script = Script.model_validate({**project.script, "shots": specs})
        except ValidationError:
            raise HTTPException(422, "分镜不符合剧本结构，请检查角色引用与总时长")
        if sum(s.duration for s in script.shots) > project.duration:
            raise HTTPException(422, "镜头总时长超过项目目标")
        shot.spec = body.model_dump()
        shot.asset_id = None
        shot.status = "draft"
        project.script = script.model_dump()
        project.export_asset = None
        return {"ok": True}


@app.post("/api/projects/{project_id}/shots/{shot_id}/reference-frame", dependencies=[Depends(authenticated)])
def prepare_reference(project_id: str, shot_id: str, body: ReferenceFrameInput):
    with Session.begin() as db:
        project = project_locked(db, project_id)
        idle(db, project_id)
        target = shot_locked(db, project_id, shot_id)
        source = shot_locked(db, project_id, body.source_shot_id)
        if source.position != target.position - 1 or source.asset_id != body.source_asset_id:
            raise HTTPException(409, "请选择上一镜头当前的视频素材")
        asset = db.get(Asset, source.asset_id)
        if not asset or asset.kind != "video" or source.status != "succeeded":
            raise HTTPException(409, "上一镜头须先生成完成")
        relative = f"{project_id}/references/{uid()}/frame.png"
        try:
            metadata, canonical = reference_frame(local_asset(db, asset), path_for(relative), project.ratio, body.frame_index)
        except (ValueError, OSError, KeyError):
            raise HTTPException(422, "选帧失败，请检查帧号、素材和 FFmpeg")
        metadata.update(source_shot_id=source.id, source_asset_id=asset.id,
                        source_preview_path=str(canonical.relative_to(path_for("."))))
        frame = Asset(project_id=project_id, kind="image", path=relative,
                      duration=None, metadata_json=metadata)
        db.add(frame)
        db.flush()
        return {"asset_id": frame.id, **metadata}


@app.post("/api/projects/{project_id}/reference-images", dependencies=[Depends(authenticated)])
def upload_reference_image(project_id: str, body: ImageUpload):
    from .media import image_info
    with Session.begin() as db:
        project_locked(db, project_id)
        idle(db, project_id)
        relative = f"{project_id}/references/{uid()}/upload"
        target = path_for(relative)
        try:
            raw = base64.b64decode(body.data, validate=True)
            if len(raw) > 29 * 1024 * 1024:
                raise ValueError("图片超过 29 MB")
            if not (raw.startswith(b"\x89PNG\r\n\x1a\n") or raw.startswith(b"\xff\xd8\xff")):
                raise ValueError("请上传 PNG 或 JPEG 图片")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            info = image_info(target)
            info["origin"] = "uploaded"
            asset = Asset(project_id=project_id, kind="image", path=relative, metadata_json=info)
            db.add(asset)
            db.flush()
            return {"asset_id": asset.id, **info}
        except (ValueError, OSError, binascii.Error) as error:
            target.unlink(missing_ok=True)
            raise HTTPException(422, str(error))


@app.put("/api/projects/{project_id}/shots/{shot_id}/generation-settings", dependencies=[Depends(authenticated)])
def set_generation(project_id: str, shot_id: str, body: GenerationInput):
    with Session.begin() as db:
        project = project_locked(db, project_id)
        idle(db, project_id)
        target = shot_locked(db, project_id, shot_id)
        config = body.model_dump(exclude_none=True)
        if body.mode in ("first_frame", "first_last_frame"):
            frame = db.get(Asset, body.reference_asset_id)
            if not frame or frame.project_id != project_id or frame.kind != "image":
                raise HTTPException(422, "请选择当前项目的参考帧")
            if frame.metadata_json.get("origin") != "uploaded":
                source = shot_locked(db, project_id, frame.metadata_json.get("source_shot_id", ""))
                if source.position != target.position - 1 or source.asset_id != frame.metadata_json.get("source_asset_id"):
                    raise HTTPException(409, "参考帧来源已变化，请重新选帧")
                source.edit_config = {"asset_id": source.asset_id,
                                      "out_frame_exclusive": frame.metadata_json["out_frame_exclusive"]}
        target.generation_config = config
        try:
            refs = references_snapshot(db, target, project.ratio)
            validate_reference_mode(project, config, refs)
        except (ValueError, OSError, KeyError) as error:
            raise HTTPException(422, str(error))
        project.export_asset = None
        return {"ok": True}


def reference_ratio_matches(reference, ratio):
    metadata = reference.metadata_json
    width, height = SIZES[ratio]
    return bool(metadata.get("height") and abs(metadata.get("width", 0) / metadata["height"] / (width / height) - 1) < .01)


def validate_reference_mode(project, config, refs):
    if video_provider() == "comfyui":
        from .comfyui_config import validate_request
        validate_request(comfyui_config(), video_settings(project), project.ratio,
                         generation=config, references=refs)
        return
    model = project.video_model
    if refs and not model.startswith(("doubao-seedance-2-0", "doubao-seedance-2-5", "doubao-seedance-1-0-pro")):
        raise ValueError("参考模式请使用明确的 Seedance Model ID")
    if config["mode"] == "reference" and not model.startswith(("doubao-seedance-2-0", "doubao-seedance-2-5")):
        raise ValueError("多模态参考需要 Seedance 2.0 或 2.5")
    if any(r["type"] == "audio_url" for r in refs) and not video_settings(project)["generate_audio"]:
        raise ValueError("音频参考需要开启生成声音")
    encoded_size = sum((path_for(r["path"]).stat().st_size + 2) // 3 * 4 for r in refs if r["type"] == "image_url")
    if encoded_size > 63 * 1024 * 1024:
        raise ValueError("参考图片编码后合计过大，请压缩图片（请求体上限 64 MB）")


def video_snapshot(db, project, shot):
    try:
        reference = reference_snapshot(db, shot, project.ratio)
        refs = references_snapshot(db, shot, project.ratio)
        validate_reference_mode(project, generation_config(shot), refs)
    except (ValueError, OSError, KeyError) as error:
        raise HTTPException(409, str(error))
    if video_provider() == "ark" and reference and not project.video_model.startswith(("doubao-seedance-2-0", "doubao-seedance-2-5",
                                                        "doubao-seedance-1-0-pro")):
        raise HTTPException(422, "首帧模式请使用已验证的 Seedance 2.0、2.5 或 1.0 pro Model ID")
    data = {**snapshot(project), "shot": shot.spec,
            "generation_config": generation_config(shot), "reference": reference, "references": refs}
    try:
        validate_settings(project.video_model, data["video_settings"])
    except ValueError as error:
        raise HTTPException(422, str(error))
    data["prompt"] = build_prompt(data)
    data["video_provider"] = video_provider()
    if data["video_provider"] == "comfyui":
        from .comfyui_config import validate_request
        data["comfyui"] = comfyui_config()
        data["seed"] = secrets.randbits(63)
        try:
            validate_request(data["comfyui"], data["video_settings"], project.ratio,
                             shot.spec["duration"], data["generation_config"], refs)
        except ValueError as error:
            raise HTTPException(422, str(error))
    return data


@app.get("/api/projects/{project_id}/shots/{shot_id}/generation-preview", dependencies=[Depends(authenticated)])
def generation_preview(project_id: str, shot_id: str):
    with Session() as db:
        project = db.get(Project, project_id)
        if not project:
            raise HTTPException(404, "项目不存在")
        data = video_snapshot(db, project, shot_locked(db, project_id, shot_id))
        return {"prompt": data["prompt"], "model": project.video_model,
                "duration": data["shot"]["duration"],
                "ratio": "adaptive" if data["reference"] and data["video_provider"] == "ark" else project.ratio,
                **data["video_settings"], "return_last_frame": data["video_provider"] == "ark",
                "reference_asset_id": data["reference"]["asset_id"] if data["reference"] else None,
                "references": [{k: r[k] for k in ("asset_id", "role", "type", "duration", "url") if k in r}
                               for r in data["references"]],
                "warnings": ["提示词超过 500 字，建议精简当前镜头描述"] if len(data["prompt"]) > 500 else []}


def video_job(db, project, shot):
    data = video_snapshot(db, project, shot)
    job = Job(project_id=project.id, shot_id=shot.id, kind="video", snapshot=data)
    db.add(job)
    shot.status = "queued"
    project.export_asset = None
    return job


@app.post("/api/projects/{project_id}/videos", dependencies=[Depends(authenticated)])
def generate_videos(project_id: str):
    with Session.begin() as db:
        project = project_locked(db, project_id)
        idle(db, project_id)
        shots = list(db.scalars(select(Shot).where(Shot.project_id == project_id).order_by(Shot.position)))
        if not shots:
            raise HTTPException(409, "请先生成剧本")
        ids = []
        for shot in shots:
            if needs_generation(db, shot, project):
                job = video_job(db, project, shot)
                db.flush()
                ids.append(job.id)
        return {"jobs": ids}


@app.post("/api/projects/{project_id}/shots/{shot_id}/generate", dependencies=[Depends(authenticated)])
def regenerate_shot(project_id: str, shot_id: str):
    with Session.begin() as db:
        project = project_locked(db, project_id)
        idle(db, project_id)
        job = video_job(db, project, shot_locked(db, project_id, shot_id))
        db.flush()
        return job_data(job)


@app.post("/api/projects/{project_id}/compose", dependencies=[Depends(authenticated)])
def composition(project_id: str):
    with Session.begin() as db:
        project = project_locked(db, project_id)
        idle(db, project_id)
        return queue_composition(db, project)


def queue_composition(db, project):
    project_id = project.id
    shots = list(db.scalars(select(Shot).where(Shot.project_id == project_id).order_by(Shot.position)))
    if not shots or any(not s.asset_id or s.status != "succeeded" or needs_generation(db, s, project) for s in shots):
        raise HTTPException(409, "所有镜头完成后才能拼接")
    if any(not media_ready(db, s.asset_id) for s in shots):
        raise HTTPException(409, "请等待所有镜头上传完成；上传失败可单独重试")
    for shot in shots:
        try:
            references_snapshot(db, shot, project.ratio)
        except (ValueError, OSError, KeyError):
            raise HTTPException(409, "镜头参考来源已变化，请重新确认并生成")
    job = Job(project_id=project_id, kind="compose",
              snapshot={**snapshot(project), "assets": [s.asset_id for s in shots],
                        "cuts": [s.edit_config if s.edit_config.get("asset_id") == s.asset_id else {} for s in shots]})
    db.add(job)
    db.flush()
    return job_data(job)


@app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(authenticated)])
def cancel_script(job_id: str):
    with Session.begin() as db:
        existing = db.get(Job, job_id)
        if not existing:
            raise HTTPException(404, "任务不存在")
        project_locked(db, existing.project_id)
        # Refresh the initial unlocked read: the Worker may have finished while
        # cancellation was waiting for the project lock.
        job = db.scalar(select(Job).where(Job.id == job_id).with_for_update()
                        .execution_options(populate_existing=True))
        if job.kind != "script":
            raise HTTPException(409, "此入口仅支持取消剧本任务")
        if job.status == "cancelled":
            return job_data(job)
        if job.status not in ("queued", "running", "waiting"):
            raise HTTPException(409, "任务已结束，请刷新任务记录")
        job.status = "cancelled"
        job.phase = "cancelled"
        job.error = None
        job.error_details = None
        # Fence in-flight work: its old owner can no longer persist any output.
        job.owner = None
        job.lease_until = None
        return job_data(job)


@app.post("/api/jobs/{job_id}/recover", dependencies=[Depends(authenticated)])
def recover(job_id: str, body: RecoveryInput):
    with Session.begin() as db:
        existing = db.get(Job, job_id)
        if not existing:
            raise HTTPException(404, "任务不存在")
        project_locked(db, existing.project_id)
        # 等待项目锁期间状态可能变化，刷新本次请求先前读入的任务对象。
        job = db.scalar(select(Job).where(Job.id == job_id).with_for_update()
                        .execution_options(populate_existing=True))
        if job.status not in ("failed", "uncertain"):
            raise HTTPException(409, "仅失败或提交待核对任务可恢复")
        # 不同分镜的视频可以排队；项目级任务与当前分镜任务必须互斥。
        if db.scalar(select(Job.id).where(
            Job.project_id == job.project_id, Job.id != job.id, Job.status.in_(ACTIVE),
            or_(Job.shot_id == job.shot_id, Job.shot_id.is_(None))
            if job.shot_id else text("true")).limit(1)):
            raise HTTPException(409, "相关任务尚在执行")
        if job.kind == "video":
            shot = db.get(Shot, job.shot_id) if job.shot_id else None
            if not shot or shot.spec != job.snapshot["shot"]:
                raise HTTPException(409, "此任务的分镜已被修改或替换，请从当前分镜重新生成")
            if "generation_config" in job.snapshot and GenerationInput.model_validate(generation_config(shot)) != GenerationInput.model_validate(job.snapshot["generation_config"]):
                raise HTTPException(409, "此任务的首帧配置已变化，请从当前分镜重新生成")
            if db.scalar(select(Job.id).where(Job.shot_id == job.shot_id,
                Job.created_at > job.created_at, Job.status == "succeeded").limit(1)):
                raise HTTPException(409, "此镜头已有更新的成功任务，请从当前分镜重生成")
            if job.phase == "rejected":
                if not body.acknowledge_duplicate_cost:
                    raise HTTPException(409, "模型已终止此任务，重新生成会再次调用模型；请确认可能计费")
                job.external_id = None
                if job.snapshot.get("video_provider") == "comfyui":
                    job.snapshot = {k: v for k, v in job.snapshot.items() if k != "comfyui_job_url"}
            if body.external_id and job.phase != "rejected":
                job.external_id = body.external_id
                if job.snapshot.get("video_provider") == "comfyui":
                    job.snapshot = {k: v for k, v in job.snapshot.items() if k != "comfyui_job_url"}
            if job.status == "uncertain" and not job.external_id and not body.acknowledge_duplicate_cost:
                raise HTTPException(409, "请填写已受理任务 ID，或明确确认可能重复计费后重提")
            if job.snapshot.get("video_provider") == "comfyui" and not job.external_id:
                # 人工恢复会清零失败次数，另存本轮标识以免复用先前的幂等键。
                job.snapshot = {**job.snapshot, "comfyui_submission_nonce": secrets.token_hex(16)}
            job.phase = "polling" if job.external_id else "pending"
            shot.status = "queued"
        elif job.kind == "compose":
            shots = list(db.scalars(select(Shot).where(Shot.project_id == job.project_id).order_by(Shot.position)))
            current = [s.asset_id for s in shots]
            if current != job.snapshot["assets"]:
                raise HTTPException(409, "镜头素材已经变化，请重新创建拼接任务")
            cuts = [s.edit_config if s.edit_config.get("asset_id") == s.asset_id else {} for s in shots]
            if "cuts" in job.snapshot and cuts != job.snapshot["cuts"]:
                raise HTTPException(409, "剪辑出点已变化，请重新创建拼接任务")
            if output_settings(job.snapshot.get("video_settings", {})) != output_settings(video_settings(db.get(Project, job.project_id))):
                raise HTTPException(409, "导出规格已变化，请重新创建拼接任务")
            if job.snapshot["ratio"] != db.get(Project, job.project_id).ratio:
                raise HTTPException(409, "画面比例已变化，请重新创建拼接任务")
            if (job.snapshot.get("video_model") != db.get(Project, job.project_id).video_model
                    or any(needs_generation(db, s, db.get(Project, job.project_id)) for s in shots)):
                raise HTTPException(409, "镜头模型或生成配置已变化，请重新生成镜头后创建拼接任务")
            job.phase = "pending"
        else:
            job.phase = "pending"
            job.snapshot = {**job.snapshot, "_script_started_at": now().isoformat()}
        job.status = "queued"
        job.error = None
        job.error_details = None
        job.owner = None
        job.lease_until = None
        job.attempts = 0
        job.next_run = now()
        return job_data(job)


def media_access(db, asset, download=False, preview=False):
    try:
        return access_data(db, asset, download, preview)
    except FileNotFoundError:
        raise HTTPException(404, "素材不存在")
    except Exception:
        # 签名错误与模型生成无关，异常文本可能含凭证，不能直接返回。
        raise HTTPException(503, "素材访问地址暂不可用，请稍后刷新或检查 TOS 配置") from None


@app.get("/api/assets/{asset_id}/access", dependencies=[Depends(authenticated)])
def asset_access(asset_id: str, download: bool = False, preview: bool = False):
    with Session() as db:
        asset = db.get(Asset, asset_id)
        if not asset or (preview and (asset.kind != "image" or not asset.metadata_json.get("source_preview_path"))):
            raise HTTPException(404, "素材不存在")
        return media_access(db, asset, download, preview)


@app.get("/api/assets/{asset_id}", dependencies=[Depends(authenticated)])
def asset(asset_id: str, download: bool = False):
    with Session() as db:
        asset = db.get(Asset, asset_id)
        if not asset:
            raise HTTPException(404, "素材不存在")
        if storage_settings():
            access = media_access(db, asset, download)
            if not access["url"]:
                raise HTTPException(409, "素材尚未上传完成，请稍后刷新")
            return RedirectResponse(access["url"], status_code=307)
        path = path_for(asset.path)
        if not path.is_file():
            raise HTTPException(404, "素材文件已丢失")
        mime = asset.metadata_json.get("mime_type", "image/png") if asset.kind == "image" else "video/mp4"
        extension = "png" if mime == "image/png" else "jpg" if mime == "image/jpeg" else "mp4"
        return FileResponse(path, media_type=mime,
                            filename=f"{asset_id}.{extension}" if download else None)


@app.get("/api/assets/{asset_id}/source-preview", dependencies=[Depends(authenticated)])
def source_preview(asset_id: str):
    with Session() as db:
        reference = db.get(Asset, asset_id)
        if not reference or reference.kind != "image" or not reference.metadata_json.get("source_preview_path"):
            raise HTTPException(404, "参考帧预览不存在")
        if storage_settings():
            access = media_access(db, reference, preview=True)
            if not access["url"]:
                raise HTTPException(409, "预览尚未上传完成，请稍后刷新")
            return RedirectResponse(access["url"], status_code=307)
        path = path_for(reference.metadata_json["source_preview_path"])
        if not path.is_file():
            raise HTTPException(404, "预览素材丢失，请重新选帧")
        return FileResponse(path, media_type="video/mp4")


@app.get("/api/assets/{asset_id}/info", dependencies=[Depends(authenticated)])
def asset_info(asset_id: str):
    with Session() as db:
        asset = db.get(Asset, asset_id)
        if not asset:
            raise HTTPException(404, "素材不存在")
        return {"asset_id": asset.id, "kind": asset.kind, "duration": asset.duration,
                **asset.metadata_json, "backup": backup_data(db, asset.id),
                "media_ready": media_ready(db, asset.id)}


@app.post("/api/assets/{asset_id}/backup/retry", dependencies=[Depends(authenticated)])
def retry_backup(asset_id: str):
    config = storage_settings()
    if not config:
        raise HTTPException(409, "未启用 TOS 自动备份，请管理员配置素材桶")
    with Session.begin() as db:
        if not db.get(Asset, asset_id):
            raise HTTPException(404, "素材不存在")
        row = db.scalar(select(AssetBackup).where(
            AssetBackup.asset_id == asset_id).with_for_update())
        if row and row.status == "failed":
            row.status = "queued"
            row.next_run = now()
            row.error = None
        return backup_data(db, asset_id)


@app.get("/api/projects/{project_id}/export", dependencies=[Depends(authenticated)])
def export_script(project_id: str):
    with Session() as db:
        project = db.get(Project, project_id)
        if not project:
            raise HTTPException(404, "项目不存在")
        return project.script


# 官方 ComfyUI 的 HTTP 业务扩展入口；保持与原业务接口共用认证和事务。
from . import studio  # noqa: E402,F401
