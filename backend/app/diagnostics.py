"""Allowlisted, bounded diagnostics; never persist complete provider responses."""
import re

from .config import ark_key, secret


def safe_text(value, limit=1600):
    if not isinstance(value, str):
        return None
    # Redact known credentials even when echoed without a label.
    for credential in (ark_key(), secret("owner_password"), secret("session_key"),
                       secret("postgres_password")):
        if credential:
            value = value.replace(credential, "[已隐藏]")
    value = re.sub(r"data:[^\s\"<>]+", "[素材数据已隐藏]", value, flags=re.I)
    value = re.sub(r"https?://[^\s\"<>]+", "[地址已隐藏]", value, flags=re.I)
    value = re.sub(r"\bBearer\s+\S+", "Bearer [已隐藏]", value, flags=re.I)
    value = re.sub(
        r"""(?i)\b(api[_-]?key|authorization|password|secret|token)\b["']?\s*[:=]\s*["']?[^,\s"'}]+""",
        r"\1=[已隐藏]", value,
    )
    value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", value)
    return value[:limit] or None


def identifier(value):
    value = safe_text(value, 200)
    return value if value and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value) else None


def explanation(code, message, http_status=None, status=None):
    code = (code or "").lower()
    message = (message or "").lower()
    if "privacyinformation" in code:
        return "参考素材被云端判定涉及真人或隐私信息。请检查素材；AI 生成人物也可能触发此审核。"
    if "copyright" in message:
        return "云端判定生成内容可能涉及版权限制。请检查品牌、角色及其他可能受保护的内容。"
    if "sensitivecontent" in code or "policyviolation" in code:
        return "输入素材或生成内容未通过云端内容审核。请结合具体原因检查提示词和参考素材。"
    if http_status in (401, 403) or "authentication" in code or "permission" in code:
        return "模型服务鉴权或权限校验失败。请检查 API Key、模型开通状态和账号权限。"
    if http_status == 429:
        return "模型服务触发限流或配额限制。请检查配额及并发设置，稍后重试。"
    if http_status and http_status >= 500:
        return "模型服务暂时异常。提交结果待核对时，请先到方舟确认任务，避免重复计费。"
    if status == "expired":
        return "云端任务已过期，未完成生成。请核对任务时限和云端状态。"
    if status == "cancelled":
        return "云端任务已取消。"
    if http_status == 400:
        return "云端拒绝了生成请求。请结合错误码和具体原因检查参数、模型及参考素材。"
    return "模型任务未完成，请查看云端具体原因；如需排查，可复制诊断信息。"


def provider_details(data, *, http_status=None, headers=None, status=None):
    data = data if isinstance(data, dict) else {}
    error = data.get("error")
    error = error if isinstance(error, dict) else {}
    code = identifier(error.get("code") or data.get("code"))
    message = safe_text(error.get("message") or data.get("message"))
    headers = headers or {}
    request_id = identifier(
        error.get("request_id") or error.get("requestId") or data.get("request_id")
        or data.get("requestId") or headers.get("x-request-id") or headers.get("x-tt-logid")
    )
    if not request_id and message:
        match = re.search(r"(?i)request[_ -]?id\s*[:=]\s*([A-Za-z0-9_.:-]+)", message)
        request_id = identifier(match.group(1)) if match else None
    return {
        "source": "ark", "code": code, "message": message, "request_id": request_id,
        "http_status": http_status, "provider_status": identifier(status),
        "summary": explanation(code, message, http_status, status),
    }
