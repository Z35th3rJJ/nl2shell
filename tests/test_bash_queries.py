import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from cli import execute_request, run_batch, show_preview
from core.history import HistoryStore
from core.bash_queries import (prepare_queries, review_commands, execute_query, query_preview,
                               same_query_plan, COMMANDS)
from core.engine import Engine
from core.execution import BashExecutor, ExecutionResult, DockerExecutor
from core.task_plan import TaskPlan, TaskStep, parse_operation_plan


def plan(operation, **parameters):
    return TaskPlan((TaskStep(operation=operation, parameters=parameters),))


def candidates(prepared):
    return {"commands": [{"step": action["step"], "argv": action["_required_argv"],
                          "explanation": "按明确条件查询"} for action in prepared["actions"]]}


class History:
    def __init__(self):
        self.records = []
    def append(self, record):
        self.records.append(record)


def engine_for(value):
    engine = Mock()
    engine.generate_task_plan.return_value = value
    engine.generate_bash_commands.side_effect = lambda request, cwd, value, prepared, answers: candidates(prepared)
    return engine


@pytest.fixture
def text_file(tmp_path):
    name = "-中文 空格'引号.txt"
    (tmp_path / name).write_bytes("第一行\n目标文本\n末行".encode())
    return name


@pytest.mark.parametrize("operation,parameters,command", [
    ("working_directory", {}, "pwd"),
    ("list_directory", {"path": ".", "include_hidden": False}, "ls"),
    ("find_files", {"path": ".", "recursive": False, "pattern": "*.txt"}, "find"),
    ("count_files", {"path": ".", "recursive": False}, "find"),
    ("search_text", {"pattern": "目标文本"}, "grep"),
    ("text_stats", {"metric": "lines"}, "wc"),
    ("text_stats", {"metric": "words"}, "wc"),
    ("text_stats", {"metric": "bytes"}, "wc"),
    ("read_text", {"end": "head", "lines": 2}, "head"),
    ("read_text", {"end": "tail", "lines": 2}, "tail"),
    ("system_info", {"query": "disk"}, "df"),
    ("system_info", {"query": "memory"}, "free"),
    ("system_info", {"query": "system"}, "uname"),
])
def test_supported_command_rules(tmp_path, text_file, operation, parameters, command):
    if operation in {"search_text", "text_stats", "read_text"}:
        parameters = dict(parameters, path=text_file)
    prepared = prepare_queries(plan(operation, **parameters), str(tmp_path))
    review_commands(prepared, candidates(prepared))
    action = prepared["actions"][0]
    assert action["candidate_argv"][0] == command in COMMANDS
    assert action["execution_kind"] == "model_bash_argv"
    assert not any(key.startswith("_") for key in query_preview(prepared)[0])
    if command in {"grep", "head", "tail", "wc"}:
        assert action["candidate_argv"][-1] == "./" + text_file
    if os.name == "posix":
        outcome = execute_query(prepared, action, BashExecutor())
        assert outcome["status"] == "verified", outcome
        assert outcome["task_completed"] and outcome["executed"]
        assert outcome["actual_argv"][1:] == action["candidate_argv"][1:]
        assert (tmp_path / text_file).read_bytes() == "第一行\n目标文本\n末行".encode()


