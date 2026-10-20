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
