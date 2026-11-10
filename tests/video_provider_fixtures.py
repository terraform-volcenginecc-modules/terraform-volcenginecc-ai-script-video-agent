"""只供本地假服务验证的数据，不是可部署的 ComfyUI 工作流。"""
from copy import deepcopy


def 服务配置(base_url, key_file):
    # 节点名和类名故意标记为假数据；假服务只验证协议，不执行这个图。
    return {
        "base_url": base_url, "api_key_file": str(key_file),
        "workflow": {
            "假输入": {"class_type": "仅供假服务测试", "inputs": {
                "提示": "", "秒数": 4, "宽": 1280, "高": 720, "种子": 0,
                "声音": True, "水印": False, "首帧": "", "尾帧": "", "参考图片": "",
            }},
            "假输出": {"class_type": "仅供假服务输出", "inputs": {"视频": ["假输入", 0]}},
        },
        "bindings": {
            name: {"node_id": "假输入", "input": field}
            for name, field in (
                ("prompt", "提示"), ("duration", "秒数"), ("width", "宽"),
                ("height", "高"), ("seed", "种子"), ("generate_audio", "声音"),
                ("watermark", "水印"), ("first_frame", "首帧"), ("last_frame", "尾帧"),
            )
        },
        "output_node_id": "假输出",
        "capabilities": {"modes": ["text", "first_frame", "first_last_frame"],
                         "resolutions": ["480p", "720p"], "ratios": ["16:9", "9:16"],
                         "min_duration": 4, "max_duration": 15,
                         "generate_audio": [True, False], "watermark": [False, True]},
    }


def 视频快照(config):
    return {
        "video_provider": "comfyui", "comfyui": deepcopy(config), "seed": 42,
        "submission_key": "fake-submit-key",
        "video_model": "本地假服务模型", "script_model": "本地假服务剧本",
        "ratio": "16:9", "video_settings": {"resolution": "720p", "generate_audio": True, "watermark": False},
        "generation_config": {"mode": "text"}, "references": [], "reference": None,
        "prompt": "仅用于假服务，不执行模型",
        "script": {"title": "假服务剧本", "synopsis": "本地协议验证",
                   "style_bible": "仅供本地验证", "characters": [], "shots": []},
        "shot": {"duration": 4, "scene": "假场景", "characters": [], "action": "假动作", "camera": "固定"},
        "project": {"name": "假项目", "brief": "本地协议验证", "style": "", "characters": "",
                    "duration": 4, "ratio": "16:9"},
    }
