"""终端与历史必须依据真实执行证据，而不是计划中的写操作类型。"""
import errno
from pathlib import Path
from unittest.mock import Mock

import pytest

from cli import execute_request, print_history
from core.execution import ExecutionResult, ExecutionStartError
from core.history import HistoryStore
from core.operations import execute_action, prepare_plan
from core.task_plan import TaskPlan, TaskStep


def engine_for(*steps):
    engine = Mock()
    engine.generate_task_plan.return_value = TaskPlan(steps)
    engine.suggest_fix.return_value = "写操作已经产生部分修改。"
    engine.generate_bash_commands.side_effect = lambda request, cwd, plan, prepared, answers, **kwargs: {
        "commands": [{"step": a["step"], "argv": a["_required_argv"], "explanation": "按原范围查询"}
                     for a in prepared["actions"]]}
    return engine


def copy(source, target):
    return TaskStep(operation="copy_files", parameters={"sources": [source], "destination": ".", "destination_name": target})


def test_missing_second_source_is_preparation_failure_without_mutations(tmp_path, capsys):
    (tmp_path / "first.txt").write_bytes(b"first")
    engine = engine_for(copy("first.txt", "first-copy.txt"), copy("missing.txt", "missing-copy.txt"))
    history = HistoryStore(tmp_path / "audit.jsonl")
    confirm = Mock(side_effect=AssertionError("准备失败不得请求授权"))
    assert execute_request(engine, Mock(), history, "复制两个文件", str(tmp_path), "confirm", input_fn=confirm) == "blocked"
    output = capsys.readouterr().out
    assert "可能已有部分修改" not in output and "已经产生部分修改" not in output
    assert "未进入执行阶段" in output
    record = history.query(limit=1)[0]
    assert record["phase"] == "preparation" and not record["authorized"]
    assert record["execution_state"] == "not_started" and not record["executed"]
    assert record["file_change_state"] == "unchanged"
    assert [row["status"] for row in record["verification"]] == ["not_executed", "not_executed"]
    assert all(row["detail"] for row in record["verification"])
    assert not (tmp_path / "first-copy.txt").exists() and not (tmp_path / "missing-copy.txt").exists()
    assert (tmp_path / "first.txt").read_bytes() == b"first"
    engine.suggest_fix.assert_not_called()
    confirm.assert_not_called()


def test_parent_creation_before_copy_failure_is_execution_evidence(tmp_path, monkeypatch):
    (tmp_path / "source.txt").write_bytes(b"source")
    step = TaskStep(operation="copy_files", parameters={"sources": ["source.txt"], "destination": "new/nested"})
    prepared = prepare_plan(TaskPlan((step,)), str(tmp_path))
    real_workspace_path = __import__("core.operations", fromlist=["workspace_path"]).workspace_path

    def fail_after_parents(root, value, **kwargs):
        if Path(value).is_absolute() and (tmp_path / "new/nested").exists():
            raise PermissionError(errno.EACCES, "fixture recheck failed")
        return real_workspace_path(root, value, **kwargs)

    monkeypatch.setattr("core.operations.workspace_path", fail_after_parents)
    result = execute_action(prepared, prepared["actions"][0])
    assert (tmp_path / "new/nested").is_dir() and not (tmp_path / "new/nested/source.txt").exists()
    assert result["executed"] and result["execution_state"] != "not_started"
    assert result["file_change_state"] in {"changed", "unknown"}
    assert result["failure_phase"] == "execution"


def test_three_steps_preserve_changed_failed_and_unexecuted(tmp_path, monkeypatch, capsys):
    import core.operations as operations
    for name in ("first", "second", "third"):
        (tmp_path / (name + ".txt")).write_bytes(name.encode())
    actual_copy = operations._copy

    def fail(source, destination, deadline):
        if source.name == "second.txt":
            destination.write_bytes(b"partial")
            raise OSError(errno.ENOSPC, "fixture disk full")
        actual_copy(source, destination, deadline)

    monkeypatch.setattr(operations, "_copy", fail)
    history = HistoryStore(tmp_path / "audit.jsonl")
    engine = engine_for(*(copy(n + ".txt", n + "-copy.txt") for n in ("first", "second", "third")))
    assert execute_request(engine, Mock(), history, "复制三步", str(tmp_path), "confirm", input_fn=lambda _: "yes") == "execution_failed"
    record = history.query(limit=1)[0]
    assert [row["status"] for row in record["verification"]] == ["verified", "execution_failed", "not_executed"]
    assert record["executed"] and record["file_change_state"] == "changed"
    assert record["verification"][2]["blocked_by_step"] == 2
    assert (tmp_path / "first-copy.txt").read_bytes() == b"first"
    assert (tmp_path / "second-copy.txt").read_bytes() == b"partial"
    assert not (tmp_path / "third-copy.txt").exists()
    assert "未执行" in capsys.readouterr().out


