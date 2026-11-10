"""官方 ComfyUI 自定义节点，通过 HTTP 接入持久任务，不修改官方源码。"""
import hmac
from importlib.util import find_spec
import json
import math
import os
from pathlib import Path
import re
import secrets
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
            raise RuntimeError(state.get("error") or "请在任务面板核对并恢复原任务")
        if time.monotonic() >= deadline:
            raise RuntimeError("节点等待超时，任务仍保留；请在任务面板查看，勿重复创建项目")
        time.sleep(3)


class ProjectNode:
    CATEGORY = "火山引擎/视频工作室"
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


NODE_CLASS_MAPPINGS = {
    "VolcProject": ProjectNode, "VolcScript": ScriptNode,
    "VolcVideo": VideoNode, "VolcCompose": ComposeNode,
}
NODE_DISPLAY_NAME_MAPPINGS = {
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
