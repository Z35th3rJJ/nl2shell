"""Bash 命令执行与结果采集。"""
from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import tempfile
import time


@dataclass
class ExecutionResult:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    output_truncated: bool = False


DEFAULT_OUTPUT_LIMIT = 200_000


def _read_limited(file, limit: int) -> tuple[str, bool]:
    file.seek(0, os.SEEK_END)
    size = file.tell()
    if size <= limit:
        file.seek(0)
        return file.read().decode(errors="replace"), False
    half = max(1, limit // 2)
    file.seek(0)
    head = file.read(half)
    file.seek(-half, os.SEEK_END)
    tail = file.read(half)
    marker = f"\n... 输出已截断（原始 {size} 字节）...\n".encode()
    return (head + marker + tail).decode(errors="replace"), True


def _terminate_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


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
    def __init__(self, bash_path: str | None = None, output_limit: int = DEFAULT_OUTPUT_LIMIT):
        self.bash_path = bash_path or resolve_bash_path()
        self.output_limit = output_limit

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

    def execute(self, command: str, timeout_seconds: float | None = 60,
                cwd: str | None = None) -> ExecutionResult:
        if not self.bash_path:
            raise RuntimeError(bash_unavailable_message())

        start = time.monotonic()
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                [self.bash_path, "-lc", command], stdout=stdout_file, stderr=stderr_file,
                cwd=cwd, start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
            timed_out = False
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_tree(process)
            stdout, stdout_truncated = _read_limited(stdout_file, self.output_limit)
            stderr, stderr_truncated = _read_limited(stderr_file, self.output_limit)
        if timed_out:
            return ExecutionResult(None, stdout, stderr, time.monotonic() - start, True,
                                   stdout_truncated or stderr_truncated)
        return ExecutionResult(
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=time.monotonic() - start,
            output_truncated=stdout_truncated or stderr_truncated,
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