def test_unknown_query_exception_is_not_displayed_as_not_executed(tmp_path, capsys):
    history = HistoryStore(tmp_path / "audit.jsonl")
    executor = Mock()
    executor.execute_query.side_effect = OSError("fixture result capture failed")
    assert execute_request(engine_for(TaskStep(operation="working_directory")), executor, history, "显示目录", str(tmp_path), "confirm", input_fn=lambda _: "yes") == "execution_failed"
    record = history.query(limit=1)[0]
    assert record["execution_state"] == "unknown"
    assert "执行状态：未执行" not in capsys.readouterr().out
    print_history(history)
    assert "无法确定" in capsys.readouterr().out


def test_repair_does_not_erase_a_previous_started_attempt(tmp_path):
    history = HistoryStore(tmp_path / "audit.jsonl")
    executor = Mock()
    executor.execute_query.side_effect = [
        (ExecutionResult(2, "", "Resource temporarily unavailable", 0.01), ["/usr/bin/pwd", "-P"]),
        ExecutionStartError(errno.EAGAIN, "Resource temporarily unavailable"),
    ]
    assert execute_request(engine_for(TaskStep(operation="working_directory")), executor, history, "显示目录", str(tmp_path), "confirm", input_fn=lambda _: "yes") == "execution_failed"
    record = history.query(limit=1)[0]
    assert record["repairs"][0]["initial_outcome"]["executed"]
    assert not record["repairs"][0]["outcome"]["executed"]
    assert record["executed"] and record["execution_state"] != "not_started"
    authorization = next(row for row in history.query(limit=None) if row["status"] == "repair_confirmed")
    assert authorization["executed"]


def test_unexpected_runner_exception_retains_failed_and_blocked_steps(tmp_path, monkeypatch):
    (tmp_path / "first.txt").write_bytes(b"first")

    def unexpected(prepared, action, *args, **kwargs):
        (tmp_path / "first-copy.txt").write_bytes(b"partial")
        raise RuntimeError("fixture lost runner result")

    monkeypatch.setattr("cli.execute_action", unexpected)
    history = HistoryStore(tmp_path / "audit.jsonl")
    engine = engine_for(copy("first.txt", "first-copy.txt"), TaskStep(operation="create_file", parameters={"path": "later"}))
    assert execute_request(engine, Mock(), history, "两步操作", str(tmp_path), "confirm", input_fn=lambda _: "yes") == "execution_failed"
    record = history.query(limit=1)[0]
    assert record["execution_state"] == "unknown" and record["file_change_state"] == "unknown"
    assert [row["status"] for row in record["verification"]] == ["execution_failed", "not_executed"]
    assert not (tmp_path / "later").exists()


@pytest.mark.parametrize("fault", ["capture", "interrupt"])
def test_started_process_keeps_start_evidence_when_result_is_lost(tmp_path, monkeypatch, fault):
    from core.bash_queries import prepare_queries, review_commands, execute_query
    from core.execution import BashExecutor

    prepared = prepare_queries(TaskPlan((TaskStep(operation="working_directory"),)), str(tmp_path))
    action = prepared["actions"][0]
    review_commands(prepared, {"commands": [{"step": 1, "argv": action["_required_argv"], "explanation": "查询"}]})
    process = Mock(returncode=0)
    process.poll.return_value = 0
    if fault == "interrupt":
        process.wait.side_effect = KeyboardInterrupt()
    else:
        monkeypatch.setattr("core.execution._read_limited", Mock(side_effect=PermissionError("fixture capture denied")))
    monkeypatch.setattr("core.execution.subprocess.Popen", Mock(return_value=process))
    terminate = Mock()
    monkeypatch.setattr("core.execution._terminate_process_tree", terminate)
    executor = Mock()
    executor.execute_query.side_effect = lambda *args, **kwargs: BashExecutor()._execute_argv(["fixture-no-real-process"], 1)
    result = execute_query(prepared, action, executor)
    assert result["executed"] and result["execution_state"] == "stopped"
    assert result["file_change_state"] == "unchanged"
    assert result["status"] == ("interrupted" if fault == "interrupt" else "execution_failed")
    assert terminate.call_count == (1 if fault == "interrupt" else 0)


