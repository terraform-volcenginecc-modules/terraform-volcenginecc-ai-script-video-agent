"""ComfyUI 工作流契约与能力校验，不执行网络或文件操作。"""
from copy import deepcopy
from urllib.parse import urlsplit

from .schemas import VideoSettings

REQUIRED_BINDINGS = {"prompt", "duration", "width", "height", "seed"}
OPTIONAL_BINDINGS = {"generate_audio", "watermark", "first_frame", "last_frame", "reference_images"}


def validate_config(config):
    try:
        base = urlsplit(config["base_url"])
        if (base.scheme not in ("https", "http") or not base.hostname or base.username
                or base.password or base.query or base.fragment):
            raise ValueError("ComfyUI 地址须为无凭据、无查询参数的 HTTP(S) 服务地址")
        base.port
        workflow = config["workflow"]
        if not isinstance(workflow, dict) or not workflow or "nodes" in workflow or "links" in workflow:
            raise ValueError("必须提供真实的 ComfyUI API 格式 Workflow JSON，不能使用画布导出格式")
        for node in workflow.values():
            if (not isinstance(node, dict) or not isinstance(node.get("class_type"), str)
                    or not node["class_type"] or not isinstance(node.get("inputs"), dict)):
                raise ValueError("Workflow 节点必须包含 class_type 和 inputs")
        bindings = config["bindings"]
        if (not isinstance(bindings, dict) or not REQUIRED_BINDINGS <= bindings.keys()
                or bindings.keys() - REQUIRED_BINDINGS - OPTIONAL_BINDINGS):
            raise ValueError("节点映射必须包含 prompt、duration、width、height、seed，且不能包含未知映射")
        targets = set()
        for name, binding in bindings.items():
            items = binding if name == "reference_images" else [binding]
            if not isinstance(items, list) or not items or len(items) > 9:
                raise ValueError("参考图片映射须为 1 到 9 个节点字段")
            for item in items:
                if not isinstance(item, dict) or set(item) != {"node_id", "input"}:
                    raise ValueError("每个节点映射必须且只能包含 node_id 和 input")
                node_id, field = item["node_id"], item["input"]
                if (not isinstance(node_id, str) or not isinstance(field, str)
                        or node_id not in workflow or field not in workflow[node_id]["inputs"]):
                    raise ValueError("节点映射指向不存在的节点或输入字段")
                if (node_id, field) in targets:
                    raise ValueError("不同参数不能覆盖同一个 Workflow 输入字段")
                targets.add((node_id, field))
        if config["output_node_id"] not in workflow:
            raise ValueError("视频输出节点不在 Workflow 中")
        index = config.get("output_index", 0)
        if type(index) is not int or index < 0:
            raise ValueError("视频输出序号须为非负整数")
        caps = config["capabilities"]
        modes = caps["modes"]
        mode_bindings = {"text": set(), "first_frame": {"first_frame"},
                         "first_last_frame": {"first_frame", "last_frame"},
                         "reference": {"reference_images"}}
        if (not isinstance(modes, list) or not modes or any(
                mode not in mode_bindings or not mode_bindings[mode] <= bindings.keys() for mode in modes)):
            raise ValueError("必须明确声明支持的参考模式，并为每种模式配置输入映射")
        for name, allowed in (
            ("resolutions", {"480p", "720p", "1080p"}),
            ("ratios", {"16:9", "9:16", "1:1", "4:3", "3:4", "21:9"}),
        ):
            values = caps[name]
            if not isinstance(values, list) or not values or not set(values) <= allowed:
                raise ValueError("必须明确声明工作流支持的画幅和分辨率")
        for name in ("generate_audio", "watermark"):
            values = caps[name]
            if (not isinstance(values, list) or not values
                    or any(type(v) is not bool for v in values)
                    or (name not in bindings and len(values) != 1)):
                raise ValueError("声音和水印须声明布尔能力；可切换能力必须映射输入字段")
        low, high = caps["min_duration"], caps["max_duration"]
        if type(low) is not int or type(high) is not int or not 4 <= low <= high <= 15:
            raise ValueError("工作流时长范围须为 4 到 15 秒内的整数")
        if "last_frame" in bindings and "first_frame" not in bindings:
            raise ValueError("尾帧映射必须配套首帧映射")
    except (KeyError, TypeError, AttributeError):
        raise ValueError("ComfyUI 配置缺少地址、真实工作流、节点映射、输出节点或能力声明") from None
    return config


def validate_request(config, settings, ratio=None, duration=None, generation=None, references=None):
    validate_config(config)
    settings = VideoSettings.model_validate(settings or {}).model_dump()
    caps, bindings = config["capabilities"], config["bindings"]
    for setting, capability in (
        ("resolution", "resolutions"), ("generate_audio", "generate_audio"), ("watermark", "watermark"),
    ):
        if settings[setting] not in caps[capability]:
            raise ValueError(f"当前 ComfyUI 工作流不支持所选 {setting}，请按能力声明调整项目设置")
    if ratio is not None and ratio not in caps["ratios"]:
        raise ValueError("当前 ComfyUI 工作流不支持所选画幅")
    if duration is not None and not caps["min_duration"] <= duration <= caps["max_duration"]:
        raise ValueError("镜头时长超出当前 ComfyUI 工作流能力")
    # 创建项目时仅校验输出规格，参考模式要等分镜配置完成后再检查。
    if generation is None and references is None:
        return
    mode = (generation or {}).get("mode", "text")
    required = {"text": set(), "first_frame": {"first_frame"},
                "first_last_frame": {"first_frame", "last_frame"},
                "reference": {"reference_images"}}.get(mode)
    if required is None or mode not in caps["modes"] or not required <= bindings.keys():
        raise ValueError("当前 ComfyUI 工作流没有所选参考模式的节点映射")
    refs = references or []
    if any(ref["type"] != "image_url" for ref in refs):
        raise ValueError("本阶段 ComfyUI 仅支持本地图片参考，不支持远端视频或音频参考")
    expected_roles = {"text": {}, "first_frame": {"first_frame": 1},
                      "first_last_frame": {"first_frame": 1, "last_frame": 1},
                      "reference": {"reference_image": len(refs)}}[mode]
    actual_roles = {role: sum(r["role"] == role for r in refs) for role in expected_roles}
    if references is not None and (actual_roles != expected_roles or sum(actual_roles.values()) != len(refs)):
        raise ValueError("参考素材与所选模式不一致")
    if mode == "reference" and references is not None and len(refs) != len(bindings["reference_images"]):
        raise ValueError("参考图片数量须与工作流参考图片映射数量一致")


def workflow_for(config, snapshot, seed):
    from .generation import build_prompt
    from .media import output_size
    settings = VideoSettings.model_validate(snapshot.get("video_settings", {})).model_dump()
    validate_request(config, settings, snapshot["ratio"], snapshot["shot"]["duration"],
                     snapshot.get("generation_config"), snapshot.get("references", []))
    width, height = output_size(snapshot["ratio"], settings["resolution"])
    values = {"prompt": snapshot.get("prompt") or build_prompt(snapshot),
              "duration": snapshot["shot"]["duration"], "width": width, "height": height,
              "seed": seed, "generate_audio": settings["generate_audio"], "watermark": settings["watermark"]}
    workflow = deepcopy(config["workflow"])
    for name, value in values.items():
        if name in config["bindings"]:
            binding = config["bindings"][name]
            workflow[binding["node_id"]]["inputs"][binding["input"]] = value
    return workflow
