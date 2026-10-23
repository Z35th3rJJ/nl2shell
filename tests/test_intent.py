import json

import pytest

from core.engine import Engine
from core.intent import analyze_request


@pytest.mark.parametrize("user_input", [
    "把 note.txt 丢到回收区", "把 note.txt 放进回收区", "把 note.txt 扔进回收站",
    "把 note.txt 移到垃圾箱", "把 note.txt 删掉但保留恢复能力",
])
def test_independent_intent_blocks_query_only_plan(monkeypatch, tmp_path, user_input):
    seen = []
    def intent_reply(messages, backend=None):
        seen.extend(messages)
        return json.dumps({"status": "ready", "operations": ["trash"]})
    monkeypatch.setattr("core.intent.chat", intent_reply)
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: json.dumps({"status": "ready", "steps": [
        {"operation": "find_files", "parameters": {"path": ".", "recursive": False}}]}))
    engine = Engine(ssh_hosts=[])
    with pytest.raises(ValueError, match="trash"):
        engine.generate_task_plan(user_input, str(tmp_path))
    assert engine.request_intent["operations"] == ["trash"]
    assert len(engine.plan_attempts) == 2
    assert user_input in seen[1]["content"]
    assert '"steps"' not in seen[1]["content"]


@pytest.mark.parametrize("payload", [
    {"status": "ready", "operations": []}, {"status": "ready", "operations": ["run_shell"]},
    {"status": "ready", "operations": ["trash", "trash"]},
])
def test_invalid_intent_cannot_reach_ready(monkeypatch, payload):
    monkeypatch.setattr("core.intent.chat", lambda *args, **kwargs: json.dumps(payload))
    attempts = []
    with pytest.raises(ValueError, match="结构化意图"):
        analyze_request("处理文件", [], "local", attempts)
    assert len(attempts) == 2


def test_uncertain_intent_asks_before_planning(monkeypatch, tmp_path):
    monkeypatch.setattr("core.intent.chat", lambda *args, **kwargs: json.dumps({
        "status": "need_clarification", "clarification": "您想对文件做什么？"}))
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: pytest.fail("意图不明确时不应生成执行计划"))
    assert Engine(ssh_hosts=[]).generate_task_plan("处理一下", str(tmp_path)).clarification


def test_plan_cannot_add_unrequested_write(monkeypatch, tmp_path):
    monkeypatch.setattr("core.intent.chat", lambda *args, **kwargs: json.dumps({"status": "ready", "operations": ["find_files"]}))
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: json.dumps({"status": "ready", "steps": [
        {"operation": "find_files", "parameters": {"path": ".", "recursive": False}},
        {"operation": "trash", "parameters": {"sources": ["note.txt"]}}]}))
    with pytest.raises(ValueError, match="未要求的写操作"):
        Engine(ssh_hosts=[]).generate_task_plan("只看文件，不要删除", str(tmp_path))


def test_unsupported_intent_is_reviewed_without_reading_plan(monkeypatch):
    replies = iter([json.dumps({"status": "unsupported", "reason_code": "permanent_delete"}),
                    json.dumps({"status": "ready", "operations": ["trash"]})])
    monkeypatch.setattr("core.intent.chat", lambda *args, **kwargs: next(replies))
    attempts = []
    assert analyze_request("放进回收站，允许恢复", [], "local", attempts)["operations"] == ["trash"]
    assert attempts[0]["review_requested"] and len(attempts) == 2


def test_named_copy_does_not_move_or_search(monkeypatch, tmp_path):
    from core.operations import prepare_plan, execute_action
    (tmp_path / "note.txt").write_bytes(b"original")
    intent = {"status": "ready", "operations": ["find_files", "copy_files"],
              "named_sources": ["note.txt"], "selection_required": False}
    monkeypatch.setattr("core.intent.chat", lambda *args, **kwargs: json.dumps(intent))
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: json.dumps({"status": "ready", "steps": [
        {"operation": "find_files", "parameters": {"path": ".", "recursive": False, "pattern": "note.txt"}},
        {"operation": "copy_files", "parameters": {"source_step": 1, "destination": "backup"}}]}))
    engine = Engine(ssh_hosts=[])
    plan = engine.generate_task_plan("不要移动 note.txt，只复制到 backup", str(tmp_path))
    assert engine.request_intent["operations"] == ["copy_files"]
    assert [step.operation for step in plan.steps] == ["copy_files"]
    assert plan.steps[0].parameters["sources"] == ["note.txt"]
    prepared = prepare_plan(plan, str(tmp_path))
    assert execute_action(prepared, prepared["actions"][0])["status"] == "verified"
    assert (tmp_path / "note.txt").read_bytes() == (tmp_path / "backup/note.txt").read_bytes() == b"original"


