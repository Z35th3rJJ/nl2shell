from unittest.mock import Mock

from cli import execute_request, json_result
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


def test_workspace_root_delete_records_structured_block_reason(tmp_path):
    engine = _engine(TaskStep(f"rm -rf {tmp_path.as_posix()}", "删除项目", "", ""))
    executor, history = Mock(), History()
    status = execute_request(engine, executor, history, "删除", str(tmp_path), AUTO_SAFE)
    assert status == "blocked"
    assert history.records[0]["block_rule"] == "workspace_root_delete"
    payload = json_result(history.records[0], status)
    assert payload["error"] == "禁止删除当前工作区本身或其父目录"
    executor.execute.assert_not_called()


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
    assert history.records[0]["verification"][-1]["status"] == "not_executed"


def test_assume_yes_only_skips_safe_confirmation():
    engine = _engine(TaskStep("ls", "", "", ""))
    executor, history = Mock(), History()
    executor.is_available.return_value = True
    executor.execute.return_value = ExecutionResult(0, "", "", 0.1)
    execute_request(engine, executor, history, "输出", "/work", "confirm",
                    input_fn=lambda _: (_ for _ in ()).throw(AssertionError("不应询问")), assume_yes=True)
    assert history.records[0]["status"] == "exit_code_only"


def test_json_result_has_stable_public_shape():
    payload = json_result({"risk": "SAFE", "command": "ls"}, "preview")
    assert set(payload) == {"status", "risk_level", "steps", "verification",
                            "duration_seconds", "error"}


def test_candidate_and_target_are_corrected_without_regenerating_plan(tmp_path):
    (tmp_path / "README.md").write_text("docs", encoding="utf-8")
    engine = _engine(TaskStep("cp readme .", "复制文件", "", ""))
    executor, history = Mock(), History()
    answers = iter(["", ""])
    execute_request(engine, executor, history, "复制readme", str(tmp_path), PREVIEW,
                    input_fn=lambda _: next(answers))
    assert engine.generate_task_plan.call_count == 1
    executor.execute.assert_not_called()
    preflight = history.records[0]["preflight"]
    assert preflight["confirmed_candidates"][0]["selected"] == "README.md"
    assert preflight["corrected_commands"] == ["cp README.md README_copy.md"]
    assert preflight["used_default_target"] is True


def test_missing_source_without_candidate_blocks_bash(tmp_path):
    engine = _engine(TaskStep("cat missing.txt", "查看文件", "", ""))
    executor, history = Mock(), History()
    execute_request(engine, executor, history, "查看文件", str(tmp_path), AUTO_SAFE)
    executor.execute.assert_not_called()
    assert history.records[0]["status"] == "preflight_failed"


def test_rejected_candidate_blocks_without_second_model_call(tmp_path):
    (tmp_path / "README.md").write_text("", encoding="utf-8")
    engine = _engine(TaskStep("cp readme .", "复制", "", ""))
    executor, history = Mock(), History()
    execute_request(engine, executor, history, "复制readme", str(tmp_path), AUTO_SAFE,
                    input_fn=lambda _: "n")
    assert engine.generate_task_plan.call_count == 1
    executor.execute.assert_not_called()
    assert history.records[0]["status"] == "preflight_failed"


def test_cat_candidate_is_reported_but_not_automatically_rewritten(tmp_path):
    (tmp_path / "README.md").write_text("", encoding="utf-8")
    (tmp_path / "README.txt").write_text("", encoding="utf-8")
    engine = _engine(TaskStep("cat readme", "查看", "", ""))
    executor, history = Mock(), History()
    execute_request(engine, executor, history, "查看readme", str(tmp_path), PREVIEW)
    assert engine.generate_task_plan.call_count == 1
    assert history.records[0]["status"] == "preflight_failed"


def test_existing_default_copy_name_uses_incremented_suffix(tmp_path):
    (tmp_path / "archive.tar.gz").write_text("", encoding="utf-8")
    (tmp_path / "archive_copy.tar.gz").write_text("", encoding="utf-8")
    engine = _engine(TaskStep("cp archive.tar.gz .", "复制", "", ""))
    executor, history = Mock(), History()
    execute_request(engine, executor, history, "复制压缩包", str(tmp_path), PREVIEW,
                    input_fn=lambda _: "")
    assert history.records[0]["preflight"]["corrected_commands"] == [
        "cp archive.tar.gz archive_copy_2.tar.gz"
    ]


def test_custom_outside_target_reaches_risk_decision(tmp_path):
    (tmp_path / "README.md").write_text("", encoding="utf-8")
    engine = _engine(TaskStep("cp README.md .", "复制", "", ""))
    executor, history = Mock(), History()
    execute_request(engine, executor, history, "复制README", str(tmp_path), PREVIEW,
                    input_fn=lambda _: "/home")
    record = history.records[0]
    assert record["preflight"]["selected_target"] == "/home"
    assert record["decisions"] == ["confirm"]


def test_move_does_not_offer_default_target(tmp_path):
    (tmp_path / "README.md").write_text("", encoding="utf-8")
    engine = _engine(TaskStep("mv README.md .", "移动", "", ""))
    executor, history, prompts = Mock(), History(), []

    def answer(prompt):
        prompts.append(prompt)
        return "moved.md"

    execute_request(engine, executor, history, "移动README", str(tmp_path), PREVIEW, input_fn=answer)
    assert all("回车使用" not in prompt for prompt in prompts)
    assert history.records[0]["preflight"]["corrected_commands"] == ["mv README.md moved.md"]


def test_multi_source_copy_requires_explicit_target(tmp_path):
    (tmp_path / "one.txt").write_text("", encoding="utf-8")
    (tmp_path / "two.txt").write_text("", encoding="utf-8")
    engine = _engine(TaskStep("cp one.txt two.txt .", "复制", "", ""))
    executor, history, prompts = Mock(), History(), []

    def answer(prompt):
        prompts.append(prompt)
        return "copies"

    execute_request(engine, executor, history, "复制两个文件", str(tmp_path), PREVIEW, input_fn=answer)
    assert all("回车使用" not in prompt for prompt in prompts)
    assert history.records[0]["preflight"]["corrected_commands"] == ["cp one.txt two.txt copies"]


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
