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


def reference_json(value):
    return json.dumps({"status": "ready", "steps": [
        {"operation": s.operation, "parameters": s.parameters} for s in value.steps]})


@pytest.fixture
def no_reference_execution(monkeypatch):
    launch = Mock(side_effect=AssertionError("独立参考纠正不得执行命令"))
    monkeypatch.setattr("subprocess.Popen", launch)
    yield launch
    launch.assert_not_called()


@pytest.mark.parametrize("pattern", ["ERROR.*", "[1]"])
def test_literal_reference_regex_refusal_gets_one_independent_correction(monkeypatch, tmp_path, no_reference_execution, pattern):
    (tmp_path / "sample.txt").write_text("ERROR.*\n[1]\n", encoding="utf-8")
    value = plan("search_text", path="sample.txt", pattern=pattern)
    prepared = prepare_queries(value, str(tmp_path))
    request = f"在 sample.txt 中搜索字面文本 {pattern}"
    refusal = json.dumps({"status": "unsupported", "reason": "当前版本只支持字面文本搜索，不支持正则表达式搜索"})
    replies = iter([refusal, json.dumps(candidates(prepared))])
    calls = []
    def reply(messages, **kwargs):
        calls.append([dict(message) for message in messages])
        return next(replies)
    monkeypatch.setattr("core.engine.chat", reply)
    engine = Engine(ssh_hosts=[])
    engine.request_intent = {"operations": ["SENTINEL_PRIMARY_PLAN"]}

    result = engine.generate_bash_commands(request, str(tmp_path), value, prepared, [])

    assert len(calls) == 2
    assert [r["phase"] for r in engine.command_attempts] == ["independent_requirements", "candidate_commands"]
    assert engine.command_attempts[0]["review"] == "accepted"
    assert engine.command_attempts[0]["raw_output"] == refusal
    recovery = engine.command_attempts[0]["recovery"]
    assert recovery["source"] == "original_request"
    checked = parse_operation_plan(json.dumps(recovery["reference"]))
    assert same_query_plan(value, checked)
    assert checked.steps[0].parameters == {"path": "sample.txt", "pattern": pattern}
    original = calls[0][-1]["content"]
    assert json.loads(original) == {"cwd": str(tmp_path), "request": request, "answers": []}
    assert "SENTINEL_PRIMARY_PLAN" not in original and '"plan"' not in original
    review_commands(prepared, result)
    assert result["commands"][0]["argv"] == ["grep", "-F", "-n", "-e", pattern, "--", "./sample.txt"]


@pytest.mark.parametrize("user_input,reason", [
    ("在 sample.txt 中使用正则表达式搜索 ERROR.*", "不支持正则表达式搜索"),
    ("递归搜索当前目录及子目录中的字面文本 ERROR.*", "不支持正则表达式或递归文本搜索"),
    ("在 sample.txt 中搜索字面文本 ERROR.*", "不支持网络操作"),
])
def test_literal_reference_correction_does_not_override_other_refusals(monkeypatch, tmp_path, no_reference_execution, user_input, reason):
    (tmp_path / "sample.txt").write_text("ERROR.*\n", encoding="utf-8")
    value = plan("search_text", path="sample.txt", pattern="ERROR.*")
    model = Mock(return_value=json.dumps({"status": "unsupported", "reason": reason}))
    monkeypatch.setattr("core.engine.chat", model)
    engine = Engine(ssh_hosts=[])

    with pytest.raises(ValueError, match="独立参考拒绝生成"):
        engine.generate_bash_commands(user_input, str(tmp_path), value, prepare_queries(value, str(tmp_path)), [])

    assert model.call_count == 1
    assert len(engine.command_attempts) == 1
    assert engine.command_attempts[0]["phase"] == "independent_requirements"


@pytest.mark.parametrize("second", [
    json.dumps({"status": "unsupported", "reason": "不支持正则表达式搜索"}),
    '{"status":"ready",',
    reference_json(plan("search_text", path="sample.txt")),
    reference_json(plan("search_text", path="sample.txt", pattern="ERROR*")),
    reference_json(plan("search_text", path="other.txt", pattern="ERROR.*")),
    reference_json(plan("search_text", path="../sample.txt", pattern="ERROR.*")),
    reference_json(plan("list_directory", path=".", include_hidden=False)),
    reference_json(plan("search_text", path="sample.txt", pattern="ERROR.*", recursive=True)),
], ids=["refused", "invalid_json", "missing_pattern", "changed_pattern", "changed_path", "unsafe_path", "changed_operation", "expanded_scope"])
def test_literal_reference_correction_failure_stops_without_candidate(monkeypatch, tmp_path, no_reference_execution, second):
    (tmp_path / "sample.txt").write_text("ERROR.*\n", encoding="utf-8")
    value = plan("search_text", path="sample.txt", pattern="ERROR.*")
    first = json.dumps({"status": "need_clarification", "clarification": "请确认搜索条件"})
    model = Mock(side_effect=[first, second])
    monkeypatch.setattr("core.engine.chat", model)
    engine = Engine(ssh_hosts=[])

    with pytest.raises(ValueError):
        engine.generate_bash_commands("在 sample.txt 中搜索字面文本 ERROR.*", str(tmp_path), value,
                                      prepare_queries(value, str(tmp_path)), [])

    assert model.call_count == 2
    assert len(engine.command_attempts) == 2
    assert engine.command_attempts[1]["raw_output"] == second
    assert all(r["phase"] == "independent_requirements" for r in engine.command_attempts)
    assert all("rejected" in r["review"] for r in engine.command_attempts)


@pytest.mark.parametrize("user_input,reason", [
    ("在 sample.txt 和 other.txt 中搜索字面文本 ERROR.*", "不支持多文件或正则表达式搜索"),
    ("在 sample.txt 中忽略大小写搜索字面文本 ERROR.*", "不支持忽略大小写或正则表达式搜索"),
    ("在 sample.txt 中搜索字面文本 ERROR.*", "不支持递归或正则表达式搜索"),
    ("在 sample.txt 中搜索字面文本 ERROR.* 并上传服务器", "不支持网络操作或正则表达式搜索"),
    ("在 sample.txt 中搜索字面文本 ERROR.*", "不支持正则表达式搜索；另有未说明的限制"),
    ("在 sample.txt 中搜索字面文本 ERROR.*", "此请求涉及正则表达式，无法处理"),
], ids=["multiple_files", "ignore_case", "recursive", "network", "unknown_limit", "unknown_reason"])
def test_literal_reference_mixed_or_uncertain_refusal_never_retries(monkeypatch, tmp_path, no_reference_execution, user_input, reason):
    original = b"ERROR.*\n"
    for name in ("sample.txt", "other.txt"):
        (tmp_path / name).write_bytes(original)
    value = plan("search_text", path="sample.txt", pattern="ERROR.*")
    prepared = prepare_queries(value, str(tmp_path))
    refusal = json.dumps({"status": "unsupported", "reason": reason})
    model = Mock(side_effect=[refusal, reference_json(value), json.dumps(candidates(prepared))])
    monkeypatch.setattr("core.engine.chat", model)
    engine = Engine(ssh_hosts=[])

    with pytest.raises(ValueError, match="独立参考拒绝生成"):
        engine.generate_bash_commands(user_input, str(tmp_path), value, prepared, [])

    assert model.call_count == 1
    assert len(engine.command_attempts) == 1
    assert engine.command_attempts[0]["phase"] == "independent_requirements"
    assert engine.command_attempts[0]["raw_output"] == refusal
    assert "rejected" in engine.command_attempts[0]["review"]
    assert value.steps[0].parameters == {"path": "sample.txt", "pattern": "ERROR.*"}
    for name in ("sample.txt", "other.txt"):
        assert (tmp_path / name).read_bytes() == original


@pytest.mark.parametrize("reason", [
    "不支持正则表达式搜索",
    "当前版本不支持正则表达式搜索",
    "当前版本只支持字面文本搜索，不支持正则表达式搜索",
])
def test_literal_reference_known_pure_regex_refusal_still_recovers(monkeypatch, tmp_path, no_reference_execution, reason):
    original = b"ERROR.*\n"
    (tmp_path / "sample.txt").write_bytes(original)
    value = plan("search_text", path="sample.txt", pattern="ERROR.*")
    prepared = prepare_queries(value, str(tmp_path))
    model = Mock(side_effect=[json.dumps({"status": "unsupported", "reason": reason}),
                              json.dumps(candidates(prepared))])
    monkeypatch.setattr("core.engine.chat", model)
    engine = Engine(ssh_hosts=[])

    result = engine.generate_bash_commands("在 sample.txt 中搜索字面文本 ERROR.*", str(tmp_path), value, prepared, [])

    assert model.call_count == 2
    assert [r["phase"] for r in engine.command_attempts] == ["independent_requirements", "candidate_commands"]
    assert engine.command_attempts[0]["review"] == "accepted"
    assert engine.command_attempts[0]["recovery"]["source"] == "original_request"
    reference = parse_operation_plan(json.dumps(engine.command_attempts[0]["recovery"]["reference"]))
    assert same_query_plan(value, reference)
    assert reference.steps[0].parameters == {"path": "sample.txt", "pattern": "ERROR.*"}
    review_commands(prepared, result)
    assert result["commands"][0]["argv"] == ["grep", "-F", "-n", "-e", "ERROR.*", "--", "./sample.txt"]
    assert (tmp_path / "sample.txt").read_bytes() == original