def test_permanent_delete_uses_correct_reason_category(monkeypatch, tmp_path):
    monkeypatch.setattr("core.intent.chat", lambda *args, **kwargs: json.dumps({
        "status": "unsupported", "reason_code": "permanent_delete"}))
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: pytest.fail("拒绝后不生成计划"))
    plan = Engine(ssh_hosts=[]).generate_task_plan("永久删除 note.txt", str(tmp_path))
    assert plan.refused and "永久删除" in plan.reason and "脚本" not in plan.reason


def test_no_explicit_action_cannot_be_guessed_as_find(monkeypatch, tmp_path):
    monkeypatch.setattr("core.intent.chat", lambda *args, **kwargs: json.dumps({
        "status": "ready", "operations": ["find_files"], "action_explicit": False}))
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: pytest.fail("动作不明确时不生成计划"))
    plan = Engine(ssh_hosts=[]).generate_task_plan("帮我处理一下 note.txt", str(tmp_path))
    assert plan.status == "need_clarification" and not plan.steps


def test_named_source_must_come_from_user(monkeypatch):
    monkeypatch.setattr("core.intent.chat", lambda *args, **kwargs: json.dumps({
        "status": "ready", "operations": ["copy_files"], "named_sources": ["invented.txt"], "selection_required": False}))
    with pytest.raises(ValueError, match="结构化意图"):
        analyze_request("复制 note.txt 到 backup", [], "local", [])


def test_review_question_does_not_replace_permanent_delete_reason(monkeypatch, tmp_path):
    replies = iter([json.dumps({"status": "unsupported", "reason_code": "permanent_delete"}),
                    json.dumps({"status": "need_clarification", "clarification": "您想执行什么动作？"})])
    monkeypatch.setattr("core.intent.chat", lambda *args, **kwargs: next(replies))
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: pytest.fail("拒绝后不生成计划"))
    engine = Engine(ssh_hosts=[])
    plan = engine.generate_task_plan("永久删除 note.txt", str(tmp_path))
    assert plan.refused and "永久删除" in plan.reason
    assert engine.request_intent["reason_code"] == "permanent_delete"


@pytest.mark.parametrize("user_input,operations,answers", [
    ("把当前目录过去两周修改的文件备份到 archive", ["find_files", "copy_files"], []),
    ("只列出当前目录文件，不要删除或移动", ["find_files"], []),
    ("找出 . 当前这一层的 txt 文件", ["find_files"], ["仅当前层"]),
])
def test_intent_review_repeats_original_request(monkeypatch, user_input, operations, answers):
    original = json.dumps({"request": user_input, "answers": answers}, ensure_ascii=False)
    seen = []
    def reply(messages, backend=None):
        if seen:
            # Keep the actual request last so review examples cannot become a new task.
            assert messages[-1]["content"].endswith(original)
        seen.append(messages[-1]["content"])
        return json.dumps({"status": "ready", "operations": operations})
    monkeypatch.setattr("core.intent.chat", reply)
    assert analyze_request(user_input, answers, "local", [])["operations"] == operations
    assert len(seen) == 2


@pytest.mark.parametrize("user_input", ["只列出 backup 内的文件夹", "找出名称含 report 的文件和文件夹"])
def test_directory_selection_refusal_explains_object_limit(monkeypatch, tmp_path, user_input):
    monkeypatch.setattr("core.intent.chat", lambda *args, **kwargs: json.dumps({
        "status": "unsupported", "reason_code": "directory_selection"}))
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: pytest.fail("不支持对象时不得生成计划"))
    value = Engine(ssh_hosts=[]).generate_task_plan(user_input, str(tmp_path))
    assert value.status == "unsupported" and not value.steps
    assert "文件夹" in value.reason and "普通文件筛选" in value.reason


@pytest.fixture
def no_filename_query_execution(monkeypatch):
    from unittest.mock import Mock

    launch = Mock(side_effect=AssertionError("意图和计划校验不能启动候选命令"))
    monkeypatch.setattr("subprocess.Popen", launch)
    yield launch
    launch.assert_not_called()


@pytest.mark.parametrize("user_input", [
    "递归查找子目录中名为 *.log 的文件",
    "递归查找当前目录及子目录中名为 *.log 的普通文件",
])
def test_recursive_filename_query_recovers_directory_selection_mistake(monkeypatch, tmp_path, no_filename_query_execution, user_input):
    from unittest.mock import Mock

    raw = json.dumps({"status": "unsupported", "reason_code": "directory_selection"})
    intent_model = Mock(return_value=raw)
    parameters = {"path": ".", "recursive": True, "pattern": "*.log"}
    planner = Mock(return_value=json.dumps({"status": "ready", "steps": [
        {"operation": "find_files", "parameters": parameters}]}))
    monkeypatch.setattr("core.intent.chat", intent_model)
    monkeypatch.setattr("core.engine.chat", planner)
    engine = Engine(ssh_hosts=[])

    value = engine.generate_task_plan(user_input, str(tmp_path))

    assert value.status == "ready" and not value.refused and not value.clarification
    assert len(value.steps) == 1 and value.steps[0].operation == "find_files"
    assert value.steps[0].parameters == parameters
    assert engine.request_intent == {"status": "ready", "operations": ["find_files"]}
    assert engine.intent_attempts[0]["raw_output"] == raw
    assert "普通文件" in engine.intent_attempts[0]["resolution"]
    intent_model.assert_called_once()
    planner.assert_called_once()
    assert len(engine.plan_attempts) == 1 and not engine.command_attempts


