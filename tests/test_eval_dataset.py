import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.task_plan import parse_operation_plan
from eval.run_eval import load_testcases, create_fixture, tree_state, check_result, run_eval


def test_dataset_has_unique_inputs_and_covers_model_roles():
    cases = load_testcases()
    assert len({case["input"] for case in cases}) == len(cases)
    assert len({case["id"] for case in cases}) == len(cases)
    assert {"条件理解", "多步规划", "必要追问", "范围拒绝"} <= {case["category"] for case in cases}
    for case in cases:
        if case["expected_status"] == "ready":
            parse_operation_plan(json.dumps({"status": "ready", "steps": case["expected_steps"]}))


def test_file_count_uses_current_layer_operation():
    case = next(case for case in load_testcases() if case["input"] == "统计当前目录下的 Python 文件")
    assert case["expected_steps"] == [{"operation": "count_files", "parameters": {
        "path": ".", "recursive": False, "pattern": "*.py"}}]


def test_result_checker_does_not_trust_success_labels(tmp_path):
    create_fixture(tmp_path)
    before = tree_state(tmp_path)
    case = next(case for case in load_testcases() if case["input"] == "创建空文件 empty.txt")
    assert not check_result(case, [{"status": "verified"}], tmp_path, before)


def expected_engine(monkeypatch):
    import eval.run_eval as runner
    cases = {case["input"]: case for case in load_testcases()}

    class Engine:
        def __init__(self, **kwargs):
            pass

        def generate_task_plan(self, text, cwd):
            case = cases[text]
            status = case["expected_status"]
            payload = {"status": status}
            if status == "ready":
                payload["steps"] = case["expected_steps"]
            elif status == "need_clarification":
                payload["clarification"] = "请补充关键条件"
            else:
                payload["reason"] = "暂不支持"
            return parse_operation_plan(json.dumps(payload))

    monkeypatch.setattr(runner, "Engine", Engine)
    from core.execution import ExecutionResult
    executor = Mock()
    executor.execute.return_value = ExecutionResult(0, "system info", "", 0.1)
    monkeypatch.setattr(runner, "BashExecutor", lambda: executor)


def test_all_fixture_expectations_run_without_real_model(tmp_path, monkeypatch):
    expected_engine(monkeypatch)
    report = run_eval(execute_safe=True, output_dir=tmp_path)
    assert report["planning_accuracy"] == 100
    assert report["task_completion_rate"] == 100
    assert report["necessary_clarification_rate"] == 100
    assert report["unsupported_refusal_rate"] == 100
    assert report["code_revision"]
    assert report["dataset_version"]


def test_unmeasured_completion_is_null_and_results_do_not_overwrite(tmp_path, monkeypatch):
    expected_engine(monkeypatch)
    first = run_eval(limit=1, output_dir=tmp_path)
    second = run_eval(limit=1, output_dir=tmp_path)
    assert first["task_completion_rate"] is None
    assert second["details"][0]["task_completed"] is None
    assert len(list(tmp_path.glob("eval_result_*.json"))) == 2


def test_mismatched_plan_cannot_execute_and_counts_as_incomplete(tmp_path, monkeypatch):
    import eval.run_eval as runner
    from core.task_plan import TaskPlan, TaskStep
    engine = Mock()
    engine.generate_task_plan.return_value = TaskPlan((TaskStep(operation="create_file", parameters={"path": "unexpected"}),))
    monkeypatch.setattr(runner, "Engine", lambda **kwargs: engine)
    monkeypatch.setattr(runner, "execute_action", lambda *args, **kwargs: pytest.fail("错误计划不得执行"))
    report = run_eval(limit=1, execute_safe=True, output_dir=tmp_path)
    assert report["task_completion_rate"] == 0
    assert not report["details"][0]["executed"]
