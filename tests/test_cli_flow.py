from unittest.mock import Mock

from cli import execute_request
from core.execution import ExecutionResult
from core.settings import AUTO_SAFE, PREVIEW
from core.task_plan import TaskPlan, TaskStep


class History:
    def __init__(self):
        self.records = []

    def append(self, record):
        self.records.append(record)


def _engine(*steps):
    engine = Mock()
    engine.generate_task_plan.return_value = TaskPlan(tuple(steps))
    engine.suggest_fix.return_value = "检查目标路径"
    return engine


def test_preview_never_calls_bash():
    engine = _engine(TaskStep("ls", "列出文件", "显示列表", ""))
    executor, history = Mock(), History()
    execute_request(engine, executor, history, "查看文件", "/work", PREVIEW)
    executor.execute.assert_not_called()
    assert history.records[0]["status"] == "preview"


def test_destructive_command_is_blocked_before_execution():
    engine = _engine(TaskStep("rm -rf /", "删除系统", "", ""))
    executor, history = Mock(), History()
    execute_request(engine, executor, history, "删除全部", "/work", AUTO_SAFE)
    executor.execute.assert_not_called()
    assert history.records[0]["status"] == "blocked"


def test_verification_failure_generates_only_one_fix_suggestion():
    engine = _engine(
        TaskStep("touch result.txt", "创建文件", "文件存在", "test -e result.txt"),
        TaskStep("ls", "列出文件", "显示列表", ""),
    )
    executor, history = Mock(), History()
    executor.is_available.return_value = True
    executor.execute.side_effect = [
        ExecutionResult(0, "", "", 0.1),
        ExecutionResult(1, "", "missing", 0.1),
    ]
    execute_request(engine, executor, history, "创建并查看文件", "/work", AUTO_SAFE)
    engine.suggest_fix.assert_called_once()
    assert executor.execute.call_count == 2
    assert history.records[0]["status"] == "verification_failed"