def test_explicit_directory_listing_recovers_directory_selection_mistake(
        monkeypatch, tmp_path, no_filename_query_execution):
    from unittest.mock import Mock

    user_input = "列出 organize 目录的内容"
    raw = json.dumps({"status": "unsupported", "reason_code": "directory_selection"})
    intent_model = Mock(return_value=raw)
    planner = Mock(return_value=json.dumps({"status": "need_clarification", "clarification": "隐藏文件是否包含？"}))
    monkeypatch.setattr("core.intent.chat", intent_model)
    monkeypatch.setattr("core.engine.chat", planner)
    engine = Engine(ssh_hosts=[])

    value = engine.generate_task_plan(user_input, str(tmp_path))

    assert value.status == "ready" and not value.clarification and not value.refused
    assert value.steps[0].parameters == {"path": "organize", "include_hidden": False}
    assert engine.request_intent == {"status": "ready", "operations": ["list_directory"]}
    assert engine.intent_attempts[0]["raw_output"] == raw
    assert "list_directory" in engine.intent_attempts[0]["resolution"]
    intent_model.assert_called_once()
    planner.assert_called_once()
    assert user_input in planner.call_args.args[0][1]["content"]
    assert len(value.steps) == 1 and not engine.command_attempts


@pytest.mark.parametrize("user_input,answers,code", [
    ("查找 organize 目录中的文件夹", [], "directory_selection"),
    ("列出 organize 目录的内容，只看文件夹", [], "directory_selection"),
    ("列出 organize 目录的内容，筛选名称为 app 的文件和文件夹", [], "directory_selection"),
    ("列出 organize 目录的内容", ["用户回答：只列出文件夹"], "directory_selection"),
    ("列出 organize 目录的内容", [], "network"),
])
def test_explicit_directory_listing_recovery_keeps_real_rejections(
        monkeypatch, tmp_path, no_filename_query_execution, user_input, answers, code):
    from unittest.mock import Mock
    from core.intent import REJECTION_REASONS

    intent_model = Mock(return_value=json.dumps({"status": "unsupported", "reason_code": code}))
    planner = Mock(side_effect=AssertionError("真实限制不得进入计划生成"))
    monkeypatch.setattr("core.intent.chat", intent_model)
    monkeypatch.setattr("core.engine.chat", planner)
    engine = Engine(ssh_hosts=[])
    value = engine.generate_task_plan(user_input, str(tmp_path), clarifications=answers)
    assert value.refused and value.reason == REJECTION_REASONS[code]
    assert not value.steps and not engine.command_attempts
    planner.assert_not_called()


@pytest.mark.parametrize("user_input,answers,code", [
    ("递归查找子目录中名为 *.log 的文件夹", [], "directory_selection"),
    ("递归查找当前目录及子目录中名为 *.log 的文件和文件夹", [], "directory_selection"),
    ("递归查找子目录中名为 *.log 的文件，并复制到 backup", [], "directory_selection"),
    ("递归查找子目录中名为 *.log 的文件，但只要文件夹", [], "directory_selection"),
    ("递归查找子目录中名为 *.log 的文件", ["用户回答：还要包含文件夹"], "directory_selection"),
    ("递归查找子目录中名为 *.log 的文件", [], "network"),
    ("递归查找子目录中名为 *.log 的文件", [], "permissions"),
])
def test_recursive_filename_recovery_keeps_other_rejections(monkeypatch, tmp_path, no_filename_query_execution, user_input, answers, code):
    from unittest.mock import Mock
    from core.intent import REJECTION_REASONS

    intent_model = Mock(return_value=json.dumps({"status": "unsupported", "reason_code": code}))
    planner = Mock(side_effect=AssertionError("真实限制不得进入计划生成"))
    monkeypatch.setattr("core.intent.chat", intent_model)
    monkeypatch.setattr("core.engine.chat", planner)
    engine = Engine(ssh_hosts=[])

    value = engine.generate_task_plan(user_input, str(tmp_path), clarifications=answers)

    assert value.status == "unsupported" and value.refused and not value.steps
    assert value.reason == REJECTION_REASONS[code]
    assert engine.request_intent["reason_code"] == code
    assert not engine.plan_attempts and not engine.command_attempts
    assert intent_model.call_count == 2
    planner.assert_not_called()