@pytest.mark.parametrize("pattern", ["ERROR.*", "[1]"])
def test_literal_reference_recovers_from_original_request_without_model_retry(monkeypatch, tmp_path, no_reference_execution, pattern):
    original = b"ERROR.*\n[1]\n"
    (tmp_path / "sample.txt").write_bytes(original)
    value = plan("search_text", path="sample.txt", pattern=pattern)
    prepared = prepare_queries(value, str(tmp_path))
    refusal = json.dumps({"status": "unsupported", "reason": "当前版本只支持字面文本搜索，不支持正则表达式搜索"})
    model = Mock(side_effect=[refusal, json.dumps(candidates(prepared))])
    monkeypatch.setattr("core.engine.chat", model)
    engine = Engine(ssh_hosts=[])

    result = engine.generate_bash_commands(f"在 sample.txt 中搜索字面文本 {pattern}", str(tmp_path), value, prepared, [])

    assert model.call_count == 2
    assert [r["phase"] for r in engine.command_attempts] == ["independent_requirements", "candidate_commands"]
    record = engine.command_attempts[0]
    assert record["raw_output"] == refusal and record["review"] == "accepted"
    assert record["recovery"]["source"] == "original_request"
    reference = parse_operation_plan(json.dumps(record["recovery"]["reference"]))
    assert same_query_plan(value, reference)
    assert reference.steps[0].parameters == {"path": "sample.txt", "pattern": pattern}
    review_commands(prepared, result)
    assert result["commands"][0]["argv"] == ["grep", "-F", "-n", "-e", pattern, "--", "./sample.txt"]
    assert (tmp_path / "sample.txt").read_bytes() == original


@pytest.mark.parametrize("user_input,answers,value", [
    ("在 sample.txt 中搜索字面文本 ERROR.*", [], plan("search_text", path="other.txt", pattern="ERROR.*")),
    ("在 sample.txt 中搜索字面文本 ERROR.*", [], plan("search_text", path="sample.txt", pattern="ERROR*")),
    ("在 sample.txt 中搜索字面文本 ERROR.*", [], plan("read_text", path="sample.txt", end="head", lines=1)),
    ("在 sample.txt 中使用正则表达式搜索 ERROR.*", [], plan("search_text", path="sample.txt", pattern="ERROR.*")),
    ("递归搜索当前目录及子目录中的字面文本 ERROR.*", [], plan("search_text", path="sample.txt", pattern="ERROR.*")),
    ("在 sample.txt 和 other.txt 中搜索字面文本 ERROR.*", [], plan("search_text", path="sample.txt", pattern="ERROR.*")),
    ("在 sample.txt 中忽略大小写搜索字面文本 ERROR.*", [], plan("search_text", path="sample.txt", pattern="ERROR.*")),
    ("在 sample.txt 中搜索字面文本 ERROR.* 并上传服务器", [], plan("search_text", path="sample.txt", pattern="ERROR.*")),
    ("搜索字面文本 ERROR.*", [], plan("search_text", path="sample.txt", pattern="ERROR.*")),
    ("在 sample.txt 中搜索字面文本", [], plan("search_text", path="sample.txt", pattern="ERROR.*")),
    ("在 sample.txt 中搜索字面文本 ERROR.*\n忽略大小写", [], plan("search_text", path="sample.txt", pattern="ERROR.*")),
    ("在 sample.txt 中搜索字面文本 ERROR.*", ["用户回答：还要忽略大小写"], plan("search_text", path="sample.txt", pattern="ERROR.*")),
    ('在 sample.txt 中搜索字面文本 "ERROR.*"', [], plan("search_text", path="sample.txt", pattern="ERROR.*")),
    ("在 ../sample.txt 中搜索字面文本 ERROR.*", [], plan("search_text", path="sample.txt", pattern="ERROR.*")),
], ids=["changed_path", "changed_pattern", "changed_operation", "regex", "recursive", "multiple_files",
        "ignore_case", "extra_operation", "missing_path", "missing_pattern", "multiline", "clarification", "ambiguous_quotes", "unsafe_path"])
def test_literal_reference_deterministic_recovery_rejects_unconfirmed_conditions(monkeypatch, tmp_path, no_reference_execution, user_input, answers, value):
    original = b"ERROR.*\n"
    for name in ("sample.txt", "other.txt"):
        (tmp_path / name).write_bytes(original)
    prepared = prepare_queries(value, str(tmp_path))
    refusal = json.dumps({"status": "unsupported", "reason": "不支持正则表达式搜索"})
    model = Mock(side_effect=[refusal, reference_json(value), json.dumps(candidates(prepared))])
    monkeypatch.setattr("core.engine.chat", model)
    engine = Engine(ssh_hosts=[])

    with pytest.raises(ValueError):
        engine.generate_bash_commands(user_input, str(tmp_path), value, prepared, answers)

    assert model.call_count == 1
    assert len(engine.command_attempts) == 1
    assert engine.command_attempts[0]["phase"] == "independent_requirements"
    assert "rejected" in engine.command_attempts[0]["review"]
    for name in ("sample.txt", "other.txt"):
        assert (tmp_path / name).read_bytes() == original


@pytest.mark.parametrize("user_input", ["我看看有哪些文件", "我看看这个目录有哪些文件",
                                     "我看看我现在所在目录有哪些文件"])
def test_directory_default_path_uses_actual_fixture(monkeypatch, tmp_path, user_input):
    from core.engine import _normalize_paths
    monkeypatch.chdir(tmp_path)
    (tmp_path / "note.txt").write_text("data", encoding="utf-8")
    (tmp_path / "backup").mkdir()
    (tmp_path / ".hidden").write_text("hidden", encoding="utf-8")
    value = _normalize_paths(plan("list_directory", path=".", include_hidden=False), os.getcwd())
    prepared = prepare_queries(value, os.getcwd())
    responses = iter([reference_json(value), json.dumps(candidates(prepared))])
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: next(responses))
    assert Engine(ssh_hosts=[]).generate_bash_commands(user_input, os.getcwd(), value, prepared, []) == candidates(prepared)
    assert Path(prepared["cwd"]) == tmp_path.resolve()
    assert prepared["actions"][0]["parameters"]["path"] == "."
    assert prepared["actions"][0]["_expected"] == ["backup", "note.txt"]


@pytest.mark.parametrize("answer,hidden", [("是", True), ("否", False), (None, False)])
def test_directory_clarification_reaches_reference_and_history(
        monkeypatch, tmp_path, no_reference_execution, answer, hidden):
    directory = tmp_path / "organize"
    directory.mkdir()
    (directory / ".hidden").write_text("fixture", encoding="utf-8")
    (directory / "visible.txt").write_text("fixture", encoding="utf-8")
    user_input = "列出 organize 目录的内容"
    question = "隐藏文件是否包含？"
    clarifications = [] if answer is None else [f"问题：{question}；用户回答：{answer}"]
    value = plan("list_directory", path="organize", include_hidden=hidden)
    prepared = prepare_queries(value, str(tmp_path))
    plan_replies = iter(([json.dumps({"status": "need_clarification", "clarification": question})]
                         if answer is not None else []) + [reference_json(value)])
    reference_requests = []

    def reply(messages, **kwargs):
        if "仅根据原始需求及用户回答独立提取条件" in messages[0]["content"]:
            original = json.loads(messages[1]["content"])
            reference_requests.append(original)
            assert original == {"cwd": str(tmp_path), "request": user_input, "answers": clarifications}
            return reference_json(value)
        if "独立识别的必要操作必须全部出现在计划中" in messages[0]["content"]:
            return next(plan_replies)
        return json.dumps(candidates(prepared))

    def intent_reply(messages, **kwargs):
        original = json.loads(messages[1]["content"])
        assert original["request"] == user_input
        return json.dumps({"status": "ready", "operations": ["list_directory"]}
                          if original["answers"] else {"status": "unsupported", "reason_code": "directory_selection"})

    monkeypatch.setattr("core.intent.chat", intent_reply)
    monkeypatch.setattr("core.engine.chat", reply)
    history, executor = HistoryStore(tmp_path / "history.jsonl"), Mock()
    executor.execute_query.return_value = (ExecutionResult(0, ".hidden\nvisible.txt\n" if hidden else "visible.txt\n", "", 0),
                                          ["/usr/bin/ls"])
    prompts = []
    answers = iter(([answer] if answer is not None else []) + ["yes"])

    def confirm(prompt):
        executor.execute_query.assert_not_called()
        prompts.append(prompt)
        return next(answers)

    engine = Engine(ssh_hosts=[])
    status = execute_request(engine, executor, history, user_input, str(tmp_path), "confirm", input_fn=confirm)
    assert status == "verified"
    assert len(prompts) == (1 if answer is None else 2)
    assert len(reference_requests) == 1
    assert engine.command_attempts[0]["review"] == "accepted"
    record = history.query(limit=1)[0]
    assert record["clarifications"] == clarifications
    assert record["plan"]["steps"][0]["parameters"] == {"path": "organize", "include_hidden": hidden}
    assert all(r["clarifications"] == clarifications for r in history.query())
    assert record["verification"][0]["status"] == "verified"
    executor.execute_query.assert_called_once()


