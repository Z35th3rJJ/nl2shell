"""启动环境的无副作用诊断。"""
from dataclasses import dataclass
import os
from pathlib import Path

from .execution import BashExecutor, DockerExecutor


@dataclass(frozen=True)
class Diagnostic:
    name: str
    ok: bool
    message: str


def diagnose_environment(executor: BashExecutor, cwd: str | None = None) -> tuple[Diagnostic, ...]:
    cwd = cwd or os.getcwd()
    backend = os.environ.get("LLM_BACKEND", "deepseek").lower()
    if backend == "local":
        model_ok = bool(os.environ.get("LOCAL_BASE_URL", "http://localhost:11434/v1"))
        model_message = "本地模型配置已就绪" if model_ok else "请设置 LOCAL_BASE_URL"
    else:
        model_ok = bool(os.environ.get("DEEPSEEK_API_KEY"))
        model_message = "DeepSeek 配置已就绪" if model_ok else "请在 .env 设置 DEEPSEEK_API_KEY"
    path = Path(cwd)
    directory_ok = path.is_dir() and os.access(path, os.R_OK | os.X_OK)
    writable = directory_ok and os.access(path, os.W_OK)
    bash_ok = executor.is_available()
    execution_name = "Docker 沙箱" if isinstance(executor, DockerExecutor) else "本机 Bash"
    return (
        Diagnostic("model", model_ok, model_message),
        Diagnostic("execution", bash_ok,
                   f"{execution_name}可用" if bash_ok else f"{execution_name}不可用，请检查配置"),
        Diagnostic("cwd", directory_ok, "当前目录可访问" if directory_ok else "当前目录不可访问"),
        Diagnostic("write", writable, "当前目录可写" if writable else "当前目录只读，写操作将失败"),
    )