@pytest.mark.parametrize("parameters", [
    {"path": "../outside", "recursive": True, "pattern": "*.log"},
    {"path": ".", "recursive": True, "pattern": "*.log", "type": "directory"},
    {"path": ".", "pattern": "*.log"},
])
def test_recursive_filename_recovery_preserves_plan_validation(monkeypatch, tmp_path, no_filename_query_execution, parameters):
    from unittest.mock import Mock

    monkeypatch.setattr("core.intent.chat", Mock(return_value=json.dumps({
        "status": "unsupported", "reason_code": "directory_selection"})))
    planner = Mock(return_value=json.dumps({"status": "ready", "steps": [
        {"operation": "find_files", "parameters": parameters}]}))
    monkeypatch.setattr("core.engine.chat", planner)
    engine = Engine(ssh_hosts=[])

    with pytest.raises(ValueError):
        engine.generate_task_plan("递归查找子目录中名为 *.log 的文件", str(tmp_path))

    assert engine.request_intent["operations"] == ["find_files"]
    assert planner.call_count == 2 and not engine.command_attempts


@pytest.mark.parametrize("extra", [{"include_hidden": True}, {"path": "."},
                                  {"recursive": False}, {"pattern": "*.txt"}])
def test_intent_extra_parameter_gets_specific_error_and_one_retry(monkeypatch, extra):
    valid = {"status": "ready", "operations": ["list_directory"]}
    replies = iter([json.dumps({**valid, **extra}), json.dumps(valid)])
    seen = []
    def reply(messages, backend=None):
        seen.append([dict(message) for message in messages])
        return next(replies)
    monkeypatch.setattr("core.intent.chat", reply)
    attempts = []
    assert analyze_request("列出当前目录中的所有文件，包括隐藏文件", [], "local", attempts) == valid
    assert len(attempts) == 2
    error = attempts[0]["validation_errors"][0]
    assert next(iter(extra)) in error and "字段" in error
    assert "必需操作必须" not in error
    assert "不输出操作参数" in seen[1][-1]["content"]
    assert "只输出操作类型" in seen[0][0]["content"]
    assert attempts[1]["validation_errors"] == []


def test_repeated_extra_intent_field_stops_before_planning(monkeypatch, tmp_path):
    payload = {"status": "ready", "operations": ["list_directory"], "include_hidden": True}
    monkeypatch.setattr("core.intent.chat", lambda *args, **kwargs: json.dumps(payload))
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: pytest.fail("非法意图不得进入计划阶段"))
    engine = Engine(ssh_hosts=[])
    with pytest.raises(ValueError, match="结构化意图"):
        engine.generate_task_plan("列出当前目录中的所有文件，包括隐藏文件", str(tmp_path))
    assert len(engine.intent_attempts) == 2
    assert all("include_hidden" in record["validation_errors"][0] for record in engine.intent_attempts)


def test_recovered_intent_keeps_hidden_parameter_in_plan_and_preview(monkeypatch, tmp_path):
    from unittest.mock import Mock
    from cli import execute_request
    from core.settings import PREVIEW
    from tests.test_cli_flow import History
    valid = {"status": "ready", "operations": ["list_directory"]}
    intents = iter([json.dumps({**valid, "include_hidden": True}), json.dumps(valid)])
    monkeypatch.setattr("core.intent.chat", lambda *args, **kwargs: next(intents))
    listing = {"status": "ready", "steps": [{"operation": "list_directory",
               "parameters": {"path": ".", "include_hidden": True}}]}
    argv = ["ls", "-1", "--color=never", "--quoting-style=literal", "-A", "--", "."]
    replies = iter([json.dumps(listing), json.dumps(listing), json.dumps({"commands": [
        {"step": 1, "argv": argv, "explanation": "列出目录内容，包括隐藏文件"}]})])
    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: next(replies))
    (tmp_path/"note.txt").write_text("visible", encoding="utf-8")
    (tmp_path/".hidden").write_text("hidden", encoding="utf-8")
    engine, executor, history = Engine(ssh_hosts=[]), Mock(), History()
    status = execute_request(engine, executor, history, "列出当前目录中的所有文件，包括隐藏文件",
                             str(tmp_path), PREVIEW, input_fn=lambda _: pytest.fail("预览不得请求执行确认"))
    assert status == "preview"
    assert engine.request_intent == valid
    record = history.records[-1]
    assert record["plan"]["steps"][0]["parameters"] == {"path": ".", "include_hidden": True}
    assert record["preview"][0]["candidate_argv"] == argv
    assert record["preview"][0]["review"] == "accepted"
    assert not record["executed"] and not record["verification"]
    executor.execute.assert_not_called()
    executor.execute_query.assert_not_called()