def test_items_not_reached_in_same_action_are_retained(tmp_path, monkeypatch):
    import core.operations as operations

    for name in ("a", "b", "c"):
        (tmp_path / name).write_bytes(name.encode())
    step = TaskStep(operation="copy_files", parameters={"sources": ["a", "b", "c"], "destination": "backup"})
    prepared = prepare_plan(TaskPlan((step,)), str(tmp_path))
    original = operations._copy

    def interrupt(source, destination, deadline):
        if source.name == "b":
            raise KeyboardInterrupt()
        original(source, destination, deadline)

    monkeypatch.setattr(operations, "_copy", interrupt)
    result = execute_action(prepared, prepared["actions"][0])
    assert result["status"] == "interrupted" and result["file_change_state"] == "changed"
    assert [row["status"] for row in result["items"]] == ["verified", "failed", "not_executed"]
    assert [row["executed"] for row in result["items"]] == [True, True, False]
    assert (tmp_path / "backup/a").read_bytes() == b"a" and not (tmp_path / "backup/c").exists()


def test_unknown_execution_is_preserved_in_next_planner_context(tmp_path, monkeypatch):
    import json
    from core.engine import Engine

    engine = Engine(ssh_hosts=[])
    generate = engine.generate_task_plan
    monkeypatch.setattr(engine, "generate_task_plan", Mock(return_value=TaskPlan((TaskStep(operation="create_file", parameters={"path": "target"}),))))
    monkeypatch.setattr("cli.execute_action", Mock(side_effect=RuntimeError("fixture result lost")))
    history = HistoryStore(tmp_path / "audit.jsonl")
    assert execute_request(engine, Mock(), history, "创建 target", str(tmp_path), "confirm", input_fn=lambda _: "yes") == "execution_failed"
    assert engine._task_history[-1].execution_state == "unknown"
    assert engine._task_history[-1].file_change_state == "unknown"
    monkeypatch.setattr(engine, "generate_task_plan", generate)
    monkeypatch.setattr("core.engine.analyze_request", lambda *args: {"status": "ready", "operations": ["working_directory"]})
    model = Mock(return_value=json.dumps({"status": "ready", "steps": [{"operation": "working_directory", "parameters": {}}]}))
    monkeypatch.setattr("core.engine.chat", model)
    assert engine.generate_task_plan("查询当前工作目录", str(tmp_path)).status == "ready"
    prompt = model.call_args.args[0][1]["content"]
    assert '"execution_state": "unknown"' in prompt and '"file_change_state": "unknown"' in prompt
    assert "executed=false 表示没有发生" not in prompt


def test_batch_interrupt_stops_and_preserves_unknown_execution(tmp_path):
    import json
    from cli import run_batch

    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text("\n".join(json.dumps({"input": text, "cwd": str(tmp_path)}) for text in ("查询一次", "不得运行这次")), encoding="utf-8")
    history, executor = HistoryStore(tmp_path / "audit.jsonl"), Mock()
    executor.execute_query.side_effect = KeyboardInterrupt()
    summary = run_batch(engine_for(TaskStep(operation="working_directory")), executor, history, tasks)
    assert summary["interrupted"] and len(summary["results"]) == 1
    assert history.query(limit=1)[0]["execution_state"] == "unknown"
    executor.execute_query.assert_called_once()


def test_preparation_failure_does_not_reuse_previous_command_attempts(tmp_path):
    engine = engine_for(TaskStep(operation="read_text", parameters={"path": "missing", "end": "head", "lines": 1}))
    engine.command_attempts = [{"phase": "candidate_commands", "raw_output": "previous task only"}]
    history = HistoryStore(tmp_path / "audit.jsonl")
    assert execute_request(engine, Mock(), history, "查看文件", str(tmp_path), "confirm") == "blocked"
    assert history.query(limit=1)[0]["command_attempts"] == []
    engine.generate_bash_commands.assert_not_called()


def test_agent_exit_preserves_earlier_modification_evidence(tmp_path):
    from core.agent import execute_agent

    rules = [{"id": f"a{index}", "when": [], "step": {"operation": "create_file", "parameters": {"path": name}}}
             for index, name in enumerate(("first", "later"), 1)]
    engine = Mock()
    engine.agent_attempts = []
    engine.agent_contract.return_value = {"status": "ready", "rules": rules}
    engine.agent_decide.side_effect = [{"status": "act", "rule": rule["id"], "step": rule["step"], "basis": []}
                                      for rule in rules]
    history = HistoryStore(tmp_path / "audit.jsonl")
    answers = iter(["yes", "/exit"])
    with pytest.raises(SystemExit):
        execute_agent(engine, Mock(), history, "创建两个明确文件", str(tmp_path), "confirm", input_fn=lambda _: next(answers))
    record = history.query(limit=1)[0]
    assert record["agent"] and record["status"] == "cancelled"
    assert record["executed"] and record["file_change_state"] == "changed"
    assert (tmp_path / "first").is_file() and not (tmp_path / "later").exists()
