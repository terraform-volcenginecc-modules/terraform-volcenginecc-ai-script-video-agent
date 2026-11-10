"""远程 Comfy API v2 视频适配，不管理任务队列或媒资归档。"""
import logging
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx

from .comfyui_config import validate_config, workflow_for
from .config import comfyui_key
from .diagnostics import identifier
from .provider import ProviderError

log = logging.getLogger("comfyui")


class ComfyUI:
    def __init__(self, config, job_url=None):
        try:
            self.config = validate_config(config)
            key = comfyui_key(config)
            if not key or len(key) > 16384 or any(c in key for c in "\n\r\x00"):
                raise ValueError("缺少 ComfyUI 单行认证密钥")
        except (ValueError, OSError):
            raise ProviderError("ComfyUI 配置或认证无效，请检查真实工作流和密钥文件") from None
        self.base_url = config["base_url"].rstrip("/") + "/"
        self.job_url = job_url
        self.client = httpx.Client(base_url=self.base_url, headers={"Authorization": f"Bearer {key}"},
                                   timeout=httpx.Timeout(120, connect=15), follow_redirects=False)

    def close(self):
        self.client.close()

    def service_url(self, url):
        # 响应中的主机相对链接已包含挂载前缀，直接按服务源地址解析。
        resolved = urljoin(self.base_url, url)
        parsed, base = urlsplit(resolved), urlsplit(self.base_url)
        if (parsed.scheme, parsed.hostname, parsed.port) != (base.scheme, base.hostname, base.port):
            raise ProviderError("ComfyUI 返回了其他服务的认证地址")
        path = unquote(parsed.path)
        if parsed.username or parsed.password or parsed.fragment or not path.startswith(unquote(base.path) + "api/v2/"):
            raise ProviderError("ComfyUI 返回了无效的 API 地址")
        if any(part in (".", "..") for part in path.split("/")):
            raise ProviderError("ComfyUI 返回了非法 API 路径")
        return resolved

    def request(self, method, path, *, submitting=False, **kwargs):
        url = self.service_url(path)
        try:
            response = self.client.request(method, url, **kwargs)
        except httpx.HTTPError as error:
            # 仅确认尚未建立连接的异常允许重试；写入、读取及协议异常均可能已受理。
            before_send = isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
            log.warning("ComfyUI 请求网络异常，类型=%s", type(error).__name__)
            raise ProviderError("ComfyUI 网络请求失败",
                                retryable=not submitting or before_send,
                                uncertain=submitting and not before_send) from None
        log.info("ComfyUI 请求完成，方法=%s 状态码=%s", method, response.status_code)
        try:
            data = response.json()
        except ValueError:
            data = None
        if not response.is_success:
            body = data.get("error", {}) if isinstance(data, dict) else {}
            code = identifier(body.get("code")) if isinstance(body, dict) else None
            duplicate = code == "idempotency_key_reuse"
            uncertain = submitting and (duplicate or response.status_code >= 500 or response.is_redirect)
            transient = response.status_code == 429 or response.status_code >= 500
            raise ProviderError(
                "ComfyUI 提交结果需要人工核对，不会自动重提" if uncertain
                else f"ComfyUI 请求失败（HTTP {response.status_code}）",
                retryable=transient and not uncertain, uncertain=uncertain,
                details={"provider": "comfyui", "http_status": response.status_code, "code": code},
            )
        if not isinstance(data, dict):
            raise ProviderError("ComfyUI 返回了无效 JSON 对象", uncertain=submitting, retryable=not submitting)
        return data

    def submit(self, snapshot):
        from .continuity import validate_reference_file
        from .media import path_for
        try:
            workflow = workflow_for(self.config, snapshot, snapshot["seed"])
            refs = snapshot.get("references", [])
            for ref in refs:
                validate_reference_file(ref)
                if path_for(ref["path"]).stat().st_size > 29 * 1024 * 1024:
                    raise ValueError("参考图片超过 29 MiB")
        except (ValueError, KeyError, OSError):
            raise ProviderError("ComfyUI 输入或节点映射无效，请检查工作流能力及参考图片") from None
        image_index = 0
        for ref in refs:
            try:
                with path_for(ref["path"]).open("rb") as stream:
                    data = self.request("POST", "api/v2/assets",
                                        data={"content_type": ref["mime_type"], "file_path": path_for(ref["path"]).name},
                                        files={"file": (path_for(ref["path"]).name, stream, ref["mime_type"])})
            except OSError:
                raise ProviderError("ComfyUI 参考素材读取失败") from None
            asset_id = data.get("id")
            if not isinstance(asset_id, str) or not asset_id:
                raise ProviderError("ComfyUI 参考素材上传未返回资产 ID", retryable=True)
            if ref["role"] == "reference_image":
                binding = self.config["bindings"]["reference_images"][image_index]
                image_index += 1
            else:
                binding = self.config["bindings"][ref["role"]]
            workflow[binding["node_id"]]["inputs"][binding["input"]] = {
                "__type": "core/ASSET", "info": {"id": asset_id},
            }
        data = self.request("POST", "api/v2/jobs", submitting=True, json={"workflow": workflow},
                            headers={"Idempotency-Key": snapshot["submission_key"]})
        external_id = data.get("id")
        if not isinstance(external_id, str) or not external_id or len(external_id) > 150:
            raise ProviderError("ComfyUI 提交未返回有效任务 ID，需要人工核对", uncertain=True)
        try:
            self.job_url = self.service_url(data["urls"]["self"])
        except (KeyError, TypeError, ProviderError, ValueError):
            # 已拿到编号时保留它，使用标准查询入口，避免丢失已受理任务。
            self.job_url = self.service_url("api/v2/jobs/" + quote(external_id, safe=""))
        return external_id

    def poll(self, external_id):
        data = self.request("GET", self.job_url or "api/v2/jobs/" + quote(external_id, safe=""))
        if data.get("id") != external_id:
            raise ProviderError("ComfyUI 返回的任务 ID 与当前任务不一致")
        status = data.get("status")
        if status in ("queued", "running", "canceling"):
            return None
        if status in ("failed", "canceled", "expired"):
            error = data.get("error") or {}
            raise ProviderError("ComfyUI 视频任务已失败、取消或过期", terminal=True,
                                details={"provider": "comfyui", "status": status,
                                         "code": identifier(error.get("code")) if isinstance(error, dict) else None})
        if status != "succeeded":
            raise ProviderError("ComfyUI 视频任务返回未知状态")
        outputs = data.get("outputs")
        if not isinstance(outputs, list):
            raise ProviderError("ComfyUI 成功任务缺少视频输出")
        videos = [output for output in outputs if isinstance(output, dict)
                  and output.get("node_id") == self.config["output_node_id"]
                  and isinstance(output.get("content_type"), str)
                  and output["content_type"].startswith("video/")
                  and output.get("type") in ("video", "file")]
        index = self.config.get("output_index", 0)
        if index >= len(videos) or not isinstance(videos[index].get("url"), str) or not videos[index]["url"]:
            raise ProviderError("ComfyUI 指定节点没有所选视频输出，请核对输出节点和序号")
        return {"video_url": videos[index]["url"], "model": "comfyui",
                "usage": data.get("metrics") or {}}

    def download(self, url, destination):
        from .media import allowed_url, probe
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name("视频下载临时文件")
        # 下载客户端不带默认认证；每次跳转重新判断，只给原服务 API 携带 Bearer。
        with httpx.Client(timeout=httpx.Timeout(120, connect=15), follow_redirects=False) as client:
            try:
                for _ in range(4):
                    resolved = urljoin(self.base_url, url)
                    parsed, base = urlsplit(resolved), urlsplit(self.base_url)
                    same_origin = (parsed.scheme, parsed.hostname, parsed.port) == (base.scheme, base.hostname, base.port)
                    headers = {}
                    if same_origin:
                        resolved = self.service_url(resolved)
                        headers = {"Authorization": self.client.headers["Authorization"]}
                    else:
                        allowed_url(resolved)
                    with client.stream("GET", resolved, headers=headers) as response:
                        if response.is_redirect:
                            url = urljoin(resolved, response.headers["location"])
                            continue
                        response.raise_for_status()
                        size = 0
                        with temporary.open("wb") as output:
                            for chunk in response.iter_bytes():
                                size += len(chunk)
                                if size > 300 * 1024 * 1024:
                                    raise ValueError("ComfyUI 视频超过 300 MiB")
                                output.write(chunk)
                        probe(temporary)
                        temporary.replace(destination)
                        return
                raise ValueError("ComfyUI 视频下载跳转次数过多")
            except httpx.HTTPError:
                raise ProviderError("ComfyUI 视频下载失败，将继续从原任务读取结果", retryable=True) from None
            finally:
                temporary.unlink(missing_ok=True)
