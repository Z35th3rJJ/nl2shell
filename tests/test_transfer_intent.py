import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from cli import execute_request
from core.engine import Engine
from core.history import HistoryStore
from core.intent import analyze_request


QUESTION = json.dumps({"status": "need_clarification", "clarification": "您想移动文件还是复制文件？"})


@pytest.mark.parametrize("verb,operation", [("移动", "move_files"), ("复制", "copy_files")])
def test_explicit_transfer_corrects_action_question_and_preserves_existing_target(tmp_path, monkeypatch, verb, operation):
    source = tmp_path / "source.txt"
    source.write_bytes(b"new transfer content")
    (tmp_path / "archive").mkdir()
    target = tmp_path / "archive/target.txt"
    target.write_bytes(b"original target content")
    intent = Mock(side_effect=[QUESTION, json.dumps({"status": "ready", "operations": [operation]})])
    monkeypatch.setattr("core.intent.chat", intent)
    monkeypatch.setattr("core.engine.chat", Mock(return_value=json.dumps({"status": "ready", "steps": [{
        "operation": operation, "parameters": {"sources": ["source.txt"], "destination": "archive/target.txt"}}]})))
    launch = Mock(side_effect=AssertionError("no shell"))
    monkeypatch.setattr("subprocess.Popen", launch)
    prompts = []

    def confirm(prompt):
        prompts.append(prompt)
        assert len(prompts) == 1 and "请输入 yes 确认" in prompt
        assert source.read_bytes() == b"new transfer content"
        assert target.read_bytes() == b"original target content"
        assert not (tmp_path / "archive/target_1.txt").exists()
        return "yes"

    history = HistoryStore(tmp_path / "history.jsonl")
    engine = Engine(ssh_hosts=[])
    request = f"将 source.txt {verb}到 archive/target.txt"
    assert execute_request(engine, Mock(), history, request, str(tmp_path), "confirm", input_fn=confirm) == "verified"
    record = history.query(limit=1)[0]
    assert record["clarifications"] == [] and engine.intent_attempts[0]["resolution"]
    assert Path(record["preview"][0]["items"][0]["destination"]).as_posix() == "archive/target_1.txt"
    assert record["verification"][0]["status"] == "verified"
    assert (tmp_path / "archive/target_1.txt").read_bytes() == b"new transfer content"
    assert target.read_bytes() == b"original target content"
    assert source.exists() is (operation == "copy_files")
    assert intent.call_count == 1
    launch.assert_not_called()


def test_repeated_action_conflict_stops_within_existing_limit(monkeypatch):
    response = '{"status":"ready","operations":["copy_files"]}'
    model = Mock(return_value=response)
    monkeypatch.setattr("core.intent.chat", model)
    attempts = []
    with pytest.raises(ValueError, match="结构化意图"):
        analyze_request("将 source.txt 移动到 archive/target.txt", [], "local", attempts)
    assert model.call_count == 2 and all(row["validation_errors"] for row in attempts)


@pytest.mark.parametrize("user_input", ["处理 source.txt", "不要将 source.txt 移动到 archive", "将 source.txt 移动还是复制到 archive？",
                                      "如果需要，将 source.txt 移动到 archive", "将 source.txt 移动到合适的目录"])
def test_uncertain_or_negated_transfer_is_not_forced_ready(monkeypatch, user_input):
    model = Mock(return_value=QUESTION)
    monkeypatch.setattr("core.intent.chat", model)
    assert analyze_request(user_input, [], "local", [])["status"] == "need_clarification"
    model.assert_called_once()