@pytest.mark.parametrize("argv", [
    "pwd -P", ["bash", "-lc", "pwd"], ["pwd", "-P", ";", "touch", "x"],
    ["pwd", "-P", "|", "wc"], ["pwd", "-P", ">", "x"],
    ["pwd", "$(touch x)"], ["pwd", "`touch x`"], ["/tmp/pwd", "-P"],
    ["pwd", "-P\npwd"], ["pwd", "-P", "&&", "pwd"], ["pwd", None],
])
def test_command_combinations_never_execute(tmp_path, argv):
    engine = engine_for(plan("working_directory"))
    engine.generate_bash_commands.side_effect = None
    engine.generate_bash_commands.return_value = {"commands": [{"step": 1, "argv": argv, "explanation": "候选"}]}
    executor, history = Mock(), History()
    assert execute_request(engine, executor, history, "显示工作目录", str(tmp_path), "confirm") == "blocked"
    executor.execute_query.assert_not_called()
    executor.execute.assert_not_called()
    assert history.records[-1]["candidates"]["commands"][0]["argv"] == argv
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("mutation", ["recursive", "pattern", "time", "size", "delete", "exec", "outside"])
def test_find_cannot_drop_conditions_or_add_effects(tmp_path, mutation):
    prepared = prepare_queries(plan("find_files", path=".", recursive=False, pattern="*.txt",
                                    modified_within_days=7, size_gt_bytes=10), str(tmp_path))
    payload = candidates(prepared)
    argv = list(payload["commands"][0]["argv"])
    if mutation == "outside":
        argv[1] = ".."
    elif mutation in {"delete", "exec"}:
        argv += ["-delete"] if mutation == "delete" else ["-exec", "touch", "x", ";"]
    else:
        flag = {"recursive": "-maxdepth", "pattern": "-name", "time": "-newermt", "size": "-size"}[mutation]
        index = argv.index(flag)
        del argv[index:index + 2]
    payload["commands"][0]["argv"] = argv
    with pytest.raises(ValueError, match="不符合"):
        review_commands(prepared, payload)


@pytest.mark.parametrize("name", [".env", ".env.local", ".ssh/config", ".aws/credentials",
                                 ".git/config", "server.pem", "key.p12"])
def test_sensitive_paths_are_blocked(tmp_path, name):
    file = tmp_path / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("private", encoding="utf-8")
    with pytest.raises(ValueError, match="敏感"):
        prepare_queries(plan("read_text", path=name, end="head", lines=1), str(tmp_path))


def test_outside_absolute_and_sensitive_content_are_blocked(tmp_path):
    with pytest.raises(ValueError, match="之外"):
        prepare_queries(plan("search_text", path="../outside", pattern="text"), str(tmp_path))
    (tmp_path / "notes.txt").write_text("PASSWORD=example", encoding="utf-8")
    with pytest.raises(ValueError, match="凭据"):
        prepare_queries(plan("read_text", path="notes.txt", end="head", lines=1), str(tmp_path))


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_links_and_devices_are_blocked(tmp_path, kind):
    source = tmp_path / "source"
    source.write_text("text", encoding="utf-8")
    target = tmp_path / "link"
    try:
        if kind == "symlink":
            target.symlink_to(source)
        elif kind == "hardlink":
            os.link(source, target)
        elif os.name == "posix":
            os.mkfifo(target)
        else:
            pytest.skip("Windows 没有 POSIX FIFO")
    except OSError:
        pytest.skip("当前平台不能创建此类链接")
    with pytest.raises(ValueError):
        prepare_queries(plan("text_stats", path="link", metric="bytes"), str(tmp_path))


@pytest.mark.parametrize("content", [b"binary\0data", b"\xff", b"x" * (1024 * 1024 + 1)],
                         ids=["binary", "invalid-utf8", "oversized"])
def test_non_text_and_oversized_files_are_blocked(tmp_path, content):
    (tmp_path / "input").write_bytes(content)
    with pytest.raises(ValueError):
        prepare_queries(plan("read_text", path="input", end="head", lines=1), str(tmp_path))


def test_independent_requirements_catch_plan_omission(monkeypatch, tmp_path):
    planned = plan("find_files", path=".", recursive=False)
    reference = plan("find_files", path=".", recursive=False, pattern="*.txt")
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: json.dumps({"status": "ready", "steps": [
        {"operation": s.operation, "parameters": s.parameters} for s in reference.steps]}))
    engine = Engine(ssh_hosts=[])
    with pytest.raises(ValueError, match="独立需求"):
        engine.generate_bash_commands("找出当前目录的 txt 文件", str(tmp_path), planned,
                                     prepare_queries(planned, str(tmp_path)), [])
    assert len(engine.command_attempts) == 1
    assert engine.command_attempts[0]["review"] == "rejected"
    assert not same_query_plan(planned, reference)


