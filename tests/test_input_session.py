import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import cli
from core.input_session import BasicInputSession, create_input_session, default_input_history_path
from core.settings import AppSettings, CONFIRM_MODE


def _install_fake_prompt_toolkit(monkeypatch):
    package = ModuleType("prompt_toolkit")
    history_module = ModuleType("prompt_toolkit.history")

    class FileHistory:
        def __init__(self, filename):
            self.filename = filename

    class PromptSession:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def prompt(self, message):
            return message

    package.PromptSession = PromptSession
    history_module.FileHistory = FileHistory
    monkeypatch.setitem(sys.modules, "prompt_toolkit", package)
    monkeypatch.setitem(sys.modules, "prompt_toolkit.history", history_module)
    return PromptSession


def test_default_history_path_is_in_user_application_directory():
    assert default_input_history_path().name == "input_history"
    assert default_input_history_path().parent.name == ".nl2shell"


def test_prompt_session_uses_persistent_history_and_search(tmp_path, monkeypatch):
    prompt_session = _install_fake_prompt_toolkit(monkeypatch)
    history_path = tmp_path / "nested" / "input_history"
    session = create_input_session(history_path, output_fn=lambda _: None)
    assert isinstance(session, prompt_session)
    assert session.kwargs["history"].filename == str(history_path)
    assert session.kwargs["enable_history_search"] is True
    assert history_path.exists()


def test_missing_dependency_falls_back_with_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "prompt_toolkit", None)
    messages = []
    fallback_input = Mock(return_value="查看文件")
    session = create_input_session(output_fn=messages.append, input_fn=fallback_input)
    assert isinstance(session, BasicInputSession)
    assert session.prompt("> ") == "查看文件"
    assert any("pip install -r requirements.txt" in message for message in messages)


def test_corrupt_history_falls_back_to_basic_input(tmp_path, monkeypatch):
    _install_fake_prompt_toolkit(monkeypatch)
    history_path = tmp_path / "input_history"
    history_path.write_bytes(b"\xff\xfe")
    messages = []
    session = create_input_session(history_path, output_fn=messages.append, input_fn=lambda _: "exit")
    assert isinstance(session, BasicInputSession)
    assert any("输入历史不可用" in message for message in messages)


def test_unwritable_history_falls_back_to_basic_input(tmp_path, monkeypatch):
    _install_fake_prompt_toolkit(monkeypatch)

    def deny_write(self, exist_ok=True):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "touch", deny_write)
    messages = []
    session = create_input_session(tmp_path / "input_history", output_fn=messages.append,
                                   input_fn=lambda _: "exit")
    assert isinstance(session, BasicInputSession)
    assert any("输入历史不可用" in message for message in messages)


def test_main_uses_session_only_for_primary_command_input(monkeypatch):
    session = Mock()
    session.prompt.return_value = "/exit"
    monkeypatch.setattr(cli, "load_settings", lambda: AppSettings(CONFIRM_MODE, True))
    monkeypatch.setattr(cli, "first_run_setup", lambda settings: settings)
    monkeypatch.setattr(cli, "BashExecutor", Mock)
    monkeypatch.setattr(cli, "Engine", Mock)
    monkeypatch.setattr(cli, "HistoryStore", Mock)
    cli.main(input_session=session)
    session.prompt.assert_called_once()
