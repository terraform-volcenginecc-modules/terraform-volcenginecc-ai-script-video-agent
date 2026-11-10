#!/usr/bin/env python3
"""从本机执行完整 apply；凭据只注入子进程，State 与日志保存在工程外。"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def deploy(args):
    key = args.ark_key_file.read_text().strip()
    if not key or any(char in key for char in "\r\n\x00"):
        raise ValueError("方舟 API Key 文件为空或格式无效")
    config = json.loads(args.cloud_config.read_text())
    profile = config["profiles"][args.profile or config["current"]]
    if profile.get("mode") != "ak" or not profile.get("access-key") or not profile.get("secret-key"):
        raise ValueError("本地部署要求提供有效的云账号 AK/SK 配置")
    target = args.work_dir.resolve()
    if target == ROOT or ROOT in target.parents:
        raise ValueError("部署 State 和日志目录必须位于工程外")
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(0o700)
    for path in ROOT.glob("*.tf"):
        shutil.copy2(path, target / path.name)
    for name in ("terraform.tfvars", "开源组件.json"):
        shutil.copy2(ROOT / name, target / name)
    for name in ("backend", "frontend", "scripts"):
        link = target / name
        if not link.exists():
            link.symlink_to(ROOT / name, target_is_directory=True)
        elif link.resolve() != ROOT / name:
            raise ValueError("部署目录已有其他工程，请使用独立目录")
    environment = {**os.environ, "VOLCENGINE_ACCESS_KEY": profile["access-key"],
                   "VOLCENGINE_SECRET_KEY": profile["secret-key"], "TF_VAR_ark_api_key": key}
    if args.ecs_password_file:
        password = args.ecs_password_file.read_text().strip()
        if not password or any(char in password for char in "\r\n\x00"):
            raise ValueError("ECS 登录密码文件为空或格式无效")
        environment["TF_VAR_ecs_password"] = password
    # 参数文件里的同名值会覆盖环境变量，因此拒绝未清理的重复配置。
    import re
    if re.search(r"^\s*ark_api_key\s*=", (target / "terraform.tfvars").read_text(), re.M):
        raise ValueError("请移除 terraform.tfvars 中的 ark_api_key 配置，本脚本从密钥文件直接注入")
    if (re.search(r'^\s*ecs_login_mode\s*=\s*"password"', (target / "terraform.tfvars").read_text(), re.M)
            and not environment.get("TF_VAR_ecs_password")):
        raise ValueError("密码登录必须通过 --ecs-password-file 或 TF_VAR_ecs_password 提供密码")
    if args.ecs_password_file and re.search(r"^\s*ecs_password\s*=", (target / "terraform.tfvars").read_text(), re.M):
        raise ValueError("请移除 terraform.tfvars 中的 ecs_password 配置，避免覆盖密码文件参数")
    for command in (["terraform", "init", "-input=false"],
                    ["terraform", "validate"],
                    ["terraform", "plan", "-input=false", "-out=完整部署.tfplan"]):
        result = subprocess.run(command, cwd=target, env=environment,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        (target / (command[1] + "日志.txt")).write_text(result.stdout)
        if result.returncode:
            raise RuntimeError(f"{command[1]} 失败，查看工程外的对应日志")
    plan = subprocess.run(["terraform", "show", "-json", "完整部署.tfplan"], cwd=target, env=environment,
                          capture_output=True, text=True, check=True)
    changes = json.loads(plan.stdout).get("resource_changes", [])
    if any("delete" in change.get("change", {}).get("actions", []) for change in changes):
        raise RuntimeError("计划包含删除或替换已有资源，已停止；请先核对完整部署计划")
    # 用户已授权执行一次完整 apply；不做 target 分批、不自动重试失败 apply。
    result = subprocess.run(["terraform", "apply", "-input=false", "-auto-approve", "完整部署.tfplan"],
                            cwd=target, env=environment, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    (target / "apply日志.txt").write_text(result.stdout)
    if result.returncode:
        raise RuntimeError("完整 apply 失败，查看工程外日志；保留 State，不自动重跑")
    outputs = subprocess.run(["terraform", "output", "-json"], cwd=target, env=environment,
                             capture_output=True, text=True, check=True)
    (target / "部署输出.json").write_text(outputs.stdout)
    print(f"完整 apply 已完成，State、日志与输出保存在：{target}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="完整部署 ComfyUI 视频工作室")
    parser.add_argument("--ark-key-file", type=Path, required=True)
    parser.add_argument("--ecs-password-file", type=Path)
    parser.add_argument("--cloud-config", type=Path, default=Path.home() / ".volcengine/config.json")
    parser.add_argument("--profile")
    parser.add_argument("--work-dir", type=Path, default=Path("/tmp/ComfyUI完整部署"))
    try:
        deploy(parser.parse_args())
    except (ValueError, RuntimeError, OSError, KeyError):
        print("部署未完成，请检查凭据配置和工程外日志。", file=sys.stderr)
        sys.exit(1)