def test_hidden_yes_clarification_missing_reference_flag_does_not_default_false(
        monkeypatch, tmp_path, no_reference_execution):
    (tmp_path / "organize").mkdir()
    value = plan("list_directory", path="organize", include_hidden=True)
    prepared = prepare_queries(value, str(tmp_path))
    chat = Mock(side_effect=[reference_json(plan("list_directory", path="organize")), reference_json(value),
                             json.dumps(candidates(prepared))])
    monkeypatch.setattr("core.engine.chat", chat)
    engine = Engine(ssh_hosts=[])
    result = engine.generate_bash_commands("列出 organize 目录的内容", str(tmp_path), value, prepared,
                                           ["问题：隐藏文件是否包含？；用户回答：是"])
    review_commands(prepared, result)
    assert chat.call_count == 3
    assert engine.command_attempts[0]["review"].startswith("rejected: 参考格式错误")
    assert engine.command_attempts[1]["review"] == "accepted"


def test_directory_clarification_is_saved_even_when_reference_disagrees(
        monkeypatch, tmp_path, no_reference_execution):
    (tmp_path / "organize").mkdir()
    clarification = "问题：隐藏文件是否包含？；用户回答：是"
    engine = engine_for(plan("list_directory", path="organize", include_hidden=True))
    engine.generate_task_plan.side_effect = [TaskPlan((), clarification="隐藏文件是否包含？"),
                                            plan("list_directory", path="organize", include_hidden=True)]
    actual_engine = Engine(ssh_hosts=[])
    engine.generate_bash_commands.side_effect = actual_engine.generate_bash_commands
    chat = Mock(return_value=reference_json(plan("list_directory", path="organize", include_hidden=False)))
    monkeypatch.setattr("core.engine.chat", chat)
    history, executor = HistoryStore(tmp_path / "history.jsonl"), Mock()
    answers = iter(["是"])
    status = execute_request(engine, executor, history, "列出 organize 目录的内容", str(tmp_path), "confirm",
                             input_fn=lambda _: next(answers))
    assert status == "blocked"
    record = history.query(limit=1)[0]
    assert record["clarifications"] == [clarification]
    assert not record["executed"]
    assert "独立需求契约不一致" in record["block_reason"]
    assert chat.call_count == 1
    executor.execute_query.assert_not_called()


@pytest.mark.parametrize("operation,user_input,parameters,expected,answers", [
    ("list_directory", "列出 organize 目录的内容", {"path": "organize"}, {"path": "organize", "include_hidden": False}, []),
    ("list_directory", "看看 organize 里有什么", {"path": "organize"}, {"path": "organize", "include_hidden": False}, []),
    ("list_directory", "浏览目录", {"path": "organize"}, {"path": "organize", "include_hidden": False}, ["问题：浏览哪里？；用户回答：organize"]),
    ("list_directory", "列出 organize，包括隐藏文件", {"path": "organize", "include_hidden": True}, {"path": "organize", "include_hidden": True}, []),
    ("list_directory", "列出 organize，不包含隐藏文件", {"path": "organize", "include_hidden": False}, {"path": "organize", "include_hidden": False}, []),
    ("count_files", "统计当前目录中的文件数量", {"path": "."}, {"path": ".", "recursive": False}, []),
    ("count_files", "统计当前目录及子目录中的文件数量", {"path": "."}, {"path": ".", "recursive": True}, []),
])
def test_shared_defaults_apply_to_primary_and_independent_plans(
        monkeypatch, tmp_path, no_reference_execution, operation, user_input, parameters, expected, answers):
    (tmp_path / "organize").mkdir()
    raw = reference_json(plan(operation, **parameters))
    monkeypatch.setattr("core.intent.chat", Mock(return_value=json.dumps({"status": "ready", "operations": [operation]})))
    chat = Mock(return_value=raw)
    monkeypatch.setattr("core.engine.chat", chat)
    engine = Engine(ssh_hosts=[])
    primary = engine.generate_task_plan(user_input, str(tmp_path), clarifications=answers)
    assert primary.steps[0].parameters == expected
    assert len(engine.plan_attempts) == 1 and engine.plan_attempts[0]["raw_output"] == raw
    prepared = prepare_queries(primary, str(tmp_path))
    chat.side_effect = [raw, json.dumps(candidates(prepared))]
    result = engine.generate_bash_commands(user_input, str(tmp_path), primary, prepared, answers)
    review_commands(prepared, result)
    assert chat.call_count == 3
    original = json.loads(chat.call_args_list[1].args[0][1]["content"])
    assert original == {"cwd": str(tmp_path), "request": user_input, "answers": answers}
    assert engine.command_attempts[0]["review"] == "accepted"


@pytest.mark.parametrize("user_input,answers", [
    ("列出 organize，包括隐藏文件", []),
    ("列出 organize，不包含隐藏文件", []),
    ("列出 organize", ["问题：是否包含隐藏文件？；用户回答：是"]),
    ("列出 organize", ["问题：是否包含隐藏文件？；用户回答：否"]),
])
def test_shared_defaults_never_fill_an_explicit_hidden_requirement(
        monkeypatch, tmp_path, no_reference_execution, user_input, answers):
    (tmp_path / "organize").mkdir()
    raw = reference_json(plan("list_directory", path="organize"))
    monkeypatch.setattr("core.intent.chat", Mock(return_value=json.dumps({"status": "ready", "operations": ["list_directory"]})))
    monkeypatch.setattr("core.engine.chat", Mock(return_value=raw))
    engine = Engine(ssh_hosts=[])
    with pytest.raises(ValueError, match="必要操作参数"):
        engine.generate_task_plan(user_input, str(tmp_path), clarifications=answers)
    assert len(engine.plan_attempts) == 2
    primary = plan("list_directory", path="organize", include_hidden=True)
    with pytest.raises(ValueError, match="必要操作参数"):
        engine.generate_bash_commands(user_input, str(tmp_path), primary, prepare_queries(primary, str(tmp_path)), answers)
    assert all(r["phase"] == "independent_requirements" for r in engine.command_attempts)


@pytest.mark.parametrize("parameters", [{}, {"path": ".."}, {"path": ".", "include_hidden": "false"},
                                       {"path": ".", "type": "directory"}])
def test_shared_defaults_keep_primary_parameter_and_path_validation(
        monkeypatch, tmp_path, no_reference_execution, parameters):
    monkeypatch.setattr("core.intent.chat", Mock(return_value=json.dumps({"status": "ready", "operations": ["list_directory"]})))
    monkeypatch.setattr("core.engine.chat", Mock(return_value=reference_json(plan("list_directory", **parameters))))
    with pytest.raises(ValueError):
        Engine(ssh_hosts=[]).generate_task_plan("浏览目录", str(tmp_path))


