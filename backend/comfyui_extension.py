"""官方 ComfyUI 自定义节点，通过 HTTP 接入持久任务，不修改官方源码。"""
import hashlib
import hmac
from importlib.util import find_spec
import json
import math
import os
from pathlib import Path
import re
import secrets
from tempfile import NamedTemporaryFile
import time
from urllib.parse import urlsplit

from aiohttp import web
import httpx
from itsdangerous import BadSignature, URLSafeTimedSerializer

WEB_DIRECTORY = "./web"
BACKEND = os.getenv("ANIMATION_BACKEND_URL", "http://127.0.0.1:8000")


def secret(name):
    return Path(os.environ[f"{name.upper()}_FILE"]).read_text().strip()


def request(method, path, data=None):
    # 使用同一登录签名契约，内部调用不占用网页登录的限流额度。
    csrf = secrets.token_urlsafe(32)
    session = URLSafeTimedSerializer(secret("session_key"), salt="animation-owner-v1").dumps({"csrf": csrf})
    with httpx.Client(base_url=BACKEND, timeout=30, trust_env=False) as client:
        client.cookies.set("session", session)
        response = client.request(method, "/api" + path, json=data,
                                  headers={"x-csrf-token": csrf})
        if response.is_error:
            detail = response.json().get("detail", "业务接口请求失败")
            raise RuntimeError(str(detail))
        return response.json()


def wait(project_id, stage):
    from comfy.model_management import throw_exception_if_processing_interrupted
    deadline = time.monotonic() + 7200
    while True:
        throw_exception_if_processing_interrupted()
        state = request("GET", f"/native/projects/{project_id}/stages/{stage}")
        if state["status"] == "succeeded":
            return json.dumps(state, ensure_ascii=False)
        if state["status"] in ("failed", "uncertain", "cancelled"):
            raise RuntimeError(state.get("error") or "请通过业务任务接口核对并恢复原任务")
        if time.monotonic() >= deadline:
            raise RuntimeError("节点等待超时，任务仍保留；请查询原项目的任务状态")
        time.sleep(3)


class ProjectNode:
    CATEGORY = "火山引擎/历史项目工作流"
    RETURN_TYPES = ("ANIMATION_PROJECT",)
    RETURN_NAMES = ("项目",)
    FUNCTION = "execute"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "项目名称": ("STRING", {"default": "我的视频"}),
            "创作轮次": ("STRING", {"default": "1"}),
            "创作要求": ("STRING", {"multiline": True, "default": "请创作一段有连贯故事的视频"}),
            "风格": ("STRING", {"default": ""}),
            "角色": ("STRING", {"multiline": True, "default": ""}),
            "目标时长": ("INT", {"default": 30, "min": 4, "max": 180}),
            "画幅": (["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"],),
            "已有项目编号": ("STRING", {"default": ""}),
        }}

    def execute(self, **values):
        data = request("POST", "/native/projects", values)
        return (data["id"],)


class StageNode:
    CATEGORY = ProjectNode.CATEGORY
    RETURN_TYPES = ("ANIMATION_PROJECT", "STRING")
    RETURN_NAMES = ("项目", "结果")
    FUNCTION = "execute"
    OUTPUT_NODE = True
    STAGE = ""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"项目": ("ANIMATION_PROJECT",)}}

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # 每次执行先查询持久任务，缓存失效不会触发模型重提。
        return math.nan

    def execute(self, 项目):
        request("POST", f"/native/projects/{项目}/stages/{self.STAGE}", {})
        result = wait(项目, self.STAGE)
        return {"ui": {"text": [result]}, "result": (项目, result)}


class ScriptNode(StageNode):
    STAGE = "script"


class VideoNode(StageNode):
    STAGE = "video"


class ComposeNode(StageNode):
    STAGE = "compose"


