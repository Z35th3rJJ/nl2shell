import json
from unittest.mock import Mock

import pytest

from cli import execute_request
from core.engine import Engine
from core.history import HistoryStore


@pytest.mark.parametrize("source,destination,role", [
    ("../outside.txt", "archive", "源路径"),
    ("source.txt", "../archive", "目标路径"),
])
@pytest.mark.parametrize("model_status", ["unsupported", "ready"])
def test_explicit_transfer_path_rejection_is_program_evidence(tmp_path, monkeypatch, source, destination, role, model_status):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "archive").mkdir()
    (workspace / "source.txt").write_bytes(b"inside")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside unchanged")
    model = Mock(return_value=json.dumps({"status": "unsupported", "reason": "目标位于工作目录外，不能执行"})
                 if model_status == "unsupported" else json.dumps({"status": "ready", "steps": [{
                     "operation": "copy_files", "parameters": {"sources": [source], "destination": destination}}]}))
    monkeypatch.setattr("core.engine.chat", model)
    monkeypatch.setattr("core.intent.chat", Mock(return_value='{"status":"ready","operations":["copy_files"]}'))
    executor, confirm = Mock(), Mock(side_effect=AssertionError("unsafe path cannot reach confirmation"))
    history = HistoryStore(tmp_path / "history.jsonl")
    result = execute_request(Engine(ssh_hosts=[]), executor, history,
        f"将 {source} 复制到 {destination} 目录", str(workspace), "confirm", input_fn=confirm)
    record = history.query(limit=1)[0]
    assert result == "blocked"
    assert "路径安全检查拒绝" in record["block_reason"] and role in record["block_reason"]
    assert "工作目录之外" in record["block_reason"]
    assert record["execution_state"] == "not_started" and not record["executed"]
    assert record["file_change_state"] == "unchanged"
    assert not record["command_attempts"] and not record["preview"]
    assert outside.read_bytes() == b"outside unchanged"
    assert list((workspace / "archive").iterdir()) == []
    confirm.assert_not_called()
    executor.execute.assert_not_called()
    executor.execute_query.assert_not_called()