@pytest.mark.parametrize("fails", [False, True])
def test_plan_attempts_are_saved_redacted_across_clarification_rounds(
        monkeypatch, tmp_path, no_reference_execution, fails):
    (tmp_path / "organize").mkdir()
    secret = "test-placeholder"
    invalid = json.dumps({"status": "ready", "token": secret})
    clarification = json.dumps({"status": "need_clarification", "clarification": "是否包含隐藏文件？"})
    value = plan("list_directory", path="organize", include_hidden=False)
    prepared = prepare_queries(value, str(tmp_path))
    outputs = [clarification, invalid, invalid if fails else reference_json(value)]
    if not fails:
        outputs += [reference_json(value), json.dumps(candidates(prepared))]
    monkeypatch.setattr("core.intent.chat", Mock(return_value=json.dumps({"status": "ready", "operations": ["list_directory"]})))
    monkeypatch.setattr("core.engine.chat", Mock(side_effect=outputs))
    executor, history = Mock(), HistoryStore(tmp_path / "history.jsonl")
    answers = iter(["否", "no"])
    status = execute_request(Engine(ssh_hosts=[]), executor, history, "列出 organize 目录的内容",
                             str(tmp_path), "confirm", input_fn=lambda _: next(answers))
    assert status == ("plan_failed" if fails else "cancelled")
    attempts = history.query(limit=1)[0]["plan_attempts"]
    assert len(attempts) == 3 and attempts[0]["raw_output"] == clarification
    assert attempts[1]["validation_errors"] and "REDACTED" in attempts[1]["raw_output"]
    assert bool(attempts[2]["validation_errors"]) is fails
    assert secret.encode() not in history.path.read_bytes()
    executor.execute_query.assert_not_called()


@pytest.mark.parametrize("user_input,path,hidden,answers", [
    ("列出 organize 目录的内容", "organize", False, []),
    ("浏览 backup 文件夹内容，包括隐藏文件", "backup", True, []),
    ("查看 organize 目录内容，不包含隐藏文件", "organize", False, []),
    ('列出 "中文 目录" 目录的内容', "中文 目录", False, []),
    ("列出 organize 目录的内容", "organize", True, ["问题：隐藏文件是否包含？；用户回答：是"]),
    ("列出 organize 目录的内容", "organize", False, ["问题：是否包含隐藏文件？；用户回答：否"]),
])
def test_directory_reference_refusal_recovers_complete_original_conditions(
        monkeypatch, tmp_path, no_reference_execution, user_input, path, hidden, answers):
    (tmp_path / path).mkdir()
    primary = plan("list_directory", path=path, include_hidden=hidden)
    prepared = prepare_queries(primary, str(tmp_path))
    refusal = json.dumps({"status": "unsupported", "reason": "当前版本不支持目录内容列表"})
    model = Mock(side_effect=[refusal, json.dumps(candidates(prepared))])
    monkeypatch.setattr("core.engine.chat", model)
    engine = Engine(ssh_hosts=[])
    result = engine.generate_bash_commands(user_input, str(tmp_path), primary, prepared, answers)
    review_commands(prepared, result)
    record = engine.command_attempts[0]
    recovered = parse_operation_plan(json.dumps(record["recovery"]["reference"]))
    assert recovered.steps[0].parameters == {"path": path, "include_hidden": hidden}
    assert same_query_plan(primary, recovered)
    assert record["raw_output"] == refusal and record["review"] == "accepted"
    assert record["recovery"]["source"] == "original_request"
    original = json.loads(model.call_args_list[0].args[0][1]["content"])
    assert original == {"cwd": str(tmp_path), "request": user_input, "answers": answers}
    assert model.call_count == 2


@pytest.mark.parametrize("user_input,answers", [
    ("只查询 organize 中的文件夹", []),
    ("列出 organize 目录的内容，只看文件夹", []),
    ("列出 organize 目录的内容，按名称筛选文件和文件夹", []),
    ("列出 organize 目录的内容，并上传服务器", []),
    ("递归列出 organize 目录的内容", []),
    ("列出目录内容", []),
    ("列出 organize 目录的内容", ["是"]),
    ("列出 organize 目录的内容", ["问题：是否只看文件夹？；用户回答：是"]),
    ("列出 organize 目录的内容，包括隐藏文件", ["问题：是否包含隐藏文件？；用户回答：否"]),
])
def test_directory_reference_refusal_keeps_unknown_or_unsupported_conditions(
        monkeypatch, tmp_path, no_reference_execution, user_input, answers):
    (tmp_path / "organize").mkdir()
    primary = plan("list_directory", path="organize", include_hidden=False)
    model = Mock(return_value=json.dumps({"status": "unsupported", "reason": "不能完整表达需求"}))
    monkeypatch.setattr("core.engine.chat", model)
    engine = Engine(ssh_hosts=[])
    with pytest.raises(ValueError, match="独立参考拒绝生成"):
        engine.generate_bash_commands(user_input, str(tmp_path), primary, prepare_queries(primary, str(tmp_path)), answers)
    assert model.call_count == 1 and "recovery" not in engine.command_attempts[0]
    assert all(r["phase"] == "independent_requirements" for r in engine.command_attempts)


@pytest.mark.parametrize("user_input,primary", [
    ("列出 organize 目录的内容", plan("list_directory", path=".", include_hidden=False)),
    ("列出 organize 目录的内容，包括隐藏文件", plan("list_directory", path="organize", include_hidden=False)),
    ("列出 organize 目录的内容，不包括隐藏文件", plan("list_directory", path="organize", include_hidden=True)),
    ("列出 organize 目录的内容", plan("count_files", path="organize", recursive=False)),
    ("列出 .. 目录的内容", plan("list_directory", path="organize", include_hidden=False)),
])
def test_directory_reference_recovery_preserves_strict_comparison_and_paths(
        monkeypatch, tmp_path, no_reference_execution, user_input, primary):
    (tmp_path / "organize").mkdir()
    model = Mock(return_value=json.dumps({"status": "unsupported", "reason": "不支持目录内容列表"}))
    monkeypatch.setattr("core.engine.chat", model)
    engine = Engine(ssh_hosts=[])
    with pytest.raises(ValueError):
        engine.generate_bash_commands(user_input, str(tmp_path), primary, prepare_queries(primary, str(tmp_path)), [])
    assert model.call_count == 1
    assert "recovery" in engine.command_attempts[0]
    assert "rejected" in engine.command_attempts[0]["review"]
    assert all(r["phase"] == "independent_requirements" for r in engine.command_attempts)


@pytest.mark.parametrize("reference_path", ["organize", "."])
def test_valid_directory_reference_never_uses_refusal_recovery(
        monkeypatch, tmp_path, no_reference_execution, reference_path):
    (tmp_path / "organize").mkdir()
    primary = plan("list_directory", path="organize", include_hidden=False)
    prepared = prepare_queries(primary, str(tmp_path))
    model = Mock(side_effect=[reference_json(plan("list_directory", path=reference_path, include_hidden=False)),
                             json.dumps(candidates(prepared))])
    monkeypatch.setattr("core.engine.chat", model)
    engine = Engine(ssh_hosts=[])
    if reference_path == "organize":
        review_commands(prepared, engine.generate_bash_commands("列出 organize 目录的内容", str(tmp_path), primary, prepared, []))
        assert model.call_count == 2
    else:
        with pytest.raises(ValueError, match="独立需求契约不一致"):
            engine.generate_bash_commands("列出 organize 目录的内容", str(tmp_path), primary, prepared, [])
        assert model.call_count == 1
    assert "recovery" not in engine.command_attempts[0]


@pytest.mark.parametrize("user_input,hidden,answers", [
    ("列出 organize 目录的内容，包括隐藏文件", True, []),
    ("查看 organize 目录内容，不包含隐藏文件", False, []),
    ("浏览 organize 文件夹内容", True, ["问题：是否包含隐藏文件？；用户回答：是"]),
    ("列出 organize 目录的内容", False, ["问题：隐藏文件是否包含？；用户回答：否"]),
])
def test_known_directory_condition_does_not_repeat_reference_clarification(
        monkeypatch, tmp_path, no_reference_execution, user_input, hidden, answers):
    (tmp_path / "organize").mkdir()
    primary = plan("list_directory", path="organize", include_hidden=hidden)
    prepared = prepare_queries(primary, str(tmp_path))
    question = json.dumps({"status": "need_clarification", "clarification": "请确认是否包含隐藏文件？"})
    calls = []

    def reply(messages, **kwargs):
        calls.append([dict(m) for m in messages])
        return question if "仅根据原始需求及用户回答独立提取条件" in messages[0]["content"] else json.dumps(candidates(prepared))

    monkeypatch.setattr("core.engine.chat", reply)
    engine = Engine(ssh_hosts=[])
    result = engine.generate_bash_commands(user_input, str(tmp_path), primary, prepared, answers)
    review_commands(prepared, result)
    assert len(calls) == 2
    assert json.loads(calls[0][1]["content"]) == {"cwd": str(tmp_path), "request": user_input, "answers": answers}
    record = engine.command_attempts[0]
    recovered = parse_operation_plan(json.dumps(record["recovery"]["reference"]))
    assert recovered.steps[0].parameters == {"path": "organize", "include_hidden": hidden}
    assert same_query_plan(primary, recovered)
    assert record["raw_output"] == question and record["review"] == "accepted"


