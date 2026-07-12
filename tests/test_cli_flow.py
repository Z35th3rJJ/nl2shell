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


def test_candidate_and_target_are_confirmed_then_plan_is_regenerated_once(tmp_path):
    (tmp_path / "README.md").write_text("docs", encoding="utf-8")
    engine = Mock()
    engine.generate_task_plan.side_effect = [
        TaskPlan((TaskStep("cp readme .", "复制文件", "", ""),)),
        TaskPlan((TaskStep("cp README.md README.backup.md", "备份文件", "", ""),)),
    ]
    executor, history = Mock(), History()
    answers = iter(["y", "README.backup.md"])
    execute_request(engine, executor, history, "复制readme", str(tmp_path), PREVIEW,
                    input_fn=lambda _: next(answers))
    assert engine.generate_task_plan.call_count == 2
    assert engine.generate_task_plan.call_args.kwargs["clarifications"]
    executor.execute.assert_not_called()
    assert history.records[0]["preflight"]["confirmed_candidates"][0]["selected"] == "README.md"


def test_missing_source_without_candidate_blocks_bash(tmp_path):
    engine = _engine(TaskStep("cat missing.txt", "查看文件", "", ""))
    executor, history = Mock(), History()
    execute_request(engine, executor, history, "查看文件", str(tmp_path), AUTO_SAFE)
    executor.execute.assert_not_called()
    assert history.records[0]["status"] == "preflight_failed"


def test_user_can_select_from_multiple_file_candidates(tmp_path):
    (tmp_path / "README.md").write_text("", encoding="utf-8")
    (tmp_path / "README.txt").write_text("", encoding="utf-8")
    engine = Mock()
    engine.generate_task_plan.side_effect = [
        TaskPlan((TaskStep("cat readme", "查看", "", ""),)),
        TaskPlan((TaskStep("cat README.txt", "查看", "", ""),)),
    ]
    executor, history = Mock(), History()
    execute_request(engine, executor, history, "查看readme", str(tmp_path), PREVIEW,
                    input_fn=lambda _: "2")
    selected = history.records[0]["preflight"]["confirmed_candidates"][0]["selected"]
    assert selected == "README.txt"


def test_clarification_regenerates_only_once(tmp_path):
    engine = Mock()
    engine.generate_task_plan.side_effect = [
        TaskPlan((), "要复制成什么文件名？"),
        TaskPlan((), "仍然缺少目标文件名"),
    ]
    executor, history = Mock(), History()
    execute_request(engine, executor, history, "复制 README.md", str(tmp_path), PREVIEW,
                    input_fn=lambda _: "README.backup.md")
    assert engine.generate_task_plan.call_count == 2
    executor.execute.assert_not_called()
    assert history.records[0]["status"] == "preflight_failed"
