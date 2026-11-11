#!/usr/bin/env python3
"""下载固定版本的官方 ComfyUI，业务扩展与官方源码分开维护。"""
import argparse
import hashlib
from http.client import HTTPException
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import time
import urllib.request

PYPI_INDEXES = ("https://mirrors.ivolces.com/pypi/simple/", "https://pypi.org/simple/")
CPU_INDEXES = ("https://mirror.sjtu.edu.cn/pytorch-wheels/cpu/", "https://download.pytorch.org/whl/cpu")


def install_packages(python, requirements, indexes=PYPI_INDEXES):
    for index in indexes:
        print(f"从 {index} 安装 Python 依赖", flush=True)
        try:
            subprocess.run([str(python), "-m", "pip", "--isolated", "install",
                            "--disable-pip-version-check", "--retries", "2", "--timeout", "20",
                            "--index-url", index, *requirements], check=True, timeout=600)
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            print(f"Python 依赖安装失败：{index} ({type(exc).__name__})", flush=True)
    raise RuntimeError("所有 Python 下载源均安装失败")


def download_source(component, archive):
    urls = (component["source_url"], *component.get("fallback_source_urls", ()))
    for url in urls:
        for attempt in (1, 2):
            print(f"下载 ComfyUI 源码：{url}（第 {attempt} 次）", flush=True)
            try:
                with urllib.request.urlopen(url, timeout=45) as response, archive.open("wb") as stream:
                    shutil.copyfileobj(response, stream)
                with archive.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != component["sha256"]:
                        raise ValueError("SHA-256 校验失败")
                return
            except (OSError, HTTPException, ValueError) as exc:
                archive.unlink(missing_ok=True)
                print(f"ComfyUI 源码下载失败：{exc}", flush=True)
                if attempt == 1:
                    time.sleep(3)
    raise RuntimeError("无法下载通过 SHA-256 校验的 ComfyUI 源码")


def install(release):
    component = json.loads((release / "scripts/comfyui-release.json").read_text())["comfyui"]
    target = release / "ComfyUI"
    if not target.is_dir():
        with TemporaryDirectory(prefix="comfyui-", dir=release) as staging:
            staging = Path(staging)
            archive = staging / "comfyui.tar.gz"
            download_source(component, archive)
            with tarfile.open(archive, "r:gz") as bundle:
                members = bundle.getmembers()
                if sum(m.size for m in members) > 300 * 1024 * 1024:
                    raise ValueError("ComfyUI 解压大小超过限制")
                for member in members:
                    parts = Path(member.name).parts
                    if not parts or ".." in parts or member.name.startswith("/") or member.issym() or member.islnk():
                        raise ValueError("ComfyUI 压缩包含无效路径")
                bundle.extractall(staging, filter="data")
            (staging / f"ComfyUI-{component['version']}").rename(target)
    python = release / ".comfyui-venv/bin/python"
    marker = release / ".comfyui-installed"
    if not marker.exists():
        subprocess.run([sys.executable, "-m", "venv", str(python.parents[1])], check=True)
        if sys.platform == "linux":
            vision = "torchvision==0.23.0" if platform.machine() == "aarch64" else "torchvision==0.23.0+cpu"
            install_packages(python, ["--no-deps", "--only-binary=:all:", "torch==2.8.0+cpu",
                                      vision], CPU_INDEXES)
        lock = release / "scripts/comfyui-requirements.txt"
        install_packages(python, ["--only-binary=:all:", "-r", str(lock)])
        subprocess.run([str(python), "-m", "pip", "check"], check=True)
        marker.touch()
    extension = target / "custom_nodes/ai_animation"
    extension.mkdir(exist_ok=True)
    shutil.copy2(release / "backend/comfyui_extension.py", extension / "__init__.py")
    # Replace extension assets so an earlier sidebar script cannot remain auto-loaded.
    if (extension / "web").exists():
        shutil.rmtree(extension / "web")
    shutil.copytree(release / "frontend/comfyui-extension", extension / "web", dirs_exist_ok=True)
    shutil.copytree(release / "workflows", extension / "example_workflows", dirs_exist_ok=True)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="安装官方 ComfyUI 与本项目业务扩展")
    parser.add_argument("--release", type=Path, required=True)
    install(parser.parse_args().release.resolve())