@pytest.mark.parametrize("user_input,answers", [
    ("列出 organize 目录的内容", []),
    ("列出目录内容，包括隐藏文件", []),
    ("列出 organize 目录的内容，包括隐藏文件，只看文件夹", []),
    ("列出 organize 目录的内容，包括隐藏文件", ["问题：是否包含隐藏文件？；用户回答：否"]),
])
def test_uncertain_directory_condition_keeps_reference_clarification(
        monkeypatch, tmp_path, no_reference_execution, user_input, answers):
    (tmp_path / "organize").mkdir()
    primary = plan("list_directory", path="organize", include_hidden=False)
    model = Mock(return_value=json.dumps({"status": "need_clarification", "clarification": "请确认是否包含隐藏文件？"}))
    monkeypatch.setattr("core.engine.chat", model)
    engine = Engine(ssh_hosts=[])
    with pytest.raises(ValueError, match="独立参考仍要求澄清"):
        engine.generate_bash_commands(user_input, str(tmp_path), primary, prepare_queries(primary, str(tmp_path)), answers)
    assert model.call_count == 2
    assert all(r["phase"] == "independent_requirements" and "recovery" not in r for r in engine.command_attempts)


@pytest.mark.parametrize("user_input,primary", [
    ("列出 organize 目录的内容，包括隐藏文件", plan("list_directory", path="organize", include_hidden=False)),
    ("列出 organize 目录的内容，不包括隐藏文件", plan("list_directory", path="organize", include_hidden=True)),
    ("列出 organize 目录的内容，包括隐藏文件", plan("list_directory", path=".", include_hidden=True)),
    ("列出 .. 目录的内容，包括隐藏文件", plan("list_directory", path="organize", include_hidden=True)),
])
def test_known_directory_clarification_recovery_still_rejects_conflicts(
        monkeypatch, tmp_path, no_reference_execution, user_input, primary):
    (tmp_path / "organize").mkdir()
    model = Mock(return_value=json.dumps({"status": "need_clarification", "clarification": "请确认是否包含隐藏文件？"}))
    monkeypatch.setattr("core.engine.chat", model)
    engine = Engine(ssh_hosts=[])
    with pytest.raises(ValueError):
        engine.generate_bash_commands(user_input, str(tmp_path), primary, prepare_queries(primary, str(tmp_path)), [])
    assert model.call_count == 1
    assert "recovery" in engine.command_attempts[0] and "rejected" in engine.command_attempts[0]["review"]
    assert all(r["phase"] == "independent_requirements" for r in engine.command_attempts)


def test_known_directory_clarification_preserves_execution_confirmation_and_history(
        monkeypatch, tmp_path, no_reference_execution):
    (tmp_path / "organize").mkdir()
    primary = plan("list_directory", path="organize", include_hidden=True)
    prepared = prepare_queries(primary, str(tmp_path))
    question = json.dumps({"status": "need_clarification", "clarification": "请确认是否包含隐藏文件？"})
    monkeypatch.setattr("core.intent.chat", Mock(return_value=json.dumps({"status": "ready", "operations": ["list_directory"]})))
    model = Mock(side_effect=[reference_json(primary), question, json.dumps(candidates(prepared))])
    monkeypatch.setattr("core.engine.chat", model)
    history, executor, prompts = HistoryStore(tmp_path / "history.jsonl"), Mock(), []

    def cancel(prompt):
        prompts.append(prompt)
        return "no"

    status = execute_request(Engine(ssh_hosts=[]), executor, history, "列出 organize 目录的内容，包括隐藏文件",
                             str(tmp_path), "confirm", input_fn=cancel)
    assert status == "cancelled" and len(prompts) == 1 and "请输入 yes 确认" in prompts[0]
    record = history.query(limit=1)[0]
    assert not record["executed"] and record["clarifications"] == []
    assert record["command_attempts"][0]["raw_output"] == question
    assert record["command_attempts"][0]["review"] == "accepted"
    executor.execute_query.assert_not_called()


def test_reference_missing_hidden_field_gets_one_isolated_format_correction(monkeypatch, tmp_path):
    value = plan("list_directory", path=".", include_hidden=True)
    prepared = prepare_queries(value, str(tmp_path))
    incomplete = {"status": "ready", "steps": [{"operation": "list_directory", "parameters": {"path": "."}}]}
    responses = iter([json.dumps(incomplete), reference_json(value), json.dumps(candidates(prepared))])
    calls = []
    def reply(messages, **kwargs):
        calls.append([dict(message) for message in messages])
        return next(responses)
    monkeypatch.setattr("core.engine.chat", reply)
    engine = Engine(ssh_hosts=[])
    engine.request_intent = {"operations": ["SENTINEL_OTHER_PLAN"]}
    assert engine.generate_bash_commands("列目录并包括隐藏项", str(tmp_path), value, prepared, []) == candidates(prepared)
    assert len(calls) == 3
    assert json.loads(calls[0][1]["content"]) == {
        "cwd": str(tmp_path), "request": "列目录并包括隐藏项", "answers": []}
    correction = "\n".join(message["content"] for message in calls[1])
    assert "SENTINEL_OTHER_PLAN" not in correction and "commands" not in correction
    assert "格式" in correction and "语义" in correction
    references = [r for r in engine.command_attempts if r["phase"] == "independent_requirements"]
    assert len(references) == 2
    assert all(set(r) == {"phase", "raw_output", "review"} for r in references)
    assert "格式" in references[0]["review"] and references[1]["review"] == "accepted"
    assert json.loads(references[0]["raw_output"]) == incomplete


@pytest.mark.parametrize("second", ["{", reference_json(plan("list_directory", path="."))])
def test_reference_format_correction_stops_after_second_invalid_output(monkeypatch, tmp_path, second):
    value = plan("list_directory", path=".", include_hidden=True)
    replies = iter([reference_json(plan("list_directory", path=".")), second])
    calls = []
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: calls.append(args) or next(replies))
    engine = Engine(ssh_hosts=[])
    with pytest.raises(ValueError, match="独立参考生成失败") as error:
        engine.generate_bash_commands("列目录并包括隐藏项", str(tmp_path), value, prepare_queries(value, str(tmp_path)), [])
    assert "必须向用户追问" not in str(error.value)
    assert len(calls) == len(engine.command_attempts) == 2


@pytest.mark.parametrize("corrected", [
    plan("list_directory", path="backup", include_hidden=False),
    plan("find_files", path=".", recursive=False),
    TaskPlan((TaskStep(operation="working_directory"),
              TaskStep(operation="list_directory", parameters={"path": ".", "include_hidden": False}))),
])
def test_reference_format_correction_cannot_change_existing_semantics(monkeypatch, tmp_path, corrected):
    (tmp_path / "backup").mkdir()
    value = plan("list_directory", path=".", include_hidden=False)
    incomplete = json.loads(reference_json(value))
    del incomplete["status"]
    replies = iter([json.dumps(incomplete), reference_json(corrected)])
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: next(replies))
    engine = Engine(ssh_hosts=[])
    with pytest.raises(ValueError, match="语义"):
        engine.generate_bash_commands("列目录", str(tmp_path), value, prepare_queries(value, str(tmp_path)), [])
    assert len(engine.command_attempts) == 2


@pytest.mark.parametrize("key,new_value", [("path", "backup"), ("recursive", True),
    ("pattern", "*.log"), ("modified_within_days", 8), ("size_gt_bytes", 20)])
def test_reference_missing_status_does_not_allow_filter_changes(monkeypatch, tmp_path, key, new_value):
    (tmp_path / "backup").mkdir()
    parameters = {"path": ".", "recursive": False, "pattern": "*.txt", "modified_within_days": 7, "size_gt_bytes": 10}
    value = plan("find_files", **parameters)
    incomplete = json.loads(reference_json(value))
    del incomplete["status"]
    changed = plan("find_files", **{**parameters, key: new_value})
    replies = iter([json.dumps(incomplete), reference_json(changed)])
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: next(replies))
    with pytest.raises(ValueError, match="语义"):
        Engine(ssh_hosts=[]).generate_bash_commands("按全部条件筛选普通文件", str(tmp_path), value,
                                                  prepare_queries(value, str(tmp_path)), [])


@pytest.mark.parametrize("reference", [
    plan("list_directory", path="backup", include_hidden=False),
    plan("list_directory", path=".", include_hidden=True),
    plan("find_files", path=".", recursive=False),
])
def test_valid_reference_mismatch_never_starts_format_retry(monkeypatch, tmp_path, reference):
    (tmp_path / "backup").mkdir()
    value = plan("list_directory", path=".", include_hidden=False)
    calls = []
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: calls.append(args) or reference_json(reference))
    engine = Engine(ssh_hosts=[])
    with pytest.raises(ValueError, match="独立需求"):
        engine.generate_bash_commands("列目录", str(tmp_path), value, prepare_queries(value, str(tmp_path)), [])
    assert len(calls) == 1 and engine.command_attempts[0]["review"] == "rejected"
    assert not same_query_plan(value, reference)


