import argparse
import io
import json
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import cli
from core.execution import ExecutionResult
from core.settings import AppSettings, PREVIEW
from core.task_plan import TaskPlan
from tests.test_cli_flow import History, engine_for, step


@pytest.fixture
def terminal(monkeypatch):
    output = io.StringIO()
    output.isatty = lambda: True
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("WT_SESSION", "terminal-ui-test")
    return output


def test_preview_numbers_and_review_do_not_claim_execution(terminal):
    with redirect_stdout(terminal):
        cli.show_preview({"cwd": "/demo", "actions": [
            {"step": 1, "operation": "working_directory", "explanation": "显示工作目录",
             "parameters": {}, "files": [], "items": [], "candidate_argv": ["pwd", "-P"],
             "command": "pwd -P", "candidate_explanation": "显示实际路径", "review": "accepted"},
            {"step": 2, "operation": "create_file", "explanation": "创建空文件",
             "parameters": {"path": "done.flag"}, "files": [], "items": [{"destination": "done.flag"}]},
        ]})
    text = terminal.getvalue()
    assert cli.BOLD + "第 1 步" in text and cli.BOLD + "第 2 步" in text
    assert cli.CYAN + "    pwd -P" in text
    assert cli.GREEN + "  审查：通过（尚未执行）" in text
    assert cli.GRAY + "工作目录：/demo" in text
    assert "已验证成功" not in text
    assert "任务结果" not in text


@pytest.mark.parametrize("policy", ["no_color", "empty_no_color", "redirected", "dumb", "unsupported_windows"])
def test_color_disabled_keeps_plain_text(terminal, monkeypatch, policy):
    if policy in {"no_color", "empty_no_color"}:
        monkeypatch.setenv("NO_COLOR", "1" if policy == "no_color" else "")
    elif policy == "redirected":
        terminal.isatty = lambda: False
    elif policy == "dumb":
        monkeypatch.setenv("TERM", "dumb")
    else:
        monkeypatch.setattr(cli, "os", SimpleNamespace(name="nt", environ={}))
    with redirect_stdout(terminal):
        cli.show_outcome({"step": 1, "operation": "working_directory", "status": "verified",
                          "detail": "路径一致", "actual_argv": ["/usr/bin/pwd", "-P"], "stdout": "/demo\n"})
    text = terminal.getvalue()
    assert "\x1b" not in text
    assert "第 1 步：已验证成功" in text and "实际命令" in text


def test_preview_mode_has_no_final_success(tmp_path, terminal):
    history, executor = History(), Mock()
    with redirect_stdout(terminal):
        status = cli.execute_request(engine_for(step("create_file", path="done.flag")), executor, history,
                                     "创建 done.flag", str(tmp_path), PREVIEW)
    text = terminal.getvalue()
    assert status == "preview" and "最终验证" in text
    assert "任务结果：仅预览" in text and "执行状态：未执行" in text
    assert "已验证成功" not in text and not (tmp_path / "done.flag").exists()
    executor.execute.assert_not_called()


@pytest.mark.parametrize("case,status", [("read", "verified"), ("write", "verified"),
                                        ("refusal", "unsupported"), ("safety", "blocked"),
                                        ("failure", "execution_failed")])
def test_request_displays_confirmation_and_final_state(tmp_path, terminal, case, status):
    executor, history = Mock(), History()
    executor.execute_query.return_value = (ExecutionResult(0, str(tmp_path) + "\n", "", 0), ["/usr/bin/pwd", "-P"])
    engine = engine_for(step("working_directory"))
    if case == "write":
        engine = engine_for(step("create_file", path="done.flag"))
    elif case == "refusal":
        engine.generate_task_plan.return_value = TaskPlan((), refused=True, reason="不支持该操作")
    elif case == "safety":
        engine.generate_bash_commands.side_effect = None
        engine.generate_bash_commands.return_value = {"commands": [{"step": 1, "argv": ["pwd", "--help"], "explanation": "偏离查询要求"}]}
    elif case == "failure":
        executor.execute_query.return_value = (ExecutionResult(2, "", "permission denied", 0), ["/usr/bin/pwd", "-P"])
    prompts = []

    def confirm(prompt):
        prompts.append(prompt)
        return "yes"

    with redirect_stdout(terminal):
        result = cli.execute_request(engine, executor, history, "演示任务", str(tmp_path), "confirm", input_fn=confirm)
    text = terminal.getvalue()
    assert result == status == history.last_record["status"]
    assert cli.BOLD + "任务：演示任务" in text
    assert "最终验证" in text and "任务结果：" + cli._STATUS_NAMES[status] in text
    assert (cli.GREEN if status == "verified" else cli.RED) in text
    if case not in {"refusal", "safety"}:
        assert len(prompts) == 1 and prompts[0].startswith(cli.YELLOW)
        assert "请输入 yes 确认" in prompts[0]
    else:
        assert not prompts and not history.last_record["executed"]
        executor.execute_query.assert_not_called()
    if case == "write":
        assert (tmp_path / "done.flag").read_bytes() == b""
    for record in history.records:
        assert "\x1b" not in json.dumps(record)


