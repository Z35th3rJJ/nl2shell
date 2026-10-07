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
