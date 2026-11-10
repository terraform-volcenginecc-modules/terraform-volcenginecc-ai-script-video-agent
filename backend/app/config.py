import json
import os
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote


def secret(name: str, default: str = "") -> str:
    path = Path(os.getenv(f"{name.upper()}_FILE", f"/run/secrets/{name}"))
    return path.read_text().strip() if path.is_file() else default


def runtime() -> dict:
    path = Path(os.getenv("RUNTIME_CONFIG", "/config/runtime.json"))
    return json.loads(path.read_text()) if path.is_file() else {}


def models() -> dict:
    defaults = {
        "ark_base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "script_model_id": "doubao-seed-2-1-pro-260628",
        "video_model_id": "doubao-seedance-2-0-260128",
    }
    defaults.update({k: v for k, v in runtime().get("models", {}).items() if k in defaults})
    for key in defaults:
        defaults[key] = os.getenv(key.upper(), defaults[key])
    return defaults


def ark_key():
    path = runtime().get("models", {}).get("api_key_file")
    # Native fallback uses an explicit file override; Compose follows runtime.json.
    if os.getenv("ARK_API_KEY_FILE"):
        return secret("ark_api_key")
    return Path(path).read_text().strip() if path and Path(path).is_file() else secret("ark_api_key")


def video_provider() -> str:
    name = runtime().get("video_provider", "ark")
    if name not in ("ark", "comfyui"):
        raise ValueError("video_provider 只能选择 ark 或 comfyui")
    return name


def comfyui_config() -> dict:
    from .comfyui_config import validate_config
    config = deepcopy(runtime().get("comfyui", {}))
    validate_config(config)
    return config


def comfyui_key(config) -> str:
    if os.getenv("COMFYUI_API_KEY_FILE"):
        return secret("comfyui_api_key")
    path = Path(config.get("api_key_file", "/run/secrets/comfyui_api_key"))
    return path.read_text().strip() if path.is_file() else ""


def validate_video_config():
    if video_provider() == "comfyui":
        config = comfyui_config()
        key = comfyui_key(config)
        if not key or len(key) > 16384 or any(c in key for c in "\n\r\x00"):
            raise ValueError("选择 ComfyUI 时必须提供有效的单行认证密钥文件")
        if not runtime().get("storage", {}).get("auto_upload"):
            raise ValueError("选择 ComfyUI 时必须启用私有 TOS 归档")


def model_catalog():
    config = models()
    catalog = runtime().get("model_catalog", {})
    result = {}
    for kind, key in (("script", "script_model_id"), ("video", "video_model_id")):
        entries = [{"id": config[key], "name": "工作室默认 · " + config[key]}]
        for entry in catalog.get(kind, [])[:50]:
            if (isinstance(entry, dict) and isinstance(entry.get("id"), str)
                    and 0 < len(entry["id"]) <= 150 and entry["id"] not in {e["id"] for e in entries}):
                entries.append({"id": entry["id"], "name": str(entry.get("name") or entry["id"])[:150]})
        result[kind] = entries
    return result


def database_url() -> str:
    if os.getenv("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    password = secret("postgres_password")
    if not password:
        raise RuntimeError("缺少 PostgreSQL 密钥文件")
    host = os.getenv("DATABASE_HOST", "postgres")
    return f"postgresql+psycopg://animation:{quote(password, safe='')}@{host}:5432/animation"


MEDIA = Path(os.getenv("MEDIA_ROOT", "/work/media"))
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "10"))
