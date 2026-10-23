import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from cli import execute_request
from core.engine import Engine
from core.history import HistoryStore


def ready(operation, destination):
    return json.dumps({"status": "ready", "steps": [{"operation": operation,
        "parameters": {"sources": ["source_one.txt"], "destination": destination}}]})


@pytest.mark.parametrize("operation,verb", [("copy_files", "复制"), ("move_files", "移动")])
@pytest.mark.parametrize("program_question", [False, True])
def test_short_directory_answer_reaches_confirmed_filesystem_result(tmp_path, monkeypatch, operation, verb, program_question):
    source = tmp_path / "source_one.txt"
    source.write_bytes(b"confirmed request content")
    question = "请明确复制或移动到哪个目录？" if program_question else f"{verb}到哪个目录？"
    model = Mock(side_effect=[json.dumps({"status": "need_clarification", "clarification": question}),
                              ready(operation, "backup")])
    monkeypatch.setattr("core.engine.chat", model)
    monkeypatch.setattr("core.intent.chat", Mock(return_value=json.dumps({"status": "ready", "operations": [operation]})))
    launch = Mock(side_effect=AssertionError("no shell"))
    monkeypatch.setattr("subprocess.Popen", launch)
    history = HistoryStore(tmp_path / "history.jsonl")
    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        assert not (tmp_path / "backup").exists()
        if len(prompts) == 1:
            assert question in prompt
            return "backup"
        assert len(prompts) == 2 and "请输入 yes 确认" in prompt
        return "yes"

    assert execute_request(Engine(ssh_hosts=[]), Mock(), history,
        f"将 source_one.txt {verb}到合适的目录", str(tmp_path), "confirm", input_fn=answer) == "verified"
    record = history.query(limit=1)[0]
    assert record["clarifications"] == [f"问题：{question}；用户回答：backup"]
    assert Path(record["preview"][0]["items"][0]["destination"]).as_posix() == "backup/source_one.txt"
    assert record["executed"] and record["verification"][0]["status"] == "verified"
    assert (tmp_path / "backup/source_one.txt").read_bytes() == b"confirmed request content"
    assert source.exists() is (operation == "copy_files")
    assert "backup" in model.call_args.args[0][1]["content"]
    assert len(prompts) == 2 and model.call_count == 2
    launch.assert_not_called()


def test_plan_conflict_is_corrected_without_questioning_user(tmp_path, monkeypatch):
    monkeypatch.setattr("core.intent.chat", Mock(return_value='{"status":"ready","operations":["copy_files"]}'))
    model = Mock(side_effect=[ready("copy_files", "wrong"), ready("copy_files", "backup")])
    monkeypatch.setattr("core.engine.chat", model)
    engine = Engine(ssh_hosts=[])
    plan = engine.generate_task_plan("将 source_one.txt 复制到合适的目录", str(tmp_path),
                                    ["问题：复制到哪个目录？；用户回答：backup"])
    assert plan.status == "ready" and plan.steps[0].parameters["destination"] == "backup"
    assert model.call_count == 2 and engine.plan_attempts[0]["validation_errors"]


def test_exhausted_plan_conflict_never_becomes_user_question(tmp_path, monkeypatch):
    monkeypatch.setattr("core.intent.chat", Mock(return_value='{"status":"ready","operations":["copy_files"]}'))
    model = Mock(return_value=ready("copy_files", "wrong"))
    monkeypatch.setattr("core.engine.chat", model)
    with pytest.raises(ValueError, match="目标目录"):
        Engine(ssh_hosts=[]).generate_task_plan("将 source_one.txt 复制到 backup", str(tmp_path))
    assert model.call_count == 2 and not list(tmp_path.iterdir())


