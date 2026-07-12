"""主任务输入的跨平台行编辑与持久历史。"""
import os
from pathlib import Path
from typing import Callable


def default_input_history_path() -> Path:
    return Path.home() / ".nl2shell" / "input_history"


class BasicInputSession:
    """依赖不可用时的普通 input() 回退。"""

    def __init__(self, input_fn: Callable[[str], str] = input):
        self._input_fn = input_fn

    def prompt(self, message: str) -> str:
        return self._input_fn(message)


def create_input_session(
    history_path: Path | None = None,
    output_fn: Callable[[str], None] = print,
    input_fn: Callable[[str], str] = input,
):
    path = history_path or default_input_history_path()
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory
    except ImportError:
        output_fn("提示：未安装 prompt_toolkit，暂时无法使用上下键历史和 Ctrl+R；请运行 pip install -r requirements.txt。")
        return BasicInputSession(input_fn)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        path.read_text(encoding="utf-8")
        if os.name != "nt":
            path.chmod(0o600)
        return PromptSession(history=FileHistory(str(path)), enable_history_search=True)
    except (OSError, UnicodeError) as error:
        output_fn(f"提示：输入历史不可用（{error}），本次运行已回退到普通输入。")
        return BasicInputSession(input_fn)
