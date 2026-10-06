import json
from unittest.mock import Mock

import pytest

from cli import BATCH, execute_request, json_result
from core.task_plan import TaskPlan, TaskStep
from core.settings import AUTO_SAFE, PREVIEW


class History:
    def __init__(self):
        self.records = []

    def append(self, record):
        self.records.append(record)


def engine_for(*steps):
    engine = Mock()
    engine.generate_task_plan.return_value = TaskPlan(steps)
    return engine


def step(operation, **parameters):
    return TaskStep(operation=operation, parameters=parameters)


def test_preview_does_not_create_files_or_call_bash(tmp_path):
    executor, history = Mock(), History()
    status = execute_request(engine_for(step("create_file", path="a")), executor, history,
                             "创建 a", str(tmp_path), PREVIEW)
    assert status == "preview"
    assert not (tmp_path / "a").exists()
    executor.execute.assert_not_called()


@pytest.mark.parametrize("mode,assume_yes", [("confirm", False), (AUTO_SAFE, False), (AUTO_SAFE, True)])
def test_writes_always_need_explicit_confirmation(tmp_path, mode, assume_yes):
    executor, history = Mock(), History()
    status = execute_request(engine_for(step("create_file", path="a")), executor, history,
                             "创建 a", str(tmp_path), mode, input_fn=lambda _: "n", assume_yes=assume_yes)
    assert status == "cancelled"
    assert not (tmp_path / "a").exists()
    executor.execute.assert_not_called()


def test_confirmed_copy_is_verified_and_logged(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"source")
    executor, history = Mock(), History()
    status = execute_request(engine_for(step("copy_files", sources=["a.txt"], destination="backup")),
                             executor, history, "备份", str(tmp_path), "confirm", input_fn=lambda _: "yes")
    assert status == "verified"
    assert (tmp_path / "backup/a.txt").read_bytes() == b"source"
    assert history.records[-1]["executed"]
    assert history.records[-1]["verification"][0]["status"] == "verified"
    executor.execute.assert_not_called()


@pytest.mark.parametrize("command", ["rm -rf /", "sed -i 's/a/b/' file", "ls\ntouch a", "cd .. && touch a"])
def test_legacy_commands_are_blocked_even_with_yes(tmp_path, command):
    executor, history = Mock(), History()
    status = execute_request(engine_for(TaskStep(command=command)), executor, history,
                             "任务", str(tmp_path), AUTO_SAFE, input_fn=lambda _: "yes", assume_yes=True)
    assert status == "blocked"
    assert not history.records[-1]["executed"]
    executor.execute.assert_not_called()


def test_model_refusal_is_logged_without_crash(tmp_path):
    engine, executor, history = Mock(), Mock(), History()
    engine.generate_task_plan.return_value = TaskPlan((), refused=True, reason="暂不支持安装软件")
    assert execute_request(engine, executor, history, "安装", str(tmp_path), "confirm") == "unsupported"
    assert history.records[-1]["block_reason"] == "暂不支持安装软件"
    executor.execute.assert_not_called()


def test_model_failure_is_logged_without_execution(tmp_path):
    engine, executor, history = Mock(), Mock(), History()
    engine.generate_task_plan.side_effect = RuntimeError("模型不可用")
    assert execute_request(engine, executor, history, "查找", str(tmp_path), "confirm") == "plan_failed"
    executor.execute.assert_not_called()


def test_clarification_keeps_original_request_and_answers(tmp_path):
    engine, executor, history = Mock(), Mock(), History()
    engine.generate_task_plan.side_effect = [TaskPlan((), clarification="包含子目录吗？"),
        TaskPlan((), clarification="备份到哪里？"),
        TaskPlan((step("create_directory", path="backup"),))]
    answers = iter(["包含", "backup", "yes"])
    assert execute_request(engine, executor, history, "备份文件", str(tmp_path), "confirm",
                           input_fn=lambda _: next(answers)) == "verified"
    assert len(engine.generate_task_plan.call_args.kwargs["clarifications"]) == 2
    assert engine.generate_task_plan.call_args.args[0] == "备份文件"


def test_preview_clarification_never_prompts(tmp_path):
    engine = Mock()
    engine.generate_task_plan.return_value = TaskPlan((), clarification="哪些文件？")
    status = execute_request(engine, Mock(), History(), "删除", str(tmp_path), PREVIEW,
                             input_fn=lambda _: pytest.fail("预览不能询问执行"))
    assert status == "needs_clarification"


def test_batch_writes_cancelled_and_reads_succeed(tmp_path):
    assert execute_request(engine_for(step("create_file", path="a")), Mock(), History(),
                           "创建", str(tmp_path), BATCH) == "cancelled"
    engine = engine_for(step("count_files", path=".", recursive=False))
    assert execute_request(engine, Mock(), History(), "计数", str(tmp_path), BATCH) == "verified"
    engine.remember_task.assert_not_called()


