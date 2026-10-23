"""需求事实、模型建议和执行安全必须分别验证。"""
import json
from unittest.mock import Mock

import pytest

from core.bash_queries import prepare_queries
from core.engine import Engine
from core.task_plan import parse_operation_plan


def raw_plan(operation, **parameters):
    return json.dumps({"status": "ready", "steps": [{"operation": operation, "parameters": parameters}]})


@pytest.fixture(autouse=True)
def no_commands(monkeypatch):
    launch = Mock(side_effect=AssertionError("需求解析不得启动命令"))
    monkeypatch.setattr("subprocess.Popen", launch)
    yield
    launch.assert_not_called()


@pytest.mark.parametrize("user_input,operation,parameters", [
    ("列出 organize 目录的内容", "list_directory", {"path": "organize", "include_hidden": False}),
    ("列出 organize 目录的内容，包括隐藏文件", "list_directory", {"path": "organize", "include_hidden": True}),
    ("统计当前目录中的文件数量", "count_files", {"path": ".", "recursive": False}),
])
@pytest.mark.parametrize("stage", ["intent", "planning"])
def test_complete_requirements_cannot_become_model_questions(monkeypatch, tmp_path, user_input, operation, parameters, stage):
    question = json.dumps({"status": "need_clarification", "clarification": "您想执行什么操作，是否包含隐藏文件？"})
    intent = Mock(return_value=question if stage == "intent" else json.dumps({"status": "ready", "operations": [operation]}))
    planner = Mock(return_value=question if stage == "planning" else raw_plan(operation, **parameters))
    monkeypatch.setattr("core.intent.chat", intent)
    monkeypatch.setattr("core.engine.chat", planner)
    engine = Engine(ssh_hosts=[])
    result = engine.generate_task_plan(user_input, str(tmp_path))
    assert result.status == "ready" and not result.clarification
    assert len(result.steps) == 1 and result.steps[0].operation == operation
    assert result.steps[0].parameters == parameters
    assert planner.call_count == 1 and intent.call_count <= 2
    if stage == "intent":
        intent.assert_called_once()
        assert engine.intent_attempts[0]["resolution"]
    else:
        assert engine.plan_attempts[0]["recovery"]["source"] == "original_request"
    assert engine.command_attempts == []


@pytest.mark.parametrize("user_input,parameters", [
    ("列出 organize 目录的内容，包括隐藏文件", {"path": "organize", "include_hidden": False}),
    ("列出 organize 目录的内容", {"path": "organize", "include_hidden": True}),
    ("列出 organize 目录的内容", {"path": ".", "include_hidden": False}),
])
def test_primary_and_reference_cannot_jointly_drop_request_conditions(monkeypatch, tmp_path, user_input, parameters):
    (tmp_path / "organize").mkdir()
    bad = raw_plan("list_directory", **parameters)
    model = Mock(return_value=bad)
    monkeypatch.setattr("core.intent.chat", Mock(return_value=json.dumps({"status": "ready", "operations": ["list_directory"]})))
    monkeypatch.setattr("core.engine.chat", model)
    engine = Engine(ssh_hosts=[])
    with pytest.raises(ValueError, match="原始需求"):
        engine.generate_task_plan(user_input, str(tmp_path))
    assert model.call_count == 2 and engine.command_attempts == []
    # 直接进入独立参考接口也必须核对需求，不能只比较两个同样错误的模型计划。
    primary = parse_operation_plan(bad)
    model.reset_mock()
    with pytest.raises(ValueError, match="原始需求"):
        engine.generate_bash_commands(user_input, str(tmp_path), primary, prepare_queries(primary, str(tmp_path)), [])
    model.assert_called_once()
    assert all(record["phase"] == "independent_requirements" for record in engine.command_attempts)


@pytest.mark.parametrize("destination", ["backup", "合适的目录"])
def test_vague_copy_destination_remains_missing(monkeypatch, tmp_path, destination):
    monkeypatch.setattr("core.intent.chat", Mock(return_value=json.dumps({"status": "ready", "operations": ["copy_files"]})))
    model = Mock(return_value=raw_plan("copy_files", sources=["report.txt"], destination=destination))
    monkeypatch.setattr("core.engine.chat", model)
    result = Engine(ssh_hosts=[]).generate_task_plan("把 report.txt 复制到合适的目录", str(tmp_path))
    assert result.status == "need_clarification" and not result.steps
    assert "目录" in result.clarification
    model.assert_called_once()