@pytest.mark.parametrize("case,status", [("read", "verified"), ("write", "cancelled"),
                                        ("preview", "preview"), ("refusal", "unsupported"),
                                        ("safety", "blocked"),
                                        ("failure", "execution_failed")])
def test_json_stays_exactly_the_public_payload(tmp_path, terminal, monkeypatch, case, status):
    history, executor = History(), Mock()
    engine = engine_for(step("working_directory"))
    executor.execute_query.return_value = (ExecutionResult(0, str(tmp_path) + "\n", "", 0), ["/usr/bin/pwd", "-P"])
    if case in {"write", "preview"}:
        engine = engine_for(step("create_file", path="done.flag"))
    elif case == "refusal":
        engine.generate_task_plan.return_value = TaskPlan((), refused=True, reason="不支持该操作")
    elif case == "safety":
        engine.generate_bash_commands.side_effect = None
        engine.generate_bash_commands.return_value = {"commands": [{"step": 1, "argv": ["pwd", "--help"], "explanation": "偏离查询要求"}]}
    elif case == "failure":
        executor.execute_query.return_value = (ExecutionResult(2, "", "permission denied", 0), ["/usr/bin/pwd", "-P"])
    monkeypatch.setattr(cli, "Engine", lambda: engine)
    monkeypatch.setattr(cli, "HistoryStore", lambda: history)
    monkeypatch.setattr(cli, "create_executor", lambda: executor)
    monkeypatch.setattr(cli, "load_settings", lambda: AppSettings("confirm", True))
    monkeypatch.setattr(cli, "recover_working_directory", lambda: (str(tmp_path), ""))
    args = argparse.Namespace(task="演示任务", batch=None, timeout=60, preview=case == "preview", yes=True, json=True)
    with redirect_stdout(terminal):
        cli.main(args=args)
    text = terminal.getvalue()
    payload = json.loads(text)
    assert payload == cli.json_result(history.last_record, status)
    assert payload["status"] == status
    assert "\x1b" not in text and "最终验证" not in text and "任务预览" not in text
    assert not (tmp_path / "done.flag").exists()


def test_startup_and_primary_prompt_use_separate_sections(tmp_path, terminal, monkeypatch):
    session = Mock()
    session.prompt.return_value = "/exit"
    monkeypatch.setattr(cli, "load_settings", lambda: AppSettings("confirm", True))
    monkeypatch.setattr(cli, "first_run_setup", lambda settings: settings)
    monkeypatch.setattr(cli, "Engine", Mock)
    monkeypatch.setattr(cli, "HistoryStore", History)
    monkeypatch.setattr(cli, "create_executor", Mock)
    monkeypatch.setattr(cli, "print_diagnostics", lambda *args, **kwargs: True)
    monkeypatch.setattr(cli, "recover_working_directory", lambda *args: (str(tmp_path), ""))
    with redirect_stdout(terminal):
        cli.main(input_session=session)
    text = terminal.getvalue()
    assert cli.BOLD + cli.CYAN + "智能 Shell 助手" in text
    assert "运行方式：确认执行" in text and cli.GRAY + "工作目录：" in text
    assert cli.BOLD + "新任务" in text
    session.prompt.assert_called_once_with("你想做什么？> ")