@pytest.mark.parametrize("exit_code,stdout,status,complete", [(0, "wrong\n", "unverified", False),
    (1, "", "no_matches", False), (2, "", "execution_failed", False)])
def test_exit_code_is_not_task_completion(tmp_path, exit_code, stdout, status, complete):
    (tmp_path / "input").write_text("text\n", encoding="utf-8")
    prepared = prepare_queries(plan("search_text", path="input", pattern="absent"), str(tmp_path))
    review_commands(prepared, candidates(prepared))
    executor = Mock()
    executor.execute_query.return_value = (ExecutionResult(exit_code, stdout, "", 0.01), ["/usr/bin/grep"])
    outcome = execute_query(prepared, prepared["actions"][0], executor)
    assert outcome["status"] == status and outcome["task_completed"] is complete


def test_failed_query_stops_later_steps_without_retry(tmp_path):
    value = TaskPlan((TaskStep(operation="working_directory"), TaskStep(operation="working_directory")))
    engine, executor, history = engine_for(value), Mock(), History()
    executor.execute_query.return_value = (ExecutionResult(2, "", "failed", 0.01), ["/usr/bin/pwd", "-P"])
    status = execute_request(engine, executor, history, "显示目录", str(tmp_path), "confirm", input_fn=lambda _: "yes")
    assert status == "execution_failed"
    assert executor.execute_query.call_count == engine.generate_bash_commands.call_count == 1
    assert history.records[-1]["verification"][1]["status"] == "not_executed"


def test_confirmation_rechecks_changed_text(tmp_path):
    (tmp_path / "input").write_text("before\n", encoding="utf-8")
    calls = []
    def confirm(message):
        calls.append(message)
        if len(calls) == 1:
            (tmp_path / "input").write_text("after\n", encoding="utf-8")
            return "yes"
        return "n"
    executor = Mock()
    assert execute_request(engine_for(plan("read_text", path="input", end="head", lines=1)), executor,
                           History(), "查看首行", str(tmp_path), "confirm", input_fn=confirm) == "cancelled"
    assert len(calls) == 2
    executor.execute_query.assert_not_called()


def test_native_executor_rejects_unsafe_arguments(tmp_path):
    if os.name != "posix":
        pytest.skip("原生参数数组执行只支持 Ubuntu/WSL")
    with pytest.raises(ValueError):
        BashExecutor().execute_query(["find", ".", "-delete"], "find_files",
                                    {"path": ".", "recursive": False}, str(tmp_path), 1)
    with pytest.raises(RuntimeError, match="不会降级"):
        DockerExecutor().execute_query(["pwd", "-P"], "working_directory", {}, str(tmp_path), 1)


@pytest.mark.parametrize("parameters", [{"path": "x", "end": "head", "lines": 0},
                                      {"path": "x", "end": "head", "lines": True},
                                      {"path": "x", "end": "tail", "lines": 1001}])
def test_text_plan_parameters_are_strict(parameters):
    with pytest.raises(ValueError):
        parse_operation_plan(json.dumps({"status": "ready", "steps": [{"operation": "read_text", "parameters": parameters}]}))


def test_disk_query_checks_its_path(tmp_path):
    with pytest.raises(ValueError, match="之外"):
        prepare_queries(plan("system_info", query="disk", path=".."), str(tmp_path))
    with pytest.raises(ValueError, match="仅磁盘"):
        parse_operation_plan(json.dumps({"status": "ready", "steps": [
            {"operation": "system_info", "parameters": {"query": "memory", "path": "."}}]}))


def test_empty_search_is_not_a_batch_failure(tmp_path):
    (tmp_path / "input").write_text("text\n", encoding="utf-8")
    task = tmp_path / "tasks.jsonl"
    task.write_text(json.dumps({"input": "查找不存在的文本", "cwd": str(tmp_path)}) + "\n", encoding="utf-8")
    engine = engine_for(plan("search_text", path="input", pattern="absent"))
    executor = Mock()
    executor.execute_query.return_value = (ExecutionResult(1, "", "", 0.01), ["/usr/bin/grep"])
    summary = run_batch(engine, executor, HistoryStore(tmp_path / "history.jsonl"), task)
    assert summary["no_matches"] == 1 and summary["failed"] == summary["success"] == 0