def test_confirmation_change_rechecks_target_name(tmp_path):
    (tmp_path / "a").write_bytes(b"data")
    history, calls = History(), []

    def confirm(message):
        calls.append(message)
        if len(calls) == 1:
            (tmp_path / "backup").mkdir()
            (tmp_path / "backup/a").write_bytes(b"keep")
        return "yes"

    status = execute_request(engine_for(step("copy_files", sources=["a"], destination="backup")),
                             Mock(), history, "备份", str(tmp_path), "confirm", input_fn=confirm)
    assert status == "verified"
    assert len(calls) == 2
    assert (tmp_path / "backup/a").read_bytes() == b"keep"
    assert (tmp_path / "backup/a_1").read_bytes() == b"data"


def test_failure_stops_later_steps_and_records_partial_progress(tmp_path, monkeypatch):
    import core.operations
    (tmp_path / "a").write_bytes(b"a")
    (tmp_path / "b").write_bytes(b"b")
    original = core.operations._copy

    def fail_second(source, destination, deadline):
        if source.name == "b":
            raise OSError("磁盘写入失败")
        original(source, destination, deadline)

    monkeypatch.setattr(core.operations, "_copy", fail_second)
    history = History()
    status = execute_request(engine_for(step("copy_files", sources=["a", "b"], destination="backup"),
                                       step("create_file", path="later")), Mock(), history,
                             "备份并创建", str(tmp_path), "confirm", input_fn=lambda _: "yes")
    assert status == "execution_failed"
    assert (tmp_path / "backup/a").read_bytes() == b"a"
    assert not (tmp_path / "later").exists()
    assert history.records[-1]["verification"][0]["items"][0]["status"] == "verified"
    assert history.records[-1]["verification"][1]["status"] == "not_executed"


def test_audit_failure_prevents_file_write(tmp_path):
    history = Mock()
    history.append.side_effect = PermissionError("不可写")
    status = execute_request(engine_for(step("create_file", path="a")), Mock(), history,
                             "创建", str(tmp_path), "confirm", input_fn=lambda _: "yes")
    assert status == "audit_failed"
    assert not (tmp_path / "a").exists()


def test_json_result_has_stable_public_shape():
    payload = json_result({"risk": "SAFE", "command": ""}, "preview")
    assert set(payload) == {"status", "risk_level", "steps", "verification", "duration_seconds", "error"}


def test_restore_command_does_not_depend_on_model(tmp_path):
    (tmp_path / "a").write_bytes(b"data")
    history = History()
    execute_request(engine_for(step("trash", sources=["a"])), Mock(), history,
                    "删除 a", str(tmp_path), "confirm", input_fn=lambda _: "yes")
    identifier = history.records[-1]["verification"][0]["items"][0]["trash_id"]
    engine = Mock()
    status = execute_request(engine, Mock(), history, f"/restore {identifier}", str(tmp_path),
                             "confirm", input_fn=lambda _: "yes")
    assert status == "verified"
    assert (tmp_path / "a").read_bytes() == b"data"
    engine.generate_task_plan.assert_not_called()


def test_end_of_input_cancels_without_modifying_files(tmp_path):
    def end(_):
        raise EOFError
    assert execute_request(engine_for(step("create_file", path="a")), Mock(), History(),
                           "创建", str(tmp_path), "confirm", input_fn=end) == "cancelled"
    assert not (tmp_path / "a").exists()


def test_audit_failure_after_execution_preserves_result_in_json(tmp_path):
    history = Mock()
    history.append.side_effect = [None, PermissionError("记录不可写")]
    status = execute_request(engine_for(step("create_file", path="a")), Mock(), history,
                             "创建", str(tmp_path), "confirm", input_fn=lambda _: "yes")
    assert status == "audit_failed"
    assert (tmp_path / "a").exists()
    assert history.last_record["executed"]
    assert "记录不可写" in json_result(history.last_record, status)["error"]


def test_json_mode_is_single_object_and_never_prompts_for_write(tmp_path, monkeypatch, capsys):
    import argparse
    import cli
    from core.history import HistoryStore
    from core.settings import AppSettings
    engine = engine_for(step("create_file", path="a"))
    monkeypatch.setattr(cli, "Engine", lambda: engine)
    monkeypatch.setattr(cli, "HistoryStore", lambda: HistoryStore(tmp_path / "history.jsonl"))
    monkeypatch.setattr(cli, "create_executor", Mock)
    monkeypatch.setattr(cli, "load_settings", lambda: AppSettings("confirm", True))
    monkeypatch.setattr(cli, "recover_working_directory", lambda: (str(tmp_path), ""))
    args = argparse.Namespace(task="创建 a", batch=None, timeout=60, preview=False, yes=True, json=True)
    cli.main(args=args)
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "cancelled"
    assert payload["steps"][0]["operation"] == "create_file"
    assert not (tmp_path / "a").exists()
