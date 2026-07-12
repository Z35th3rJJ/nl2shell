import json
from unittest.mock import Mock

from cli import run_batch
from core.execution import ExecutionResult
from core.history import HistoryStore
from core.task_plan import TaskPlan, TaskStep


def _engine(command):
    engine = Mock()
    engine.generate_task_plan.return_value = TaskPlan((TaskStep(command, "", "", ""),))
    return engine


def test_batch_runs_tasks_serially_and_saves_summary(tmp_path):
    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text(json.dumps({"input": "列出文件", "cwd": str(tmp_path)}) + "\n", encoding="utf-8")
    executor = Mock()
    executor.is_available.return_value = True
    executor.execute.return_value = ExecutionResult(0, "ok\n", "", 0.1)
    history = HistoryStore(tmp_path / "history.jsonl")

    summary = run_batch(_engine("echo ok"), executor, history, tasks)

    assert summary["success"] == 1
    assert summary["failed"] == 0
    assert (tmp_path / f"batch_{summary['batch_id']}.json").exists()
    assert history.query(limit=None, batch_id=summary["batch_id"])[0]["status"] == "batch_summary"


def test_batch_blocks_high_risk_command_and_continues(tmp_path):
    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text(json.dumps({"input": "删除全部"}) + "\n", encoding="utf-8")
    executor = Mock()
    history = HistoryStore(tmp_path / "history.jsonl")

    summary = run_batch(_engine("rm -rf /"), executor, history, tasks)

    assert summary["blocked"] == 1
    executor.execute.assert_not_called()
