"""Bash 命令执行与结果采集。"""
from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time


@dataclass
class ExecutionResult:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float


def resolve_bash_path() -> str | None:
    """返回已配置且可调用的 Bash 路径；未找到时返回 None。"""
    configured = os.environ.get("BASH_PATH") or os.environ.get("SHELL_EXECUTABLE", "bash")
    path = Path(configured).expanduser()
    if path.is_file():
        return str(path)
    return shutil.which(configured)


def bash_unavailable_message() -> str:
    return "未找到 Bash。请安装 Git Bash/WSL，或在 .env 中设置 BASH_PATH。"


class BashExecutor:
    def __init__(self, bash_path: str | None = None):
        self.bash_path = bash_path or resolve_bash_path()

    def is_available(self) -> bool:
        if not self.bash_path:
            return False
        try:
            return subprocess.run(
                [self.bash_path, "-lc", "true"],
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            ).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def execute(self, command: str, timeout_seconds: float | None = None) -> ExecutionResult:
        if not self.bash_path:
            raise RuntimeError(bash_unavailable_message())

        start = time.monotonic()
        completed = subprocess.run(
            [self.bash_path, "-lc", command],
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
        return ExecutionResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=time.monotonic() - start,
        )


def try_change_directory(command: str) -> ExecutionResult | None:
    """处理 cd，使目录切换作用于 CLI 进程；非 cd 命令返回 None。"""
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if not parts or parts[0] != "cd":
        return None

    target = os.path.expanduser(parts[1] if len(parts) > 1 else "~")
    start = time.monotonic()
    try:
        os.chdir(target)
    except (FileNotFoundError, NotADirectoryError):
        return ExecutionResult(1, "", f"cd: 目录不存在：{target}\n", time.monotonic() - start)
    except PermissionError:
        return ExecutionResult(1, "", f"cd: 权限不足：{target}\n", time.monotonic() - start)
    return ExecutionResult(0, f"已切换到：{os.getcwd()}\n", "", time.monotonic() - start)
