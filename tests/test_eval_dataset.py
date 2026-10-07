import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.task_plan import parse_operation_plan
from eval.run_eval import load_testcases, create_fixture, tree_state, check_result, run_eval, matches_expected, execution_allowed


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
    engine = Mock(plan_attempts=[], request_intent={}, intent_attempts=[])
    engine.generate_task_plan.return_value = TaskPlan((TaskStep(operation="create_file", parameters={"path": "unexpected"}),))
    monkeypatch.setattr(runner, "Engine", lambda **kwargs: engine)
    monkeypatch.setattr(runner, "execute_action", lambda *args, **kwargs: pytest.fail("错误计划不得执行"))
    report = run_eval(limit=1, execute_safe=True, output_dir=tmp_path)
    assert report["task_completion_rate"] == 0
    assert not report["details"][0]["executed"]


def test_equivalent_paths_match_but_outside_paths_fail(tmp_path):
    case = load_testcases()[0]
    plan = parse_operation_plan(json.dumps({"status": "ready", "steps": [{
        "operation": "find_files", "parameters": {"path": str(tmp_path), "recursive": False, "pattern": "*.py"}}]}))
    assert matches_expected(plan, case, tmp_path)
    plan = parse_operation_plan(json.dumps({"status": "ready", "steps": [{
        "operation": "find_files", "parameters": {"path": "../outside", "recursive": False, "pattern": "*.py"}}]}))
    with pytest.raises(ValueError, match="之外"):
        matches_expected(plan, case, tmp_path)


def test_extra_read_step_can_complete_task_without_matching_plan(tmp_path, monkeypatch):
    import eval.run_eval as runner
    case = next(case for case in load_testcases() if case["input"] == "备份 note.txt 到 backup")
    monkeypatch.setattr(runner, "load_testcases", lambda: [case])
    plan = parse_operation_plan(json.dumps({"status": "ready", "steps": [
        {"operation": "find_files", "parameters": {"path": ".", "recursive": False, "pattern": "note.txt"}},
        *case["expected_steps"]]}))
    engine = Mock(plan_attempts=[], request_intent={}, intent_attempts=[])
    engine.generate_task_plan.return_value = plan
    monkeypatch.setattr(runner, "Engine", lambda **kwargs: engine)
    report = run_eval(execute_safe=True, output_dir=tmp_path)
    assert report["planning_accuracy"] == 0
    assert report["task_completion_rate"] == 100
    extra_write = parse_operation_plan(json.dumps({"status": "ready", "steps": [
        {"operation": "create_file", "parameters": {"path": "unexpected"}}, *case["expected_steps"]]}))
    assert not execution_allowed(extra_write, case, tmp_path)


def test_current_layer_structure_flag_is_equivalent_but_recursive_scope_is_not(tmp_path):
    from core.operations import prepare_plan, execute_action
    case = next(case for case in load_testcases() if case["input"] == "把当前目录过去两周修改的文件备份到 archive")
    payload = {"status": "ready", "steps": json.loads(json.dumps(case["expected_steps"]))}
    payload["steps"][1]["parameters"]["preserve_structure"] = True
    plan = parse_operation_plan(json.dumps(payload))
    assert matches_expected(plan, case, tmp_path)
    create_fixture(tmp_path)
    before = tree_state(tmp_path)
    prepared = prepare_plan(plan, str(tmp_path))
    outcomes = [execute_action(prepared, action) for action in prepared["actions"]]
    assert check_result(case, outcomes, tmp_path, before)
    case = json.loads(json.dumps(case))
    case["expected_steps"][1]["parameters"]["preserve_structure"] = True
    assert matches_expected(plan, case, tmp_path)
    payload["steps"][0]["parameters"]["recursive"] = True
    assert not matches_expected(parse_operation_plan(json.dumps(payload)), case, tmp_path)
