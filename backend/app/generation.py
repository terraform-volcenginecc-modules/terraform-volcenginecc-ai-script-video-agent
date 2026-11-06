"""Shared, side-effect-free generation parameters and prompt construction."""
from .schemas import VideoSettings


def video_settings(project):
    return VideoSettings.model_validate(project.video_settings or {}).model_dump()


def output_settings(settings):
    return {k: v for k, v in VideoSettings.model_validate(settings or {}).model_dump().items()
            if k != "execution_expires_after"}


def validate_settings(model, settings):
    if settings["resolution"] == "1080p" and model.startswith(
        ("doubao-seedance-2-0-fast", "doubao-seedance-2-0-mini")
    ):
        raise ValueError("当前 fast / mini 模型不支持 1080p，请选择 480p 或 720p")
    if not model.startswith(("doubao-seedance-2-0", "doubao-seedance-2-5")):
        if not model.startswith("doubao-seedance-1-0-pro") and output_settings(settings) != output_settings({}):
            raise ValueError("未知模型或 Endpoint 的能力尚未配置；请使用 Seedance Model ID 或默认视频规格")
        if model.startswith("doubao-seedance-1-0-pro") and settings["generate_audio"]:
            raise ValueError("Seedance 1.0 不支持生成音频，请关闭声音")


def build_prompt(snapshot):
    shot, script = snapshot["shot"], snapshot["script"]
    characters = [
        f"{c['name']}：{c['description']}" for c in script.get("characters", [])
        if c["name"] in shot["characters"]
    ]
    parts = [
        ("画面比例（以此为准）", snapshot.get("ratio", "16:9")),
        ("视觉风格", script.get("style_bible", "")),
        ("角色", "；".join(characters)),
        ("场景", shot["scene"]),
        ("起始状态", shot.get("start_state", "")),
        ("动作", shot["action"]),
        ("运镜", shot["camera"]),
        ("结束状态", shot.get("end_state", "")),
        ("连续性", shot.get("continuity_notes", "")),
    ]
    if snapshot.get("video_settings", {}).get("generate_audio", True):
        parts += [("对白", f'"{shot["dialogue"]}"' if shot.get("dialogue") else ""),
                  ("声音", shot.get("sound", ""))]
    return "\n".join(f"{label}：{value}" for label, value in parts if value)