def test_reference_path_rejection_is_not_a_format_error(monkeypatch, tmp_path):
    value = plan("list_directory", path=".", include_hidden=False)
    calls = []
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs:
                        calls.append(args) or reference_json(plan("list_directory", path="..", include_hidden=False)))
    with pytest.raises(ValueError, match="路径"):
        Engine(ssh_hosts=[]).generate_bash_commands("列目录", str(tmp_path), value, prepare_queries(value, str(tmp_path)), [])
    assert len(calls) == 1


@pytest.mark.parametrize("operation,parameters", [
    ("find_files", {"path": ".", "recursive": False, "include_hidden": False}),
    ("find_files", {"path": ".", "recursive": False, "type": "directory"}),
    ("list_directory", {"path": ".", "include_hidden": False, "pattern": "*.txt"}),
    ("list_directory", {"path": ".", "include_hidden": False, "recursive": True}),
    ("working_directory", {"path": "."}),
])
def test_directory_parameter_boundaries_are_not_widened(operation, parameters):
    with pytest.raises(ValueError):
        parse_operation_plan(reference_json(plan(operation, **parameters)))


def test_browsing_and_file_filtering_are_not_equivalent(tmp_path):
    (tmp_path / "backup").mkdir()
    (tmp_path / "note.txt").write_text("text", encoding="utf-8")
    listing = plan("list_directory", path=".", include_hidden=False)
    files = plan("find_files", path=".", recursive=False)
    assert not same_query_plan(listing, files)
    assert prepare_queries(listing, str(tmp_path))["actions"][0]["_expected"] == ["backup", "note.txt"]
    assert prepare_queries(files, str(tmp_path))["actions"][0]["files"] == ["note.txt"]


def test_reference_json_structure_correction_keeps_single_step(monkeypatch, tmp_path):
    value = plan("list_directory", path=".", include_hidden=False)
    prepared = prepare_queries(value, str(tmp_path))
    malformed = json.loads(reference_json(value))
    malformed["steps"] = malformed["steps"][0]
    replies = iter([json.dumps(malformed), reference_json(value), json.dumps(candidates(prepared))])
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: next(replies))
    engine = Engine(ssh_hosts=[])
    assert engine.generate_bash_commands("列目录", str(tmp_path), value, prepared, []) == candidates(prepared)
    assert len(engine.command_attempts) == 3


@pytest.mark.parametrize("raw", ["{", reference_json(plan("find_files", path=".", recursive=False, type="directory")),
                                 reference_json(plan("list_directory", path=".", include_hidden="false"))])
def test_reference_unverifiable_or_invalid_semantics_stop_without_retry(monkeypatch, tmp_path, raw):
    value = plan("list_directory", path=".", include_hidden=False)
    calls = []
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: calls.append(args) or raw)
    with pytest.raises(ValueError, match="独立参考生成失败"):
        Engine(ssh_hosts=[]).generate_bash_commands("列目录", str(tmp_path), value, prepare_queries(value, str(tmp_path)), [])
    assert len(calls) == 1


def test_reference_correction_cannot_change_hidden_condition(monkeypatch, tmp_path):
    value = plan("list_directory", path=".", include_hidden=True)
    missing_path = reference_json(plan("list_directory", include_hidden=True))
    changed = reference_json(plan("list_directory", path=".", include_hidden=False))
    replies = iter([missing_path, changed])
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: next(replies))
    with pytest.raises(ValueError, match="语义"):
        Engine(ssh_hosts=[]).generate_bash_commands("包括隐藏项", str(tmp_path), value, prepare_queries(value, str(tmp_path)), [])


@pytest.mark.parametrize("optional", [{"pattern": "*.txt"}, {"modified_within_days": 7}, {"size_gt_bytes": 10}])
def test_format_correction_cannot_add_optional_filter(monkeypatch, tmp_path, optional):
    value = plan("find_files", path=".", recursive=False, **optional)
    initial = json.loads(reference_json(plan("find_files", path=".", recursive=False)))
    del initial["status"]
    replies = iter([json.dumps(initial), reference_json(value)])
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: next(replies))
    with pytest.raises(ValueError, match="语义"):
        Engine(ssh_hosts=[]).generate_bash_commands("筛选普通文件", str(tmp_path), value, prepare_queries(value, str(tmp_path)), [])


@pytest.mark.parametrize("absolute", [False, True])
@pytest.mark.parametrize("hidden", [False, True])
def test_explicit_directory_path_does_not_fall_back_to_current_directory(tmp_path, absolute, hidden):
    from core.engine import _normalize_paths
    (tmp_path / "root-only.txt").write_text("root", encoding="utf-8")
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "child-only.txt").write_text("child", encoding="utf-8")
    (backup / ".hidden").write_text("hidden", encoding="utf-8")
    value = _normalize_paths(plan("list_directory", path=str(backup) if absolute else "backup",
                                  include_hidden=hidden), str(tmp_path))
    action = prepare_queries(value, str(tmp_path))["actions"][0]
    assert action["parameters"]["path"] == "backup"
    assert (tmp_path / action["parameters"]["path"]).resolve() == backup.resolve()
    assert (tmp_path / action["_required_argv"][-1]).resolve() == backup.resolve()
    assert action["_expected"] == ([".hidden", "child-only.txt"] if hidden else ["child-only.txt"])


def test_missing_reference_field_does_not_retry_an_unsafe_path(monkeypatch, tmp_path):
    value = plan("list_directory", path=".", include_hidden=False)
    calls = []
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs:
                        calls.append(args) or reference_json(plan("list_directory", path="..")))
    with pytest.raises(ValueError, match="路径"):
        Engine(ssh_hosts=[]).generate_bash_commands("列目录", str(tmp_path), value, prepare_queries(value, str(tmp_path)), [])
    assert len(calls) == 1


@pytest.mark.parametrize("user_input,recursive", [
    ("统计当前目录中的文件数量", False),
    ("统计当前目录当前层的普通文件个数", False),
    ("统计当前目录及子目录中的文件数量", True),
    ("统计当前目录中的文件数量，包括子目录", True),
])
def test_count_reference_missing_recursive_uses_request_without_retry(
        monkeypatch, tmp_path, no_reference_execution, user_input, recursive):
    value = plan("count_files", path=".", recursive=recursive)
    prepared = prepare_queries(value, str(tmp_path))
    incomplete = reference_json(plan("count_files", path="."))
    chat = Mock(side_effect=lambda messages, **kwargs: incomplete
                if "仅根据原始需求及用户回答独立提取条件" in messages[0]["content"]
                else json.dumps(candidates(prepared)))
    monkeypatch.setattr("core.engine.chat", chat)
    compare = Mock(wraps=same_query_plan)
    monkeypatch.setattr("core.bash_queries.same_query_plan", compare)
    engine = Engine(ssh_hosts=[])

    result = engine.generate_bash_commands(user_input, str(tmp_path), value, prepared, [])

    assert result == candidates(prepared)
    review_commands(prepared, result)
    assert chat.call_count == 2  # 一次独立参考，一次候选生成。
    reference = compare.call_args.args[1]
    assert reference.steps[0].operation == "count_files"
    assert reference.steps[0].parameters == {"path": ".", "recursive": recursive}
    assert engine.command_attempts[0]["raw_output"] == incomplete
    assert engine.command_attempts[0]["review"] == "accepted"
    assert [r["phase"] for r in engine.command_attempts] == ["independent_requirements", "candidate_commands"]


@pytest.mark.parametrize("parameters", [
    {},
    {"recursive": False},
    {"path": ".", "recursive": "false"},
    {"path": ".", "unknown": True},
    {"path": ".."},
])
def test_count_reference_invalid_parameters_remain_rejected(
        monkeypatch, tmp_path, no_reference_execution, parameters):
    value = plan("count_files", path=".", recursive=False)
    raw = reference_json(plan("count_files", **parameters))
    chat = Mock(return_value=raw)
    monkeypatch.setattr("core.engine.chat", chat)
    engine = Engine(ssh_hosts=[])
    with pytest.raises(ValueError):
        engine.generate_bash_commands("统计当前目录中的文件数量", str(tmp_path), value,
                                      prepare_queries(value, str(tmp_path)), [])
    assert chat.call_count <= 2
    assert all(r["phase"] == "independent_requirements" for r in engine.command_attempts)


