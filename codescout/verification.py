"""Syntax validation and explicitly configured project commands."""

import json
import os
import shlex
import subprocess

from .processes import run_command
from .snapshot import read_file


def validate_text(path, text):
    if path.endswith(".py"):
        compile(text, path, "exec")
        return True
    if path.endswith(".json"):
        json.loads(text)
        return True
    return False


def command_args(command):
    if not isinstance(command, str) or len(command) > 2000 or "\n" in command or "\r" in command:
        raise ValueError("验证命令格式无效")
    args = shlex.split(command, posix=os.name != "nt")
    if os.name == "nt":
        args = [arg[1:-1] if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in "\"'" else arg for arg in args]
    if not args or len(args) > 100 or any(arg in {"&&", "||", ";", "|", ">", "<"} for arg in args):
        raise ValueError("请配置单条验证命令，不使用 shell 连接符")
    return args


def verify_project(root, paths, command="", redactions=(), timeout=60):
    result = {"status": "not_configured", "syntax_status": "not_applicable", "command": command, "exit_code": None, "output": "", "timed_out": False, "output_truncated": False}
    try:
        validated = False
        for path in paths:
            text = read_file(root, root / path).decode("utf-8-sig")
            validated = validate_text(path, text) or validated
        result["syntax_status"] = "passed" if validated else "not_applicable"
    except (ValueError, OSError, SyntaxError):
        return {**result, "status": "failed", "syntax_status": "failed", "output": "实际文件未通过语法校验"}
    if not command:
        return result
    try:
        args = command_args(command)
        captured = run_command(args, root, timeout)
        result.update(captured, status="passed" if captured["exit_code"] == 0 and not captured["timed_out"] else "failed")
    except (ValueError, OSError, subprocess.SubprocessError):
        result.update(status="failed", output="无法执行配置的验证命令，请检查程序路径和参数")
    for secret in redactions:
        if secret:
            result["output"] = result["output"].replace(secret, "[REDACTED]")
            result["command"] = result["command"].replace(secret, "[REDACTED]")
    return result
