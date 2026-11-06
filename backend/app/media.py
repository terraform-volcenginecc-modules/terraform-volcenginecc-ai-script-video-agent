import ipaddress
import hashlib
import json
import os
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import MEDIA

SIZES = {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (720, 720),
         "4:3": (960, 720), "3:4": (720, 960), "21:9": (1680, 720)}

def output_size(ratio, resolution="720p"):
    heights = {"480p": 480, "720p": 720, "1080p": 1080}
    short = heights[resolution]
    width, height = SIZES[ratio]
    return (round(short * width / min(width, height) / 2) * 2,
            round(short * height / min(width, height) / 2) * 2)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def image_info(path):
    data = json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)]))
    streams = data.get("streams", [])
    if len(streams) != 1 or streams[0].get("codec_name") not in ("png", "mjpeg"):
        raise ValueError("参考帧必须是 PNG 或 JPEG 图片")
    stream = streams[0]
    width, height = stream["width"], stream["height"]
    if not (300 <= width <= 6000 and 300 <= height <= 6000 and .4 <= width / height <= 2.5):
        raise ValueError("参考帧尺寸不符合模型要求")
    return {"width": width, "height": height, "sha256": digest(path),
            "mime_type": "image/png" if stream["codec_name"] == "png" else "image/jpeg"}


def path_for(relative: str) -> Path:
    path = (MEDIA / relative).resolve()
    if not path.is_relative_to(MEDIA.resolve()):
        raise ValueError("非法素材路径")
    return path


def run(args):
    result = subprocess.run(args, capture_output=True, timeout=900)
    if result.returncode:
        raise ValueError("媒体处理失败，请检查素材格式和本地 FFmpeg")
    return result.stdout


def probe(path):
    data = json.loads(run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]))
    duration = float(data["format"]["duration"])
    if not 0 < duration <= 900 or not any(s["codec_type"] == "video" for s in data["streams"]):
        raise ValueError("素材时长或视频轨道无效")
    return duration, data


def allowed_url(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("模型视频必须来自 HTTPS 地址")
    hostname = parsed.hostname or ""
    # Operator-controlled exact host suffixes; never accept a user-supplied download URL.
    suffixes = os.getenv("MEDIA_ALLOWED_HOSTS", ".volces.com,.volcengine.com,.bytecdn.cn,.ibytedtos.com").split(",")
    if not any(hostname == s.lstrip(".") or (s.startswith(".") and hostname.endswith(s)) for s in suffixes):
        raise ValueError("模型视频域名未授权，请管理员配置 MEDIA_ALLOWED_HOSTS")
    addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("模型视频地址不是公网地址")


def stable_reference_url(url):
    parsed = urlparse(url)
    if parsed.query or parsed.fragment:
        raise ValueError("参考地址须为无签名的长期 HTTPS 地址，不支持临时链接")
    allowed_url(url)


def download(url, destination, image=False):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".download")
    try:
        with httpx.Client(timeout=120, follow_redirects=False) as client:
            for _ in range(4):
                allowed_url(url)
                with client.stream("GET", url) as response:
                    if response.is_redirect:
                        from urllib.parse import urljoin
                        url = urljoin(url, response.headers["location"])
                        continue
                    response.raise_for_status()
                    size = 0
                    with temporary.open("wb") as output:
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > (29 if image else 300) * 1024 * 1024:
                                raise ValueError("媒体素材超过大小限制")
                            output.write(chunk)
                    image_info(temporary) if image else probe(temporary)
                    temporary.replace(destination)
                    return
            raise ValueError("视频地址重定向次数过多")
    finally:
        temporary.unlink(missing_ok=True)


def normalize(source, target, ratio, out_frame=None, resolution="720p", audio=True):
    """The frame selector and compositor share this exact CFR24 transform."""
    width, height = output_size(ratio, resolution)
    duration, info = probe(source)
    has_audio = any(s["codec_type"] == "audio" for s in info["streams"])
    args = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(source)]
    if audio and not has_audio:
        args += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
    video_filter = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24"
    if out_frame is not None:
        if out_frame <= 0 or out_frame > round(duration * 24) + 1:
            raise ValueError("剪辑出点超出素材范围")
        video_filter += f",trim=end_frame={out_frame},setpts=PTS-STARTPTS"
        duration = min(duration, out_frame / 24)
    args += ["-map", "0:v:0", "-vf", video_filter]
    if audio:
        args += ["-map", "0:a:0" if has_audio else "1:a:0",
                 "-af", "aresample=48000,loudnorm=I=-16:TP=-1.5:LRA=11",
                 "-c:a", "aac", "-ar", "48000", "-ac", "2"]
    else:
        args += ["-an"]
    args += [
        "-c:v", "libx264", "-threads", "2", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-t", str(duration),
        "-movflags", "+faststart", str(target),
    ]
    run(args)


def reference_frame(source, destination, ratio, frame_index=None):
    destination.parent.mkdir(parents=True, exist_ok=True)
    canonical = destination.with_name("source-24fps.mp4")
    normalize(source, canonical, ratio)
    _, info = probe(canonical)
    stream = next(s for s in info["streams"] if s["codec_type"] == "video")
    count = int(stream["nb_frames"])
    index = count - 1 if frame_index is None else frame_index
    if index < 0 or index >= count:
        raise ValueError(f"请选择 0 到 {count - 1} 帧之间的出点")
    run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(canonical),
         "-vf", f"select=eq(n\\,{index})", "-frames:v", "1", str(destination)])
    metadata = image_info(destination)
    metadata.update(frame_index=index, out_frame_exclusive=index + 1, fps=24,
                    frame_count=count, time=index / 24)
    return metadata, canonical


def compose(inputs, destination, ratio, cuts=None, resolution="720p", audio=True):
    destination.parent.mkdir(parents=True, exist_ok=True)
    normalized = []
    for index, source in enumerate(inputs):
        target = destination.parent / f"normalized-{index}.mp4"
        cut = cuts[index] if cuts else {}
        normalize(source, target, ratio, cut.get("out_frame_exclusive"), resolution, audio)
        normalized.append(target)
    manifest = destination.parent / "concat.txt"
    # Paths are generated from UUIDs and numeric indexes, never user filenames.
    manifest.write_text("".join(f"file '{p.name}'\n" for p in normalized))
    temporary = destination.with_name("composed.tmp.mp4")
    run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "concat", "-safe", "1",
         "-i", str(manifest), "-c", "copy", "-movflags", "+faststart", str(temporary)])
    probe(temporary)
    temporary.replace(destination)
    for path in normalized:
        path.unlink(missing_ok=True)
    manifest.unlink(missing_ok=True)