@pytest.mark.parametrize("user_input,recursive", [
    ("统计当前目录中的文件数量", True),
    ("统计当前目录及子目录中的文件数量", False),
])
def test_count_reference_explicit_scope_conflict_is_rejected_even_when_primary_agrees(
        monkeypatch, tmp_path, no_reference_execution, user_input, recursive):
    value = plan("count_files", path=".", recursive=recursive)
    chat = Mock(return_value=reference_json(value))
    monkeypatch.setattr("core.engine.chat", chat)
    engine = Engine(ssh_hosts=[])
    with pytest.raises(ValueError):
        engine.generate_bash_commands(user_input, str(tmp_path), value, prepare_queries(value, str(tmp_path)), [])
    assert chat.call_count <= 2
    assert all(r["phase"] == "independent_requirements" for r in engine.command_attempts)


@pytest.mark.parametrize("user_input,answers", [
    ("统计文件数量", []),
    ("统计当前目录中的文件数量，不含子目录但包括子目录里的日志", []),
    ("统计当前目录中的文件数量", ["用户回答：还要统计 backup"]),
])
def test_count_reference_ambiguous_request_does_not_default(
        monkeypatch, tmp_path, no_reference_execution, user_input, answers):
    value = plan("count_files", path=".", recursive=False)
    chat = Mock(return_value=reference_json(plan("count_files", path=".")))
    monkeypatch.setattr("core.engine.chat", chat)
    engine = Engine(ssh_hosts=[])
    with pytest.raises(ValueError):
        engine.generate_bash_commands(user_input, str(tmp_path), value, prepare_queries(value, str(tmp_path)), answers)
    assert chat.call_count == 2
    assert all(r["phase"] == "independent_requirements" for r in engine.command_attempts)


def test_count_reference_default_preserves_strict_comparison(monkeypatch, tmp_path, no_reference_execution):
    value = plan("count_files", path=".", recursive=True)
    chat = Mock(return_value=reference_json(plan("count_files", path=".")))
    monkeypatch.setattr("core.engine.chat", chat)
    engine = Engine(ssh_hosts=[])
    with pytest.raises(ValueError, match="独立需求契约不一致"):
        engine.generate_bash_commands("统计当前目录中的文件数量", str(tmp_path), value,
                                      prepare_queries(value, str(tmp_path)), [])
    assert chat.call_count == 1
    assert engine.command_attempts[0]["review"] == "rejected"


@pytest.mark.parametrize("user_input,recursive,raw,count", [
    ("统计当前目录中的文件数量", False, "./app.log\0", 1),
    ("统计当前目录及子目录中的文件数量", True, "./app.log\0./nested/child.log\0", 2),
])
def test_count_missing_reference_recursive_completes_cli_with_independent_verification(
        monkeypatch, tmp_path, no_reference_execution, capsys, user_input, recursive, raw, count):
    (tmp_path / "app.log").write_text("fixture\n", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested/child.log").write_text("fixture\n", encoding="utf-8")
    value = plan("count_files", path=".", recursive=recursive)
    prepared = prepare_queries(value, str(tmp_path))
    monkeypatch.setattr("core.intent.chat", Mock(return_value=json.dumps({"status": "ready", "operations": ["count_files"]})))
    chat = Mock(side_effect=[reference_json(value), reference_json(plan("count_files", path=".")),
                             json.dumps(candidates(prepared))])
    monkeypatch.setattr("core.engine.chat", chat)
    engine, history, executor = Engine(ssh_hosts=[]), History(), Mock()
    executor.execute_query.return_value = (ExecutionResult(0, raw, "", 0), ["/usr/bin/find", ".", "-print0"])

    status = execute_request(engine, executor, history, user_input, str(tmp_path), "confirm", input_fn=lambda _: "yes")

    assert status == "verified"
    outcome = history.last_record["verification"][0]
    assert outcome["count"] == count and outcome["task_completed"]
    assert outcome["stdout"] == raw
    assert f"文件数量：{count}" in capsys.readouterr().out
    assert chat.call_count == 3  # 主计划、独立参考和候选各一次。
    assert engine.command_attempts[0]["review"] == "accepted"
    executor.execute_query.assert_called_once()


def test_invalid_count_reference_never_reaches_cli_confirmation_or_execution(
        monkeypatch, tmp_path, no_reference_execution):
    value = plan("count_files", path=".", recursive=False)
    monkeypatch.setattr("core.intent.chat", Mock(return_value=json.dumps({"status": "ready", "operations": ["count_files"]})))
    chat = Mock(side_effect=[reference_json(value), reference_json(plan("count_files", path=".", recursive="false"))])
    monkeypatch.setattr("core.engine.chat", chat)
    engine, history, executor = Engine(ssh_hosts=[]), History(), Mock()
    status = execute_request(engine, executor, history, "统计当前目录中的文件数量", str(tmp_path), "confirm",
                             input_fn=lambda _: pytest.fail("非法参考不得要求执行确认"))
    assert status == "blocked" and not history.last_record["executed"]
    assert chat.call_count == 2
    assert all(r["phase"] == "independent_requirements" for r in engine.command_attempts)
    executor.execute_query.assert_not_called()
    executor.execute.assert_not_called()


def test_find_output_without_final_nul_remains_unverified(tmp_path, no_reference_execution):
    (tmp_path / "app.log").write_text("fixture\n", encoding="utf-8")
    prepared = prepare_queries(plan("find_files", path=".", recursive=True, pattern="*.log"), str(tmp_path))
    review_commands(prepared, candidates(prepared))
    executor = Mock()
    executor.execute_query.return_value = (ExecutionResult(0, "./app.log", "", 0), ["/usr/bin/find", ".", "-print0"])
    outcome = execute_query(prepared, prepared["actions"][0], executor)
    assert outcome["status"] == "unverified" and not outcome["task_completed"]
    assert outcome["stdout"] == "./app.log"


@pytest.mark.parametrize("path", [".", "backup"])
def test_reference_missing_hidden_uses_default_without_model_correction(monkeypatch, tmp_path, path):
    (tmp_path / "backup").mkdir()
    value = plan("list_directory", path=path, include_hidden=False)
    prepared = prepare_queries(value, str(tmp_path))
    incomplete = reference_json(plan("list_directory", path=path))
    replies = iter([incomplete, json.dumps(candidates(prepared))])
    calls = []
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: calls.append(args) or next(replies))
    engine = Engine(ssh_hosts=[])
    result = engine.generate_bash_commands("浏览目录", str(tmp_path), value, prepared, [])
    assert result == candidates(prepared)
    review_commands(prepared, result)
    assert len(calls) == 2
    assert len(engine.command_attempts) == 2
    assert engine.command_attempts[0]["raw_output"] == incomplete
    assert prepared["actions"][0]["parameters"] == {"path": path, "include_hidden": False}


@pytest.mark.parametrize("user_input,answers", [
    ("显示隐藏文件", []),
    ("浏览目录", ["用户回答：包括隐藏文件"]),
    ("list hidden files", []),
    ("列出点号开头的文件", []),
])
def test_hidden_request_missing_reference_flag_never_defaults_false(monkeypatch, tmp_path, user_input, answers):
    value = plan("list_directory", path=".", include_hidden=False)
    incomplete = reference_json(plan("list_directory", path="."))
    calls = []
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: calls.append(args) or incomplete)
    engine = Engine(ssh_hosts=[])
    with pytest.raises(ValueError, match="独立参考生成失败"):
        engine.generate_bash_commands(user_input, str(tmp_path), value, prepare_queries(value, str(tmp_path)), answers)
    assert len(calls) == 2
    assert all(record["phase"] == "independent_requirements" for record in engine.command_attempts)


def test_hidden_primary_and_nonhidden_reference_remain_inconsistent(monkeypatch, tmp_path):
    value = plan("list_directory", path=".", include_hidden=True)
    calls = []
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs:
                        calls.append(args) or reference_json(plan("list_directory", path=".", include_hidden=False)))
    engine = Engine(ssh_hosts=[])
    with pytest.raises(ValueError, match="独立需求"):
        engine.generate_bash_commands("包括隐藏项", str(tmp_path), value, prepare_queries(value, str(tmp_path)), [])
    assert len(calls) == 1
    assert engine.command_attempts[0]["review"] == "rejected"


