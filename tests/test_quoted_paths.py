import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from cli import execute_request
from core.engine import Engine
from core.history import HistoryStore


@pytest.mark.parametrize("opening,closing,source,directory", [
    ("“", "”", "项目 文件.txt", "归档 目录"),
    ("‘", "’", "项目 文件.txt", "归档 目录"),
    ('"', '"', "项目 文件.txt", "归档 目录"),
    ("'", "'", "项目 文件.txt", "归档 目录"),
    ('"', '"', "项目“甲” 文件.txt", "归档‘甲’ 目录"),
    ("“", "”", "O'Reilly 项目.txt", "归档 目录"),
])
@pytest.mark.parametrize("operation,verb", [("copy_files", "复制"), ("move_files", "移动")])
def test_quoted_paths_preserve_names_and_reach_confirmation(tmp_path, monkeypatch, opening, closing, source, directory, operation, verb):
    (tmp_path / source).write_bytes(b"quoted path content")
    (tmp_path / directory).mkdir()
    intent = Mock(return_value=json.dumps({"status": "need_clarification", "clarification": "您想移动文件还是复制文件？"}))
    monkeypatch.setattr("core.intent.chat", intent)
    monkeypatch.setattr("core.engine.chat", Mock(return_value=json.dumps({"status": "ready", "steps": [{
        "operation": operation, "parameters": {"sources": [source], "destination": directory}}]})))
    history = HistoryStore(tmp_path / "history.jsonl")
    prompts = []
    def confirm(prompt):
        prompts.append(prompt)
        assert len(prompts) == 1 and "请输入 yes 确认" in prompt
        assert not (tmp_path / directory / source).exists()
        return "yes"
    user_input = f"将{opening}{source}{closing}{verb}到{opening}{directory}{closing}"
    assert execute_request(Engine(ssh_hosts=[]), Mock(), history, user_input, str(tmp_path), "confirm", input_fn=confirm) == "verified"
    row = history.query(limit=1)[0]
    assert row["input"] == user_input and row["clarifications"] == []
    assert row["plan"]["steps"][0]["parameters"] == {"sources": [source], "destination": directory}
    assert Path(row["preview"][0]["items"][0]["destination"]).as_posix() == f"{directory}/{source}"
    assert (tmp_path / directory / source).read_bytes() == b"quoted path content"
    assert (tmp_path / source).exists() is (operation == "copy_files")
    intent.assert_called_once()


def test_quoted_directory_answer_is_shared_with_reference(tmp_path, monkeypatch):
    request = "将“项目 文件.txt”复制到合适的目录"
    answers = ["问题：复制到哪个目录？；用户回答：“归档 目录”"]
    raw = json.dumps({"status": "ready", "steps": [{"operation": "copy_files", "parameters": {
        "sources": ["项目 文件.txt"], "destination": "归档 目录"}}]})
    monkeypatch.setattr("core.intent.chat", Mock(return_value='{"status":"ready","operations":["copy_files"]}'))
    model = Mock(side_effect=[raw, raw, '{"commands":[]}'])
    monkeypatch.setattr("core.engine.chat", model)
    monkeypatch.setattr("core.bash_queries.command_request", Mock(return_value=[
        {"role": "system", "content": ""}, {"role": "user", "content": ""}]))
    engine = Engine(ssh_hosts=[])
    plan = engine.generate_task_plan(request, str(tmp_path), answers)
    assert plan.status == "ready" and plan.steps[0].parameters["destination"] == "归档 目录"
    assert engine.generate_bash_commands(request, str(tmp_path), plan, {}, answers) == {"commands": []}
    assert engine.command_attempts[0]["review"] == "accepted"


@pytest.mark.parametrize("user_input", ["将“项目 文件.txt”复制到“归档 目录", "将“项目 文件.txt”复制到“归档 目录’",
                                       "将“项目 文件.txt’复制到“归档 目录”"])
def test_unpaired_path_quotes_are_not_guessed(tmp_path, monkeypatch, user_input):
    monkeypatch.setattr("core.intent.chat", Mock(return_value='{"status":"ready","operations":["copy_files"]}'))
    monkeypatch.setattr("core.engine.chat", Mock(return_value=json.dumps({"status": "ready", "steps": [{
        "operation": "copy_files", "parameters": {"sources": ["项目 文件.txt"], "destination": "归档 目录"}}]})))
    plan = Engine(ssh_hosts=[]).generate_task_plan(user_input, str(tmp_path))
    assert plan.status == "need_clarification" and not plan.steps
    assert not list(tmp_path.iterdir())


def test_chinese_quoted_directory_browsing_preserves_path(monkeypatch, tmp_path):
    monkeypatch.setattr("core.intent.chat", Mock(return_value='{"status":"ready","operations":["list_directory"]}'))
    raw = json.dumps({"status": "ready", "steps": [{"operation": "list_directory", "parameters": {
        "path": "归档 目录", "include_hidden": False}}]})
    monkeypatch.setattr("core.engine.chat", Mock(return_value=raw))
    plan = Engine(ssh_hosts=[]).generate_task_plan("列出 “归档 目录” 目录的内容", str(tmp_path))
    assert plan.steps[0].parameters == {"path": "归档 目录", "include_hidden": False}


@pytest.mark.parametrize("answers", [[], ["问题：请确认目标目录；用户回答：执行复制操作，将文件复制到名为“归档 目录”的目录中，不要移动源文件"]])
def test_original_quoted_request_and_actual_answer_preserve_destination(tmp_path, monkeypatch, answers):
    monkeypatch.setattr("core.intent.chat", Mock(return_value='{"status":"ready","operations":["copy_files"]}'))
    raw = json.dumps({"status": "ready", "steps": [{"operation": "copy_files", "parameters": {
        "sources": ["项目 文件.txt"], "destination": "归档 目录"}}]})
    monkeypatch.setattr("core.engine.chat", Mock(return_value=raw))
    plan = Engine(ssh_hosts=[]).generate_task_plan("将“项目 文件.txt”复制到“归档 目录”", str(tmp_path), answers)
    assert plan.status == "ready" and plan.steps[0].parameters["destination"] == "归档 目录"


def test_explicit_answer_cannot_be_ignored_when_directory_changes(tmp_path, monkeypatch):
    monkeypatch.setattr("core.intent.chat", Mock(return_value='{"status":"ready","operations":["copy_files"]}'))
    raw = json.dumps({"status": "ready", "steps": [{"operation": "copy_files", "parameters": {
        "sources": ["项目 文件.txt"], "destination": "归档 目录"}}]})
    monkeypatch.setattr("core.engine.chat", Mock(return_value=raw))
    with pytest.raises(ValueError, match="目标目录"):
        Engine(ssh_hosts=[]).generate_task_plan("将“项目 文件.txt”复制到“归档 目录”", str(tmp_path),
            ["问题：请确认目标目录；用户回答：执行复制操作，将文件复制到名为“其他 目录”的目录中，不要移动源文件"])
