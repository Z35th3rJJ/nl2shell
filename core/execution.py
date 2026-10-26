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
from uuid import uuid4


@dataclass
class ExecutionResult:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    output_truncated: bool = False
    process_state: str = "unknown"
    system_metadata: dict | None = None


class ExecutionStartError(OSError):
    """Popen 已确认失败，没有启动子进程。"""


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

        return self._execute_argv(
            [self.bash_path, "-lc", command], timeout_seconds=timeout_seconds, cwd=cwd,
        )

    def execute_query(self, argv, operation, parameters, cwd, now, excluded_paths=(), *, timeout_seconds=60):
        from .bash_queries import prepare_queries
        from .task_plan import TaskPlan, TaskStep
        if os.name != "posix" or type(self) is not BashExecutor:
            raise RuntimeError("新增参数数组执行仅支持原生 Ubuntu/WSL；不会降级到其他后端")
        action = prepare_queries(TaskPlan((TaskStep(operation=operation, parameters=parameters),)), cwd,
                                 now=now, timeout_seconds=timeout_seconds,
                                 excluded_paths=excluded_paths)["actions"][0]
        if not isinstance(argv, list) or argv != action["_required_argv"]:
            raise ValueError("执行器拒绝未通过参数与路径检查的命令")
        binary = Path("/usr/bin") / argv[0]
        if not binary.is_file():
            binary = Path("/bin") / argv[0]
        if not binary.is_file():
            raise RuntimeError("系统没有对应的受支持命令")
        actual = [str(binary), *argv[1:]]
        locale = "C.UTF-8"
        environment = {"PATH": "/usr/bin:/bin", "LC_ALL": locale, "LANG": locale, "POSIXLY_CORRECT": "1"}
        result = self._execute_argv(actual, timeout_seconds, cwd, env=environment)
        return result, actual

    def execute_policy_query(self, argv, cwd, excluded=(), *, timeout_seconds=60):
        from .command_policy import review_query, sandbox_argv
        from .system_view import system_view, SYSTEM_VIEW
        if os.name != 'posix' or type(self) is not BashExecutor:
            raise RuntimeError('黑名单模式仅支持原生 Linux 沙箱，不降级到其他后端')
        argv = review_query(argv, cwd)
        if argv[0] not in {'lsblk','findmnt','mount'} and not any(SYSTEM_VIEW in value for value in argv):
            actual = sandbox_argv(argv, cwd, excluded=excluded)
            return self._execute_argv(actual, timeout_seconds, cwd), actual
        with system_view() as (view, summary):
            actual = sandbox_argv(argv, cwd, excluded=excluded, system_root=view)
            result = self._execute_argv(actual, timeout_seconds, cwd)
            result.system_metadata = summary
            return result, actual

    def execute_native_item(self, prepared, action, item, *, timeout_seconds=60):
        from .command_policy import native_argv, sandbox_argv
        from .operations import workspace_path
        argv = native_argv(action['operation'], item)
        if type(self) is not BashExecutor or argv is None or item.get('native_argv') != argv:
            raise ValueError('原生写入参数未通过保护流程')
        destination = workspace_path(Path(prepared['cwd']), item['destination'])
        if destination.exists() or destination.is_symlink():
            raise FileExistsError('原生写入目标已存在，未执行命令')
        actual = sandbox_argv(argv, prepared['cwd'], writable=True, excluded=prepared['excluded'])
        return self._execute_argv(actual, timeout_seconds, prepared['cwd']), actual

    def _execute_argv(self, argv: list[str], timeout_seconds: float | None,
                      cwd: str | None = None, *, env=None) -> ExecutionResult:

        start = time.monotonic()
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            try:
                process = subprocess.Popen(
                    argv, stdout=stdout_file, stderr=stderr_file, shell=False,
                    cwd=cwd, start_new_session=os.name != "nt",
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                    **({"env": env} if env is not None else {}),
                )
            except OSError as error:
                failure = ExecutionStartError(error.errno, str(error))
                failure.attempted_argv = list(argv)
                raise failure from error
            try:
                timed_out = False
                try:
                    process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _terminate_process_tree(process)
                except KeyboardInterrupt:
                    _terminate_process_tree(process)
                    raise
                stdout, stdout_truncated = _read_limited(stdout_file, self.output_limit)
                stderr, stderr_truncated = _read_limited(stderr_file, self.output_limit)
            except (OSError, RuntimeError, KeyboardInterrupt) as error:
                error.process_started = True
                error.process_state = "stopped" if type(process.poll()) is int else "unknown"
                error.actual_argv = list(argv)
                raise
        if timed_out:
            return ExecutionResult(None, stdout, stderr, time.monotonic() - start, True,
                                   stdout_truncated or stderr_truncated,
                                   "stopped" if type(process.poll()) is int else "unknown")
        return ExecutionResult(
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=time.monotonic() - start,
            output_truncated=stdout_truncated or stderr_truncated,
            process_state="exited",
        )


LocalExecutor = BashExecutor


class DockerExecutor(BashExecutor):
    """受限 Docker 执行后端；不可用时绝不降级到本机 Bash。"""

    def __init__(self, docker_path: str | None = None, output_limit: int = DEFAULT_OUTPUT_LIMIT,
                 image: str | None = None):
        super().__init__(docker_path or shutil.which("docker"), output_limit)
        self.docker_path = self.bash_path
        self.image = image or os.environ.get("SANDBOX_IMAGE", "ubuntu:22.04")

    def is_available(self) -> bool:
        if not self.docker_path:
            return False
        try:
            return subprocess.run(
                [self.docker_path, "info"], capture_output=True, check=False, timeout=5,
            ).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def execute(self, command: str, timeout_seconds: float | None = 60,
                cwd: str | None = None) -> ExecutionResult:
        if not self.docker_path:
            raise RuntimeError("Docker 沙箱不可用；请安装并启动 Docker")
        if not cwd or not Path(cwd).is_dir():
            raise RuntimeError("Docker 沙箱需要有效的工作目录")
        workspace = str(Path(cwd).resolve())
        container_name = f"nl2shell-{uuid4().hex[:12]}"
        argv = [
            self.docker_path, "run", "--rm", "--name", container_name, "--network", "none",
            "--memory", os.environ.get("SANDBOX_MEMORY", "512m"),
            "--cpus", os.environ.get("SANDBOX_CPUS", "1"),
            "--pids-limit", os.environ.get("SANDBOX_PIDS", "64"),
            "--user", os.environ.get("SANDBOX_USER", "65534:65534"),
            "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
            "-v", f"{workspace}:/workspace:rw", "-w", "/workspace",
            self.image, "bash", "-lc", command,
        ]
        result = self._execute_argv(argv, timeout_seconds=timeout_seconds)
        if result.timed_out:
            try:
                subprocess.run(
                    [self.docker_path, "rm", "-f", container_name], capture_output=True,
                    check=False, timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        return result


def create_executor(*, require_sandbox: bool = False) -> BashExecutor:
    backend = os.environ.get("EXECUTION_BACKEND", "local").lower()
    if require_sandbox or backend == "sandbox":
        return DockerExecutor()
    if backend != "local":
        raise ValueError("EXECUTION_BACKEND 必须是 local 或 sandbox")
    return LocalExecutor()


def try_change_directory(command: str) -> ExecutionResult | None:
    """处理 cd，使目录切换作用于 CLI 进程；非 cd 命令返回 None。"""
    if any(character in command for character in ";&|<>`$\n\r"):
        return None
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if not parts or parts[0] != "cd" or len(parts) > 2 or (len(parts) == 2 and parts[1].startswith("-")):
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