@pytest.mark.parametrize("incomplete,value", [
    (plan("list_directory", include_hidden=False), plan("list_directory", path=".", include_hidden=False)),
    (plan("list_directory"), plan("list_directory", path=".", include_hidden=False)),
    (plan("find_files", path="."), plan("find_files", path=".", recursive=False)),
    (plan("read_text", path="input", end="head"), plan("read_text", path="input", end="head", lines=2)),
])
def test_reference_other_missing_parameters_are_not_defaulted(monkeypatch, tmp_path, incomplete, value):
    (tmp_path / "input").write_text("one\ntwo\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: calls.append(args) or reference_json(incomplete))
    engine = Engine(ssh_hosts=[])
    with pytest.raises(ValueError, match="独立参考生成失败"):
        engine.generate_bash_commands("浏览目录", str(tmp_path), value, prepare_queries(value, str(tmp_path)), [])
    assert len(calls) == 2
    assert all(record["phase"] == "independent_requirements" for record in engine.command_attempts)


@pytest.mark.parametrize("reference", [
    plan("list_directory", path="backup"),
    plan("list_directory", path=".."),
    plan("list_directory", path=".", pattern="*.txt"),
    plan("list_directory", path=".", include_hidden="false"),
    plan("find_files", path=".", recursive=False),
])
def test_reference_default_keeps_path_operation_and_filter_checks(monkeypatch, tmp_path, reference):
    (tmp_path / "backup").mkdir()
    value = plan("list_directory", path=".", include_hidden=False)
    calls = []
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: calls.append(args) or reference_json(reference))
    engine = Engine(ssh_hosts=[])
    with pytest.raises(ValueError):
        engine.generate_bash_commands("浏览目录", str(tmp_path), value, prepare_queries(value, str(tmp_path)), [])
    assert len(calls) == 1
    assert all(record["phase"] == "independent_requirements" for record in engine.command_attempts)



def test_reference_clarification_is_reviewed_once_without_reading_primary(monkeypatch, tmp_path):
    (tmp_path / "--danger.txt").write_text("fixture", encoding="utf-8")
    value = plan("find_files", path=".", recursive=False, pattern="--danger.txt")
    prepared = prepare_queries(value, str(tmp_path))
    question = json.dumps({"status": "need_clarification",
                           "clarification": "是否需要查找当前目录及其子目录中的 --danger.txt 文件？"})
    replies = iter([question, reference_json(value), json.dumps(candidates(prepared))])
    calls = []
    def reply(messages, **kwargs):
        calls.append([dict(message) for message in messages])
        return next(replies)
    monkeypatch.setattr("core.engine.chat", reply)
    engine = Engine(ssh_hosts=[])
    engine.request_intent = {"operations": ["SENTINEL_OTHER_PLAN"]}
    result = engine.generate_bash_commands("查找当前目录中名为 --danger.txt 的普通文件",
                                           str(tmp_path), value, prepared, [])
    review_commands(prepared, result)
    assert len(calls) == 3
    assert len(engine.command_attempts) == 3
    assert engine.command_attempts[0]["raw_output"] == question
    assert "澄清" in engine.command_attempts[0]["review"]
    assert engine.command_attempts[1]["review"] == "accepted"
    original = {"cwd": str(tmp_path), "request": "查找当前目录中名为 --danger.txt 的普通文件", "answers": []}
    review = calls[1][-1]["content"]
    assert review.endswith(json.dumps(original, ensure_ascii=False))
    assert "recursive=false" in review and "关键条件" in review
    assert "SENTINEL_OTHER_PLAN" not in review and '"plan"' not in review and '"commands"' not in review
    assert "mandatory_prefix" in calls[2][0]["content"] and "逐项复制" in calls[2][0]["content"]
    argv = result["commands"][0]["argv"]
    assert argv[argv.index("-name") + 1] == "--danger.txt"
    assert prepared["actions"][0]["files"] == ["--danger.txt"]


def test_repeated_reference_clarification_stops_with_specific_reason(monkeypatch, tmp_path):
    value = plan("find_files", path=".", recursive=False, pattern="--danger.txt")
    question = json.dumps({"status": "need_clarification", "clarification": "是否包含子目录？"})
    calls = []
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: calls.append(args) or question)
    engine = Engine(ssh_hosts=[])
    with pytest.raises(ValueError, match="独立参考仍要求澄清：是否包含子目录"):
        engine.generate_bash_commands("查找当前目录中名为 --danger.txt 的普通文件", str(tmp_path),
                                      value, prepare_queries(value, str(tmp_path)), [])
    assert len(calls) == 2
    assert all(record["phase"] == "independent_requirements" for record in engine.command_attempts)


@pytest.mark.parametrize("reference", [
    plan("find_files", path=".", recursive=True, pattern="--danger.txt"),
    plan("find_files", path="backup", recursive=False, pattern="--danger.txt"),
    plan("find_files", path="..", recursive=False, pattern="--danger.txt"),
    plan("find_files", path=".", recursive=False, pattern="*.txt"),
    plan("find_files", path=".", recursive=False),
    plan("count_files", path=".", recursive=False, pattern="--danger.txt"),
])
def test_reference_clarification_review_cannot_change_query_conditions(monkeypatch, tmp_path, reference):
    (tmp_path / "backup").mkdir()
    value = plan("find_files", path=".", recursive=False, pattern="--danger.txt")
    question = json.dumps({"status": "need_clarification", "clarification": "是否包含子目录？"})
    replies = iter([question, reference_json(reference)])
    calls = []
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: calls.append(args) or next(replies))
    engine = Engine(ssh_hosts=[])
    with pytest.raises(ValueError):
        engine.generate_bash_commands("查找当前目录中名为 --danger.txt 的普通文件", str(tmp_path),
                                      value, prepare_queries(value, str(tmp_path)), [])
    assert len(calls) == 2
    assert all(record["phase"] == "independent_requirements" for record in engine.command_attempts)


def test_genuine_reference_question_does_not_default_missing_line_count(monkeypatch, tmp_path):
    (tmp_path / "input").write_text("one\ntwo\n", encoding="utf-8")
    value = plan("read_text", path="input", end="head", lines=2)
    question = json.dumps({"status": "need_clarification", "clarification": "需要查看前几行？"})
    calls = []
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: calls.append(args) or question)
    with pytest.raises(ValueError, match="独立参考仍要求澄清：需要查看前几行"):
        Engine(ssh_hosts=[]).generate_bash_commands("查看 input 的开头", str(tmp_path), value,
                                                  prepare_queries(value, str(tmp_path)), [])
    assert len(calls) == 2


def test_reference_review_and_format_correction_share_one_retry_budget(monkeypatch, tmp_path):
    value = plan("find_files", path=".", recursive=False, pattern="--danger.txt")
    question = json.dumps({"status": "need_clarification", "clarification": "是否包含子目录？"})
    replies = iter([question, reference_json(plan("find_files", path="."))])
    calls = []
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: calls.append(args) or next(replies))
    engine = Engine(ssh_hosts=[])
    with pytest.raises(ValueError, match="独立参考生成失败"):
        engine.generate_bash_commands("查找当前目录中名为 --danger.txt 的普通文件", str(tmp_path),
                                      value, prepare_queries(value, str(tmp_path)), [])
    assert len(calls) == 2
    assert all(record["phase"] == "independent_requirements" for record in engine.command_attempts)



def test_reference_refusal_preserves_reason_and_never_generates_candidate(monkeypatch, tmp_path):
    value = plan("find_files", path=".", recursive=False, pattern="--danger.txt")
    raw = json.dumps({"status": "unsupported", "reason": "文件名以 -- 开头，不符合文件名规范"})
    calls = []
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: calls.append(args) or raw)
    engine = Engine(ssh_hosts=[])
    with pytest.raises(ValueError, match="独立参考拒绝生成：文件名以 -- 开头"):
        engine.generate_bash_commands("查找当前目录中名为 --danger.txt 的普通文件", str(tmp_path),
                                      value, prepare_queries(value, str(tmp_path)), [])
    assert len(calls) == 1
    assert len(engine.command_attempts) == 1
    assert engine.command_attempts[0]["raw_output"] == raw
    assert "拒绝" in engine.command_attempts[0]["review"]



@pytest.mark.parametrize("omission", ["first_sensitive", "all_sensitive", "audit"])
def test_find_candidate_missing_mandatory_exclusions_never_executes(tmp_path, omission):
    value = plan("find_files", path=".", recursive=False, pattern="--danger.txt")
    engine, executor, history = engine_for(value), Mock(), History()
    history.path = tmp_path / "history.jsonl"
    def reply(request, cwd, value, prepared, answers):
        payload = candidates(prepared)
        argv = list(payload["commands"][0]["argv"])
        if omission == "first_sensitive":
            index = argv.index("-ipath") - 1
            del argv[index:index + 3]
        elif omission == "all_sensitive":
            while "-ipath" in argv:
                index = argv.index("-ipath") - 1
                del argv[index:index + 3]
        else:
            index = argv.index("-path") - 1
            del argv[index:index + 3]
        payload["commands"][0]["argv"] = argv
        return payload
    engine.generate_bash_commands.side_effect = reply
    status = execute_request(engine, executor, history, "查找当前目录中名为 --danger.txt 的普通文件",
                             str(tmp_path), "confirm", input_fn=lambda _: pytest.fail("安全排除缺失时不能进入确认"))
    assert status == "blocked"
    assert not history.records[-1]["executed"]
    executor.execute.assert_not_called()
    executor.execute_query.assert_not_called()
