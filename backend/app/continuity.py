"""Stable reference bindings shared by API submission and task recovery."""
from .db import Asset, Shot
from .media import digest, image_info, path_for


def generation_config(shot):
    return shot.generation_config or {"mode": "text"}


def reference_current(db, reference):
    metadata = reference.metadata_json
    if metadata.get("origin") == "uploaded":
        return True
    source = db.get(Shot, metadata.get("source_shot_id"))
    return bool(source and source.asset_id == metadata.get("source_asset_id") and
                source.edit_config.get("out_frame_exclusive") == metadata.get("out_frame_exclusive") and
                source.edit_config.get("asset_id") == source.asset_id)


def reference_snapshot(db, shot, ratio):
    config = generation_config(shot)
    if config["mode"] not in ("first_frame", "first_last_frame"):
        return None
    reference = db.get(Asset, config.get("reference_asset_id"))
    if not reference or reference.project_id != shot.project_id or reference.kind != "image":
        raise ValueError("参考帧不存在或不属于当前项目")
    if not reference_current(db, reference):
        raise ValueError("上一镜头或剪辑出点已变化，请重新选帧确认")
    info = image_info(path_for(reference.path))
    if info["sha256"] != reference.metadata_json["sha256"]:
        raise ValueError("参考帧内容已变化，请重新选帧")
    from .media import SIZES
    width, height = SIZES[ratio]
    if abs(info["width"] / info["height"] / (width / height) - 1) >= .01:
        raise ValueError("首帧比例与项目画幅不符，请重新上传或提取参考帧")
    if reference.metadata_json.get("origin") != "uploaded" and (info["width"], info["height"]) != SIZES[ratio]:
        raise ValueError("参考帧与项目画幅不匹配")
    return {"asset_id": reference.id, "path": reference.path, **reference.metadata_json}


def references_snapshot(db, shot, ratio):
    config = generation_config(shot)
    refs = []
    first = reference_snapshot(db, shot, ratio)
    if first:
        refs.append({**first, "role": "first_frame", "type": "image_url"})
    ids = ([config["last_frame_asset_id"]] if config.get("last_frame_asset_id") else [])
    ids += config.get("reference_image_ids", [])
    for asset_id in ids:
        asset = db.get(Asset, asset_id)
        if not asset or asset.project_id != shot.project_id or asset.kind != "image":
            raise ValueError("参考图片不存在或不属于当前项目")
        info = image_info(path_for(asset.path))
        if info["sha256"] != asset.metadata_json.get("sha256"):
            raise ValueError("参考图片内容已变化")
        role = "last_frame" if asset_id == config.get("last_frame_asset_id") else "reference_image"
        if role == "last_frame" and first and (info["width"], info["height"]) != (first["width"], first["height"]):
            raise ValueError("首帧和尾帧尺寸必须一致")
        refs.append({"asset_id": asset.id, "path": asset.path, **info, "role": role, "type": "image_url"})
    from .media import stable_reference_url
    for remote in config.get("remote_references", []):
        stable_reference_url(remote["url"])
        refs.append({**remote, "type": remote["kind"] + "_url", "role": "reference_" + remote["kind"]})
    return refs


def needs_generation(db, shot, project=None):
    if not shot.asset_id:
        return True
    asset = db.get(Asset, shot.asset_id)
    if not asset or asset.project_id != shot.project_id:
        return True
    from .schemas import GenerationInput, VideoSettings
    recorded = asset.metadata_json.get("generation_config", {"mode": "text"})
    if GenerationInput.model_validate(recorded) != GenerationInput.model_validate(generation_config(shot)):
        return True
    if project is not None:
        from .generation import output_settings, video_settings
        if asset.metadata_json.get("video_model", project.video_model) != project.video_model or asset.metadata_json.get("ratio", project.ratio) != project.ratio:
            return True
        return output_settings(asset.metadata_json.get("video_settings", {})) != output_settings(video_settings(project))
    return False


def validate_reference_file(reference):
    if reference:
        path = path_for(reference["path"])
        if not path.is_file() or digest(path) != reference["sha256"]:
            raise ValueError("任务参考帧丢失或内容变化，请重新确认参考素材")