def test_filesystem_failure_is_not_retried_as_model_error(monkeypatch, tmp_path):
    monkeypatch.setattr("core.intent.chat", Mock(return_value=json.dumps({"status": "ready", "operations": ["copy_files"]})))
    model = Mock(return_value=raw_plan("copy_files", sources=["report.txt"], destination="backup"))
    monkeypatch.setattr("core.engine.chat", model)
    monkeypatch.setattr("core.engine._normalize_paths", Mock(side_effect=PermissionError("fixture permission denied")))
    engine = Engine(ssh_hosts=[])
    with pytest.raises(PermissionError, match="fixture permission denied"):
        engine.generate_task_plan("把 report.txt 复制到 backup", str(tmp_path))
    model.assert_called_once()
    assert engine.plan_attempts[0]["environment_error"] == "fixture permission denied"
    assert not engine.command_attempts


def test_question_text_cannot_authorize_a_fabricated_source(monkeypatch):
    from core.intent import analyze_request

    model = Mock(return_value=json.dumps({"status": "ready", "operations": ["copy_files"],
        "named_sources": ["invented.txt"], "selection_required": False}))
    monkeypatch.setattr("core.intent.chat", model)
    attempts = []
    with pytest.raises(ValueError, match="结构化意图"):
        analyze_request("复制 report.txt", ["问题：是否复制 invented.txt？；用户回答：不，是 report.txt"], "local", attempts)
    assert model.call_count == 2
    assert "源文件必须逐字来自用户" in attempts[0]["validation_errors"][0]


@pytest.mark.parametrize("user_input,answers", [
    ("列出 organize 目录的内容，包括隐藏文件", []),
    ("列出 organize 目录的内容", ["问题：是否包含隐藏文件？；用户回答：是"]),
])
def test_missing_model_field_uses_confirmed_fact_in_both_plans(monkeypatch, tmp_path, user_input, answers):
    from core.bash_queries import review_commands

    (tmp_path / "organize").mkdir()
    raw = raw_plan("list_directory", path="organize")
    model = Mock(return_value=raw)
    monkeypatch.setattr("core.engine.chat", model)
    monkeypatch.setattr("core.intent.chat", Mock(return_value=json.dumps({"status": "ready", "operations": ["list_directory"]})))
    engine = Engine(ssh_hosts=[])
    primary = engine.generate_task_plan(user_input, str(tmp_path), answers)
    assert primary.steps[0].parameters == {"path": "organize", "include_hidden": True}
    prepared = prepare_queries(primary, str(tmp_path))
    commands = {"commands": [{"step": 1, "argv": prepared["actions"][0]["_required_argv"], "explanation": "保留已确认条件"}]}
    model.side_effect = [raw, json.dumps(commands)]
    review_commands(prepared, engine.generate_bash_commands(user_input, str(tmp_path), primary, prepared, answers))
    assert model.call_count == 3 and engine.command_attempts[0]["review"] == "accepted"


@pytest.mark.parametrize("suffix,hidden", [("", False), ("，包括隐藏文件", True), ("，不包含隐藏文件", False)])
def test_cli_preserves_request_facts_despite_questions_at_each_stage(monkeypatch, tmp_path, suffix, hidden):
    from cli import execute_request
    from core.history import HistoryStore
    from core.bash_queries import review_commands

    (tmp_path / "organize").mkdir()
    user_input = "列出 organize 目录的内容" + suffix
    primary = parse_operation_plan(raw_plan("list_directory", path="organize", include_hidden=hidden))
    prepared = prepare_queries(primary, str(tmp_path))
    commands = {"commands": [{"step": 1, "argv": prepared["actions"][0]["_required_argv"], "explanation": "列出指定目录"}]}
    review_commands(prepared, commands)
    question = json.dumps({"status": "need_clarification", "clarification": "是否包含隐藏文件？"})
    intent = Mock(return_value=question)
    planner = Mock(side_effect=[question, question, json.dumps(commands)])
    monkeypatch.setattr("core.intent.chat", intent)
    monkeypatch.setattr("core.engine.chat", planner)
    executor, prompts = Mock(), []
    history = HistoryStore(tmp_path / "history.jsonl")

    def cancel(prompt):
        prompts.append(prompt)
        assert "请输入 yes 确认" in prompt
        return "no"

    assert execute_request(Engine(ssh_hosts=[]), executor, history, user_input, str(tmp_path), "confirm", input_fn=cancel) == "cancelled"
    assert len(prompts) == 1 and planner.call_count == 3
    intent.assert_called_once()
    record = history.query(limit=1)[0]
    assert not record["executed"] and record["clarifications"] == []
    assert record["plan"]["steps"][0]["parameters"] == primary.steps[0].parameters
    assert record["plan_attempts"][0]["raw_output"] == question
    assert record["command_attempts"][0]["raw_output"] == question
    assert record["command_attempts"][0]["review"] == "accepted"
    assert json.loads(planner.call_args_list[1].args[0][1]["content"]) == {
        "cwd": str(tmp_path), "request": user_input, "answers": []}
    executor.execute.assert_not_called()
    executor.execute_query.assert_not_called()
