import json
import base64
import logging
import time
from datetime import datetime, timezone

import httpx
from pydantic import ValidationError

from .config import ark_key, models
from .schemas import Script
from .generation import build_prompt
from .diagnostics import provider_details, safe_text

log = logging.getLogger("provider")
SCRIPT_READ_TIMEOUT_SECONDS = 180


class ProviderError(Exception):
    def __init__(self, message: str, retryable=False, uncertain=False, terminal=False, details=None):
        super().__init__(safe_text(message) or "模型服务失败")
        self.retryable = retryable
        self.uncertain = uncertain
        self.terminal = terminal
        self.details = details


class Ark:
    def __init__(self):
        key = ark_key()
        if not key:
            raise ProviderError("未配置方舟 API Key，请管理员填写密钥文件")
        self.client = httpx.Client(
            base_url=models()["ark_base_url"].rstrip("/") + "/",
            headers={"Authorization": f"Bearer {key}"},
            timeout=httpx.Timeout(90, connect=15),
            follow_redirects=False,
        )

    def close(self):
        self.client.close()

    def request(self, method, path, **kwargs):
        is_submit = method == "POST" and path == "contents/generations/tasks"
        started = time.monotonic()
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError as error:
            elapsed = time.monotonic() - started
            reason = next((label for kind, label in (
                (httpx.ConnectTimeout, "连接超时"),
                (httpx.ReadTimeout, "读取响应超时"),
                (httpx.WriteTimeout, "发送请求超时"),
                (httpx.PoolTimeout, "连接池等待超时"),
                (httpx.ConnectError, "连接失败"),
            ) if isinstance(error, kind)), "网络传输失败")
            # Log only diagnostic fields, never exception text, headers or response bodies.
            log.warning("Ark %s %s failed: %s elapsed=%.2fs",
                        method, path, type(error).__name__, elapsed)
            raise ProviderError(f"模型服务{reason}（{elapsed:.1f} 秒）",
                                retryable=not is_submit, uncertain=is_submit) from None
        log.info("Ark %s %s HTTP=%s elapsed=%.2fs",
                 method, path, response.status_code, time.monotonic() - started)
        if response.status_code >= 400:
            # Extract only sanitized diagnostic fields, never the response body.
            try:
                body = response.json()
            except ValueError:
                body = {}
            details = provider_details(body, http_status=response.status_code, headers=response.headers)
            transient = response.status_code == 429 or response.status_code >= 500
            raise ProviderError(
                details["summary"],
                retryable=transient and not is_submit,
                uncertain=is_submit and response.status_code >= 500,
                details=details,
            )
        try:
            return response.json()
        except ValueError:
            raise ProviderError("模型服务返回无效 JSON", uncertain=is_submit)

    def script(self, snapshot):
        prompt = (
            "你是动画编剧。只返回符合以下 JSON schema 的 JSON 对象，不要 Markdown。"
            "每镜头 4-15 秒，最多30镜头，总时长不得超过用户目标时长。"
            "characters 引用必须与角色名称一致。台词与声音写进分镜。schema: "
            "相邻镜头须明确start_state、end_state和continuity_notes：角色朝向、屏幕位置、"
            "主体大小、姿态与动作阶段，以及相机和背景锚点。后一镜头接前一镜头结束状态，"
            "除非明确换场，不得把同一动作重新开始。"
            + json.dumps(Script.model_json_schema(), ensure_ascii=False)
        )
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(snapshot["project"], ensure_ascii=False)},
        ]
        for attempt in range(3):
            remaining = SCRIPT_READ_TIMEOUT_SECONDS
            if snapshot.get("_script_deadline"):
                remaining = min(remaining, (
                    datetime.fromisoformat(snapshot["_script_deadline"]) - datetime.now(timezone.utc)
                ).total_seconds())
                if remaining <= 0:
                    raise ProviderError("剧本任务总时限已到，请调整设置后重新生成")
            data = self.request("POST", "chat/completions", json={
                "model": snapshot["script_model"], "messages": messages,
                "response_format": {"type": "json_object"}, "max_tokens": 10000,
            }, timeout=httpx.Timeout(remaining, connect=min(15, remaining)))
            try:
                content = data["choices"][0]["message"]["content"]
                result = Script.model_validate_json(content)
                if sum(s.duration for s in result.shots) > snapshot["project"]["duration"]:
                    raise ValueError("分镜总时长超过项目目标")
                return result.model_dump()
            except (KeyError, IndexError, TypeError, ValidationError, ValueError):
                if attempt == 2:
                    raise ProviderError("剧本结构连续三次校验失败，请调整创意后重试")
                messages.append({"role": "user", "content": "输出结构或时长不符合 schema，请重新生成合法 JSON。"})

    def submit(self, snapshot):
        shot = snapshot["shot"]
        prompt = snapshot.get("prompt") or build_prompt(snapshot)
        content = [{"type": "text", "text": prompt}]
        reference = snapshot.get("reference")
        references = snapshot.get("references")
        if references is None:
            references = [{**reference, "role": "first_frame", "type": "image_url"}] if reference else []
        for ref in references:
            if ref["type"] != "image_url":
                from .media import stable_reference_url
                try:
                    stable_reference_url(ref["url"])
                except (ValueError, OSError):
                    raise ProviderError("远端参考地址校验失败，请检查长期公网素材地址")
                content.append({"type": ref["type"], "role": ref["role"], ref["type"]: {"url": ref["url"]}})
                continue
            from .continuity import validate_reference_file
            from .media import path_for
            try:
                validate_reference_file(ref)
                raw = path_for(ref["path"]).read_bytes()
                if len(raw) > 29 * 1024 * 1024:
                    raise ValueError("参考帧超过图片大小限制")
            except (ValueError, OSError):
                raise ProviderError("参考帧校验失败，请重新确认素材")
            content.append({"type": "image_url", "role": ref["role"],
                            "image_url": {"url": f"data:{ref['mime_type']};base64," +
                                          base64.b64encode(raw).decode("ascii")}})
        payload = {
            "model": snapshot["video_model"],
            "content": content,
            "duration": shot["duration"], "ratio": snapshot["ratio"],
            **snapshot.get("video_settings", {"resolution": "720p", "generate_audio": True, "watermark": False}),
            "return_last_frame": True,
        }
        if reference or any(r["role"] == "first_frame" for r in references):
            payload["ratio"] = "adaptive"
        if len(json.dumps(payload).encode()) > 64 * 1024 * 1024:
            raise ProviderError("图片编码后的请求体超过 64 MB")
        data = self.request("POST", "contents/generations/tasks", json=payload)
        external_id = data.get("id")
        if not isinstance(external_id, str) or not external_id:
            raise ProviderError("视频提交未返回任务 ID，需要人工核对", uncertain=True)
        return external_id

    def poll(self, external_id):
        from urllib.parse import quote
        data = self.request("GET", "contents/generations/tasks/" + quote(external_id, safe=""))
        status = data.get("status")
        if status == "succeeded":
            url = data.get("content", {}).get("video_url")
            if not url:
                raise ProviderError("成功任务未返回视频地址")
            return {"video_url": url, "last_frame_url": data.get("content", {}).get("last_frame_url"),
                    "usage": data.get("usage", {}), "model": data.get("model")}
        if status in ("failed", "cancelled", "expired"):
            details = provider_details(data, status=status)
            raise ProviderError(details["summary"], terminal=True, details=details)
        if status not in ("queued", "running"):
            raise ProviderError("视频任务返回未知状态")
        return None