def test_interrupt_terminates_the_process_before_returning(monkeypatch):
    process = Mock()
    process.wait.side_effect = KeyboardInterrupt
    terminate = Mock()
    monkeypatch.setattr("core.execution.subprocess.Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr("core.execution._terminate_process_tree", terminate)
    with pytest.raises(KeyboardInterrupt):
        BashExecutor()._execute_argv(["pwd", "-P"], 1)
    terminate.assert_called_once_with(process)


@pytest.mark.parametrize("text", ["中文\u00a0word", "中文\u2060word", "\x01"])
def test_word_count_rejects_unsupported_separators(tmp_path, text):
    (tmp_path / "input").write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="Unicode"):
        prepare_queries(plan("text_stats", path="input", metric="words"), str(tmp_path))


def test_independent_request_keeps_original_data_without_the_plan(monkeypatch, tmp_path):
    value = plan("working_directory")
    prepared = prepare_queries(value, str(tmp_path))
    calls = []
    def reply(messages, backend=None, json_mode=False):
        assert json_mode
        calls.append(messages)
        if len(calls) == 1:
            original = json.loads(messages[-1]["content"])
            assert original == {"cwd": str(tmp_path), "request": "显示当前工作目录", "answers": []}
            assert "plan" not in original and "commands" not in original
            return json.dumps({"status": "ready", "steps": [{"operation": "working_directory", "parameters": {}}]})
        return json.dumps(candidates(prepared))
    monkeypatch.setattr("core.engine.chat", reply)
    assert Engine(ssh_hosts=[]).generate_bash_commands("显示当前工作目录", str(tmp_path), value,
                                                      prepared, []) == candidates(prepared)
    assert len(calls) == 2


@pytest.mark.parametrize("enabled", [False, True])
def test_json_mode_is_only_requested_for_structured_calls(monkeypatch, enabled):
    from core.llm import chat
    client = Mock()
    client.chat.completions.create.return_value.choices = [Mock(message=Mock(content='{"ok":true}'))]
    monkeypatch.setattr("core.llm._get_client", lambda backend: (client, "model"))
    assert chat([{"role": "user", "content": "JSON"}], "local", json_mode=enabled) == '{"ok":true}'
    sent = client.chat.completions.create.call_args.kwargs
    assert ("response_format" in sent) is enabled
    if enabled:
        assert sent["response_format"] == {"type": "json_object"}


def test_negative_epoch_bounds_keep_the_fraction_sign():
    from core.bash_queries import _timestamp
    assert _timestamp(-1) == "@-0.000000001"
    assert _timestamp(-1000000001) == "@-1.000000001"
    assert _timestamp(1000000001) == "@1.000000001"


@pytest.mark.parametrize("value", [
    TaskPlan((TaskStep(operation="working_directory"),) * 4),
    TaskPlan((TaskStep(operation="working_directory"),), refused=True),
    TaskPlan((TaskStep(operation="working_directory"),), clarification="缺少条件"),
])
def test_query_preparation_keeps_original_plan_boundaries(tmp_path, value):
    with pytest.raises(ValueError, match="完整"):
        prepare_queries(value, str(tmp_path))


def test_preview_displays_program_checked_conditions(tmp_path, capsys):
    prepared = prepare_queries(plan("find_files", path=".", recursive=False, pattern="*.txt",
                                    modified_within_days=7, size_gt_bytes=10), str(tmp_path))
    review_commands(prepared, candidates(prepared))
    show_preview(prepared)
    output = capsys.readouterr().out
    assert "最近 7 天" in output and "大于 10 字节" in output and "*.txt" in output
    assert "候选命令" in output and "模型命令" in output
