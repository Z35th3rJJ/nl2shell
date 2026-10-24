import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from cli import execute_request
from core.engine import Engine
from core.history import HistoryStore


@pytest.mark.parametrize("source,directory", [("report (final) [v1].txt", "archive (test)"), ("项目 文件.txt", "归档 目录")])
@pytest.mark.parametrize("operation,verb", [("copy_files", "复制"), ("move_files", "移动")])
def test_unquoted_spaced_paths_reach_exact_preview_and_filesystem(tmp_path, monkeypatch, source, directory, operation, verb):
    (tmp_path / source).write_bytes(b"special path content")
    (tmp_path / directory).mkdir()
    monkeypatch.setattr("core.intent.chat", Mock(return_value='{"status":"need_clarification","clarification":"您想移动文件还是复制文件？"}'))
    monkeypatch.setattr("core.engine.chat", Mock(return_value=json.dumps({"status": "ready", "steps": [{
        "operation": operation, "parameters": {"sources": [source], "destination": directory}}]})))
    history = HistoryStore(tmp_path / "history.jsonl")
    prompts = []
    def confirm(prompt):
        prompts.append(prompt)
        assert len(prompts) == 1 and "请输入 yes 确认" in prompt
        assert not (tmp_path / directory / source).exists()
        return "yes"
    assert execute_request(Engine(ssh_hosts=[]), Mock(), history, f"将 {source} {verb}到 {directory} 目录",
                           str(tmp_path), "confirm", input_fn=confirm) == "verified"
    row = history.query(limit=1)[0]
    assert row["clarifications"] == []
    assert row["plan"]["steps"][0]["parameters"] == {"sources": [source], "destination": directory}
    assert Path(row["preview"][0]["items"][0]["destination"]).as_posix() == f"{directory}/{source}"
    assert (tmp_path / directory / source).read_bytes() == b"special path content"
    assert (tmp_path / source).exists() is (operation == "copy_files")


@pytest.mark.parametrize("source,directory", [("report", "archive (test)"), ("report (final) [v1].txt", "archive")])
def test_truncated_model_paths_cannot_become_ready(tmp_path, monkeypatch, source, directory):
    monkeypatch.setattr("core.intent.chat", Mock(return_value='{"status":"ready","operations":["copy_files"]}'))
    model = Mock(return_value=json.dumps({"status": "ready", "steps": [{"operation": "copy_files", "parameters": {
        "sources": [source], "destination": directory}}]}))
    monkeypatch.setattr("core.engine.chat", model)
    with pytest.raises(ValueError, match="对应关系|目标目录"):
        Engine(ssh_hosts=[]).generate_task_plan("将 report (final) [v1].txt 复制到 archive (test) 目录", str(tmp_path))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("user_input", ["将 one.txt 和 two.txt 复制到 archive (test) 目录",
    "将 report (final) [v1].txt 复制到 archive (test) 或 backup 目录"])
def test_uncertain_path_boundaries_do_not_restore_single_source_intent(monkeypatch, user_input):
    from core.intent import analyze_request
    monkeypatch.setattr("core.intent.chat", Mock(return_value='{"status":"need_clarification","clarification":"请明确完整源路径和目标路径，并用引号分别包住"}'))
    assert analyze_request(user_input, [], "local", [])["status"] == "need_clarification"


@pytest.mark.parametrize("directory", ["archive (test)", "archive"])
def test_independent_reference_uses_same_complete_path_conditions(tmp_path, monkeypatch, directory):
    from core.task_plan import parse_operation_plan
    raw = json.dumps({"status": "ready", "steps": [{"operation": "copy_files", "parameters": {
        "sources": ["report (final) [v1].txt"], "destination": directory}}]})
    model = Mock(side_effect=[raw, '{"commands":[]}'])
    monkeypatch.setattr("core.engine.chat", model)
    monkeypatch.setattr("core.bash_queries.command_request", Mock(return_value=[
        {"role": "system", "content": ""}, {"role": "user", "content": ""}]))
    engine = Engine(ssh_hosts=[])
    args = ("将 report (final) [v1].txt 复制到 archive (test) 目录", str(tmp_path), parse_operation_plan(raw), {}, [])
    if directory == "archive":
        with pytest.raises(ValueError, match="目标目录"):
            engine.generate_bash_commands(*args)
        assert model.call_count == 1
    else:
        assert engine.generate_bash_commands(*args) == {"commands": []}
        assert engine.command_attempts[0]["review"] == "accepted"


def test_ambiguous_unquoted_paths_get_specific_question(tmp_path, monkeypatch):
    monkeypatch.setattr("core.intent.chat", Mock(return_value='{"status":"ready","operations":["copy_files"]}'))
    monkeypatch.setattr("core.engine.chat", Mock(return_value=json.dumps({"status": "ready", "steps": [{
        "operation": "copy_files", "parameters": {"sources": ["one.txt"], "destination": "archive"}}]})))
    plan = Engine(ssh_hosts=[]).generate_task_plan("将 one.txt 和 two.txt 复制到 archive (test) 目录", str(tmp_path))
    assert plan.status == "need_clarification"
    assert "完整源路径和目标路径" in plan.clarification and "配对引号" in plan.clarification
    assert not list(tmp_path.iterdir())
