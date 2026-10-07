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
