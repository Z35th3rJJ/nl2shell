import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from cli import execute_request
from core.engine import Engine
from core.history import HistoryStore


def test_confirmed_two_copy_names_do_not_leak_into_the_last_step(tmp_path, monkeypatch):
    request = ("先将 atomic_first_source.txt 复制为 atomic_first_copy.txt，\n"
               "再将 atomic_missing_source.txt 复制为 atomic_missing_copy.txt")
    answer = ("第一步目标文件名为 atomic_first_copy.txt，\n"
              "第二步目标文件名为 atomic_missing_copy.txt。保留这两个文件名，不要改名。")
    sources = ["atomic_first_source.txt", "atomic_missing_source.txt"]
    targets = ["atomic_first_copy.txt", "atomic_missing_copy.txt"]
    for index, source in enumerate(sources):
        (tmp_path / source).write_bytes(f"source {index}".encode())
    raw = json.dumps({"status": "ready", "steps": [{"operation": "copy_files", "parameters": {
        "sources": [source], "destination": ".", "destination_name": target}}
        for source, target in zip(sources, targets)]})
    question = "请明确每个复制源与目标文件名的对应关系"
    model = Mock(side_effect=[json.dumps({"status": "need_clarification", "clarification": question}), raw])
    monkeypatch.setattr("core.engine.chat", model)
    monkeypatch.setattr("core.intent.chat", Mock(return_value=json.dumps({"status": "ready", "operations": ["copy_files"]})))
    launch = Mock(side_effect=AssertionError("复制任务不能启动 Shell"))
    monkeypatch.setattr("subprocess.Popen", launch)
    prompts = []

    def respond(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            assert question in prompt
            return answer
        assert len(prompts) == 2 and "请输入 yes 确认" in prompt, "确认文件名后不应再追问"
        assert not any((tmp_path / target).exists() for target in targets)
        return "yes"

    history, executor = HistoryStore(tmp_path / "history.jsonl"), Mock()
    assert execute_request(Engine(ssh_hosts=[]), executor, history, request, str(tmp_path),
                           "confirm", input_fn=respond) == "verified"
    assert len(prompts) == 2 and model.call_count == 2
    assert request in model.call_args.args[0][1]["content"] and answer in model.call_args.args[0][1]["content"]
    record = history.query(limit=1)[0]
    assert record["clarifications"] == [f"问题：{question}；用户回答：{answer}"]
    assert [(Path(action["items"][0]["source"]).as_posix(), Path(action["items"][0]["destination"]).as_posix())
            for action in record["preview"]] == list(zip(sources, targets))
    assert [outcome["status"] for outcome in record["verification"]] == ["verified", "verified"]
    for index, (source, target) in enumerate(zip(sources, targets)):
        assert (tmp_path / target).read_bytes() == (tmp_path / source).read_bytes() == f"source {index}".encode()
    executor.execute.assert_not_called()
    executor.execute_query.assert_not_called()
    launch.assert_not_called()


@pytest.mark.parametrize("answer,ready", [
    ("第一步目标文件名为 new_first.txt，第二步目标文件名为 new_second.txt", True),
    ("atomic_first_source.txt 的目标文件名为 new_first.txt，atomic_missing_source.txt 的目标文件名为 new_second.txt", True),
    ("目标文件名为 new_first.txt，目标文件名为 new_second.txt", False),
    ("第一步 atomic_missing_source.txt 的目标文件名为 new_first.txt，第二步目标文件名为 new_second.txt", False),
])
def test_name_answers_require_unambiguous_step_or_source_references(tmp_path, monkeypatch, answer, ready):
    request = ("先将 atomic_first_source.txt 复制为 atomic_first_copy.txt，\n"
               "再将 atomic_missing_source.txt 复制为 atomic_missing_copy.txt")
    raw = json.dumps({"status": "ready", "steps": [{"operation": "copy_files", "parameters": {
        "sources": [source], "destination": ".", "destination_name": target}}
        for source, target in [("atomic_first_source.txt", "new_first.txt"), ("atomic_missing_source.txt", "new_second.txt")]]})
    model = Mock(side_effect=[raw])
    monkeypatch.setattr("core.engine.chat", model)
    monkeypatch.setattr("core.intent.chat", Mock(return_value=json.dumps({"status": "ready", "operations": ["copy_files"]})))
    engine = Engine(ssh_hosts=[])
    result = engine.generate_task_plan(request, str(tmp_path), [f"问题：请确认目标文件名；用户回答：{answer}"])
    model.assert_called_once()
    if ready:
        assert result.status == "ready" and not result.clarification
        assert [(step.parameters["sources"], step.parameters["destination_name"]) for step in result.steps] == [
            (["atomic_first_source.txt"], "new_first.txt"), (["atomic_missing_source.txt"], "new_second.txt")]
    else:
        assert result.status == "need_clarification" and not result.steps
    assert engine.command_attempts == [] and not list(tmp_path.iterdir())


def test_clarified_copy_directory_and_name_must_both_match(tmp_path, monkeypatch):
    request = "将 first.txt 复制为 first_copy.txt；将 second.txt 复制为 second_copy.txt"
    answer = "第一步复制到 backup，目标文件名为 first_copy.txt，第二步目标文件名为 second_copy.txt"
    monkeypatch.setattr("core.intent.chat", Mock(return_value=json.dumps({"status": "ready", "operations": ["copy_files"]})))
    for reply, directory, filename, expected in [
        (answer, ".", "first_copy.txt", "need_clarification"),
        (answer, "backup", "first_copy.txt", "ready"),
        (answer, "backup", "changed.txt", "need_clarification"),
        ("第一步复制到，目标文件名为 first_copy.txt，第二步目标文件名为 second_copy.txt", ".", "first_copy.txt", "need_clarification"),
        ("第一步复制到 backup 或 archive，目标文件名为 first_copy.txt，第二步目标文件名为 second_copy.txt", "backup", "first_copy.txt", "need_clarification"),
        ("第一步复制到 backup", "backup", "first_copy.txt", "ready"),
    ]:
        raw = json.dumps({"status": "ready", "steps": [{"operation": "copy_files", "parameters": {
            "sources": [source], "destination": target, "destination_name": name}}
            for source, target, name in [("first.txt", directory, filename), ("second.txt", ".", "second_copy.txt")]]})
        model = Mock(side_effect=[raw])
        monkeypatch.setattr("core.engine.chat", model)
        engine = Engine(ssh_hosts=[])
        result = engine.generate_task_plan(request, str(tmp_path), [f"问题：确认复制条件；用户回答：{reply}"])
        assert result.status == expected, (reply, directory, filename)
        if expected == "ready":
            assert result.steps[0].parameters == {"sources": ["first.txt"], "destination": "backup", "destination_name": "first_copy.txt"}
        else:
            assert not result.steps
        model.assert_called_once()
        assert engine.command_attempts == []
    assert not list(tmp_path.iterdir())


def test_repeated_source_and_name_keep_independent_destination_bindings(tmp_path, monkeypatch):
    request = ("将 shared_source.txt 复制到 first_backup 并命名为 shared_copy.txt；"
               "将 shared_source.txt 复制到 second_backup 并命名为 shared_copy.txt")
    monkeypatch.setattr("core.intent.chat", Mock(return_value=json.dumps({"status": "ready", "operations": ["copy_files"]})))
    for directories, expected in [(["second_backup", "second_backup"], "need_clarification"),
                                  (["second_backup", "first_backup"], "need_clarification"),
                                  (["first_backup", "second_backup"], "ready")]:
        raw = json.dumps({"status": "ready", "steps": [{"operation": "copy_files", "parameters": {
            "sources": ["shared_source.txt"], "destination": directory, "destination_name": "shared_copy.txt"}}
            for directory in directories]})
        model = Mock(side_effect=[raw])
        monkeypatch.setattr("core.engine.chat", model)
        engine = Engine(ssh_hosts=[])
        result = engine.generate_task_plan(request, str(tmp_path))
        assert result.status == expected
        if expected == "ready":
            assert [step.parameters["destination"] for step in result.steps] == ["first_backup", "second_backup"]
        else:
            assert not result.steps
        model.assert_called_once()
        assert engine.command_attempts == []
    assert not list(tmp_path.iterdir())


def test_conflicting_names_in_one_answer_are_not_applied_last_wins(tmp_path, monkeypatch):
    request = "将 first.txt 复制为 first_copy.txt；将 second.txt 复制为 second_copy.txt"
    answer = "第一步目标文件名为 conflicting.txt，第一步目标文件名为 chosen_last.txt，第二步目标文件名为 second_copy.txt"
    raw = json.dumps({"status": "ready", "steps": [{"operation": "copy_files", "parameters": {
        "sources": [source], "destination": ".", "destination_name": name}}
        for source, name in [("first.txt", "chosen_last.txt"), ("second.txt", "second_copy.txt")]]})
    model = Mock(side_effect=[raw])
    monkeypatch.setattr("core.engine.chat", model)
    monkeypatch.setattr("core.intent.chat", Mock(return_value=json.dumps({"status": "ready", "operations": ["copy_files"]})))
    engine = Engine(ssh_hosts=[])
    result = engine.generate_task_plan(request, str(tmp_path), [f"问题：确认文件名；用户回答：{answer}"])
    assert result.status == "need_clarification" and not result.steps
    model.assert_called_once()
    assert engine.command_attempts == [] and not list(tmp_path.iterdir())