@pytest.mark.parametrize("reply,expected", [("backup", "accepted"), ("wrong", "rejected")])
def test_independent_reference_checks_confirmed_directory(tmp_path, monkeypatch, reply, expected):
    from core.task_plan import parse_operation_plan

    model = Mock(side_effect=[ready("copy_files", reply), '{"commands":[]}'])
    monkeypatch.setattr("core.engine.chat", model)
    monkeypatch.setattr("core.bash_queries.command_request", Mock(return_value=[
        {"role": "system", "content": ""}, {"role": "user", "content": ""}]))
    engine = Engine(ssh_hosts=[])
    # 两个模型共同遗漏回答时也必须失败，不能只依赖计划间比较。
    primary = parse_operation_plan(ready("copy_files", reply))
    args = ("将 source_one.txt 复制到合适的目录", str(tmp_path), primary, {},
            ["问题：复制到哪个目录？；用户回答：backup"])
    if expected == "accepted":
        assert engine.generate_bash_commands(*args) == {"commands": []}
        assert engine.command_attempts[0]["review"] == "accepted"
    else:
        with pytest.raises(ValueError, match="目标目录"):
            engine.generate_bash_commands(*args)
        assert model.call_count == 1


@pytest.mark.parametrize("answer", ["不知道", "/exit"])
def test_unresolved_answer_or_exit_never_writes(tmp_path, monkeypatch, answer):
    source = tmp_path / "source_one.txt"
    source.write_bytes(b"keep")
    question = '{"status":"need_clarification","clarification":"复制到哪个目录？"}'
    model = Mock(side_effect=[question, ready("copy_files", "backup")])
    monkeypatch.setattr("core.engine.chat", model)
    monkeypatch.setattr("core.intent.chat", Mock(return_value='{"status":"ready","operations":["copy_files"]}'))
    history = HistoryStore(tmp_path / "history.jsonl")
    replies = iter([answer, "/exit"])
    with pytest.raises(SystemExit):
        execute_request(Engine(ssh_hosts=[]), Mock(), history,
            "将 source_one.txt 复制到合适的目录", str(tmp_path), "confirm", input_fn=lambda _: next(replies))
    record = history.query(limit=1)[0]
    assert record["status"] == "cancelled" and record["execution_state"] == "not_started"
    assert record["file_change_state"] == "unchanged" and not record["executed"]
    assert all("/exit" not in value for value in record["clarifications"])
    assert model.call_count == (1 if answer == "/exit" else 2)
    assert source.read_bytes() == b"keep" and not (tmp_path / "backup").exists()


def test_invalid_json_stops_as_plan_failure_without_clarification(tmp_path, monkeypatch):
    monkeypatch.setattr("core.intent.chat", Mock(return_value='{"status":"ready","operations":["copy_files"]}'))
    model = Mock(return_value="invalid json")
    monkeypatch.setattr("core.engine.chat", model)
    history = HistoryStore(tmp_path / "history.jsonl")
    prompt = Mock(side_effect=AssertionError("model error cannot question the user"))
    assert execute_request(Engine(ssh_hosts=[]), Mock(), history, "将 source_one.txt 复制到 backup",
                           str(tmp_path), "confirm", input_fn=prompt) == "plan_failed"
    record = history.query(limit=1)[0]
    assert len(record["plan_attempts"]) == 2
    assert all(attempt["failure_kind"] == "model_plan" for attempt in record["plan_attempts"])
    assert record["execution_state"] == "not_started" and not record["executed"]
    prompt.assert_not_called()


def test_repeated_question_about_confirmed_directory_is_model_error(tmp_path, monkeypatch):
    question = '{"status":"need_clarification","clarification":"复制到哪个目录？"}'
    monkeypatch.setattr("core.intent.chat", Mock(return_value='{"status":"ready","operations":["copy_files"]}'))
    model = Mock(side_effect=[question, ready("copy_files", "backup")])
    monkeypatch.setattr("core.engine.chat", model)
    engine = Engine(ssh_hosts=[])
    plan = engine.generate_task_plan("将 source_one.txt 复制到合适的目录", str(tmp_path),
                                    ["问题：复制到哪个目录？；用户回答：backup"])
    assert plan.status == "ready" and plan.steps[0].parameters["destination"] == "backup"
    assert engine.plan_attempts[0]["failure_kind"] == "model_plan"
    assert model.call_count == 2