class ArkTextToVideo:
    CATEGORY = "火山方舟"
    RETURN_TYPES = ("VIDEO",)
    RETURN_NAMES = ("视频",)
    FUNCTION = "execute"
    OUTPUT_NODE = True
    DESCRIPTION = "使用服务端方舟 Key 生成视频。同一批次和参数复用原任务，换批次生成新视频。"

    @classmethod
    def INPUT_TYPES(cls):
        capabilities = request("GET", "/ark/video-models")
        return {"required": {
            "prompt": ("STRING", {"multiline": True, "default": "一只纸船在平静的水面上缓慢漂动，固定镜头。"}),
            "model": (capabilities["models"],),
            "duration": ("INT", {"default": 4, "min": 4, "max": 15}),
            "ratio": (capabilities["ratios"],),
            "resolution": (["480p", "720p", "1080p"], {"default": "720p"}),
            "generate_audio": ("BOOLEAN", {"default": True}),
            "watermark": ("BOOLEAN", {"default": False}),
            "generation_id": ("STRING", {"default": "1", "tooltip": "生成批次。相同批次和参数复用原任务。生成新视频请更换批次。"}),
        }}

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def execute(self, **values):
        from comfy.model_management import throw_exception_if_processing_interrupted
        from comfy_api.latest import InputImpl, io, ui
        import folder_paths

        state = request("POST", "/ark/videos", values)
        job_id = state["id"]
        deadline = time.monotonic() + 7200
        while state["status"] != "succeeded":
            throw_exception_if_processing_interrupted()
            if state["status"] in ("failed", "uncertain", "cancelled", "upload_failed"):
                raise RuntimeError(f"任务 {job_id}：{state.get('error') or state['status']}。保留生成批次可查询原任务。")
            if time.monotonic() >= deadline:
                raise RuntimeError(f"节点等待超时，任务 {job_id} 仍保留。保持生成批次再次运行以查询原任务。")
            time.sleep(3)
            state = request("GET", f"/ark/videos/{job_id}")
        throw_exception_if_processing_interrupted()
        directory = Path(folder_paths.get_temp_directory()) / "ark"
        directory.mkdir(parents=True, exist_ok=True)
        # Both identifiers come from the backend, never from a workflow path input.
        import uuid
        filename = f"{uuid.UUID(job_id)}-{uuid.UUID(state['asset_id'])}.mp4"
        target = directory / filename
        csrf = secrets.token_urlsafe(32)
        cookie = URLSafeTimedSerializer(secret("session_key"), salt="animation-owner-v1").dumps({"csrf": csrf})
        temporary = None
        try:
            with httpx.Client(base_url=BACKEND, timeout=120, trust_env=False) as client:
                client.cookies.set("session", cookie)
                with client.stream("GET", f"/api/ark/videos/{job_id}/content") as response:
                    if response.is_error:
                        raise RuntimeError(f"任务 {job_id} 的视频下载失败，请保持生成批次重试")
                    checksum = response.headers.get("x-content-sha256")
                    digest = hashlib.sha256()
                    size = 0
                    with NamedTemporaryFile(dir=directory, suffix=".part", delete=False) as stream:
                        temporary = Path(stream.name)
                        for chunk in response.iter_bytes():
                            throw_exception_if_processing_interrupted()
                            size += len(chunk)
                            if size > 1024 * 1024 * 1024:
                                raise RuntimeError("视频超过 1 GiB 本地下载限制")
                            digest.update(chunk)
                            stream.write(chunk)
                    if not size or not checksum or digest.hexdigest() != checksum:
                        raise RuntimeError("视频完整性校验失败，请保持生成批次重试")
                    temporary.replace(target)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
        video = InputImpl.VideoFromFile(str(target))
        return {"ui": ui.PreviewVideo([ui.SavedResult(filename, "ark", io.FolderType.temp)]).as_dict(),
                "result": (video,)}


NODE_CLASS_MAPPINGS = {
    "VolcArkTextToVideo": ArkTextToVideo,
    "VolcProject": ProjectNode, "VolcScript": ScriptNode,
    "VolcVideo": VideoNode, "VolcCompose": ComposeNode,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "VolcArkTextToVideo": "火山方舟视频生成",
    "VolcProject": "火山 · 项目", "VolcScript": "火山 · Ark 剧本",
    "VolcVideo": "火山 · 分镜视频", "VolcCompose": "火山 · 拼接与 TOS 归档",
}


def install_security(server):
    has_templates = find_spec("comfyui_workflow_templates") is not None
    if not has_templates:
        from folder_paths import get_public_user_directory

        # The studio supplies its own workflows. Skip the upstream first-run template picker.
        settings = Path(get_public_user_directory("default")) / "comfy.settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        try:
            with settings.open("x") as stream:
                json.dump({"Comfy.TutorialCompleted": True}, stream)
        except FileExistsError:
            pass

    @web.middleware
    async def authenticated(request, handler):
        try:
            session = URLSafeTimedSerializer(secret("session_key"), salt="animation-owner-v1").loads(
                request.cookies.get("session", ""), max_age=86400)
        except (BadSignature, KeyError):
            return web.json_response({"error": "请先登录"}, status=401)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin", "")
            if origin and urlsplit(origin).netloc != request.host:
                return web.json_response({"error": "请求来源无效"}, status=403)
            if request.path in ("/prompt", "/api/prompt") and not hmac.compare_digest(
                    str(session["csrf"]), request.headers.get("x-csrf-token", "")):
                return web.json_response({"error": "请刷新登录状态"}, status=403)
        if not has_templates and request.method in ("GET", "HEAD"):
            if request.path == "/templates/index_logo.json":
                return web.json_response({})
            if re.fullmatch(r"/templates/index(?:\.[\w-]+)?\.json", request.path):
                return web.json_response([])
        return await handler(request)
    server.app.middlewares.append(authenticated)


try:
    from server import PromptServer
    install_security(PromptServer.instance)
except ImportError:
    # 本地契约测试可单独载入本文件，不启动 ComfyUI。
    pass
