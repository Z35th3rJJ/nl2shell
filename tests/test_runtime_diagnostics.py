import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cli import execute_request, json_result
from core.engine import Engine
from core.execution import ExecutionResult
from core.runtime_diagnostics import DiagnosticTask, MAX_EVIDENCE_BYTES, current_task, differences, emit
from core import runtime_diagnostics


class History:
    def __init__(self):
        self.records = []

    def append(self, record):
        self.records.append(record)


def plan(operation="working_directory", **parameters):
    return {"status": "ready", "steps": [{"operation": operation, "parameters": parameters}]}


def run_task(monkeypatch, cwd, *, reference=None, replies=None, stdout=None, exit_code=0,
             request="显示当前工作目录", answer="yes", operation="working_directory", parameters=None):
    value = plan(operation, **(parameters or {}))
    outputs = iter(replies or [
        {"status": "ready", "operations": [operation]},
        {"status": "ready", "operations": [operation]}, value,
        reference if reference is not None else value,
        {"commands": [{"step": 1, "argv": ["pwd", "-P"], "explanation": "显示当前目录"}]},
    ])
    client = Mock()
    def respond(**kwargs):
        result = next(outputs)
        if isinstance(result, Exception):
            raise result
        raw = json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=raw))])
    client.chat.completions.create.side_effect = respond
    monkeypatch.setattr("core.llm._get_client", lambda backend: (client, "synthetic-model"))
    executor, history = Mock(), History()
    executor.execute_query.return_value = (ExecutionResult(exit_code, str(cwd) + "\n" if stdout is None else stdout,
                                                          "", 0.01), ["/usr/bin/pwd", "-P"])
    engine = Engine(ssh_hosts=[])
    engine.suggest_fix = Mock(return_value="请检查状态")
    root = runtime_diagnostics.default_diagnostics_root()
    before = set(root.glob("*"))
    status = execute_request(engine, executor, history, request, str(cwd), "confirm", input_fn=lambda _: answer)
    package = (set(root.glob("*")) - before).pop()
    return status, package, executor, history, client


def read_package(package):
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    trace = [json.loads(line) for line in (package / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    return manifest, trace


def evidence(package, event, field="output_ref"):
    return json.loads((package / event[field]).read_text(encoding="utf-8"))


def test_success_complete_order_and_public_contract(monkeypatch, tmp_path, diagnostic_storage_and_offline):
    status, package, executor, history, client = run_task(monkeypatch, tmp_path)
    assert status == "verified"
    manifest, trace = read_package(package)
    assert manifest["final_status"] == "verified" and not manifest["recording_errors"]
    assert manifest["code"]["git_sha"] and manifest["code"]["modules"]["core.engine"]["path"]
    assert manifest["runtime"]["python"] and manifest["runtime"]["dependencies"]["openai"]
    assert manifest["model"]["model"] == "synthetic-model"
    assert not (package / "failure.json").exists()
    assert [row["seq"] for row in trace] == list(range(1, len(trace) + 1))
    assert len({row["event_id"] for row in trace}) == len(trace)
    assert {row["task_id"] for row in trace} == {manifest["task_id"]} == {package.name}
    stages = [row["stage"] for row in trace]
    for name in ("request", "model_request", "model_response", "plan_parse", "parameter_check",
                 "independent_reference", "strict_comparison", "command_review", "confirmation", "execution", "verification"):
        assert name in stages
    assert stages.index("strict_comparison") < stages.index("confirmation") < stages.index("execution") < stages.index("verification")
    assert {row["status"] for row in trace if row["stage"] == "format_correction"} == {"skipped"}
    sent = next(row for row in trace if row["stage"] == "model_request")
    assert evidence(package, sent, "input_ref")["messages"] == client.chat.completions.create.call_args_list[0].kwargs["messages"]
    for row in trace:
        assert {"task_id", "event_id", "seq", "timestamp", "stage", "status", "input_ref", "output_ref", "error_code", "reason"} <= row.keys()
        for field in ("input_ref", "output_ref"):
            if row[field]:
                entry = manifest["evidence_index"][row[field]]
                assert hashlib.sha256((package / row[field]).read_bytes()).hexdigest() == entry["sha256"]
    assert executor.execute_query.call_count == 1
    assert "task_id" not in history.records[-1]
    assert set(json_result(history.last_record, status)) == {"status", "risk_level", "steps", "verification", "duration_seconds", "error"}


def test_reference_mismatch_is_first_failure_and_blocks_execution(monkeypatch, tmp_path):
    status, package, executor, _, _ = run_task(monkeypatch, tmp_path,
                                            reference=plan("list_directory", path=".", include_hidden=False))
    assert status == "blocked"
    failure = json.loads((package / "failure.json").read_text(encoding="utf-8"))
    assert failure["first_failure"]["stage"] == "strict_comparison"
    assert failure["direct_error"]["error_code"] == "reference_mismatch"
    diff = evidence(package, failure["first_failure"])["diff"]
    assert diff and any(item["actual"] != item["expected"] for item in diff)
    _, trace = read_package(package)
    correction = next(row for row in trace if row["stage"] == "format_correction")
    assert correction["status"] == "skipped"
    assert "format_correction" not in {item["stage"] for item in failure["blocked_stages"]}
    assert failure["actual_result"]["event_ref"] == trace[-1]["event_id"]
    for name in ("confirmation", "execution", "verification"):
        row = next(row for row in trace if row["stage"] == name)
        assert row["status"] == "blocked" and row["reason"] and row["blocked_by"]
    executor.execute_query.assert_not_called()
    executor.execute.assert_not_called()


@pytest.mark.parametrize("bad", ["not JSON", '{"status":"ready","steps":[{"operation":"read_text","parameters":{"path":"a"}}]}'])
def test_first_parse_or_parameter_error_survives_retry(monkeypatch, tmp_path, bad):
    replies = [{"status": "ready", "operations": ["read_text"]}] * 2 + [bad, bad]
    status, package, executor, _, _ = run_task(monkeypatch, tmp_path, replies=replies)
    failure = json.loads((package / "failure.json").read_text(encoding="utf-8"))
    assert status == "plan_failed"
    assert failure["first_failure"]["stage"] == ("plan_parse" if bad == "not JSON" else "parameter_check")
    assert read_package(package)[0]["format_corrections"] == 1
    _, trace = read_package(package)
    correction = next(row for row in trace if row["stage"] == "format_correction")
    assert any(row.get("format_correction_event_id") == correction["event_id"] for row in trace if row["stage"] == "model_request")
    executor.execute_query.assert_not_called()


def test_safety_rejection_stops_and_cancellation_is_distinct(monkeypatch, tmp_path):
    outputs = [{"status": "ready", "operations": ["working_directory"]}] * 2 + [plan()] * 2 + [
        {"commands": [{"step": 1, "argv": ["rm", "-rf", "/"], "explanation": "不可信候选"}]}]
    status, package, executor, _, _ = run_task(monkeypatch, tmp_path, replies=outputs)
    assert status == "blocked"
    manifest, trace = read_package(package)
    assert any(row["stage"] == "command_review" and row["error_code"] == "safety_rejected" for row in trace)
    executor.execute_query.assert_not_called()
    status, package, executor, _, _ = run_task(monkeypatch, tmp_path, answer="no")
    assert status == "cancelled"
    failure = json.loads((package / "failure.json").read_text(encoding="utf-8"))
    assert failure["direct_error"]["error_code"] == "user_cancelled"
    executor.execute_query.assert_not_called()


@pytest.mark.parametrize("exit_code,stdout,stage,code", [
    (2, "", "execution", "execution_failed"), (0, "wrong path\n", "verification", "verification_failed"),
])
def test_execution_and_verification_are_distinct(monkeypatch, tmp_path, exit_code, stdout, stage, code):
    status, package, _, _, _ = run_task(monkeypatch, tmp_path, exit_code=exit_code, stdout=stdout)
    failure = json.loads((package / "failure.json").read_text(encoding="utf-8"))
    assert failure["first_failure"]["stage"] == stage
    assert failure["direct_error"]["error_code"] == code
    assert status == ("execution_failed" if exit_code else "unverified")


def test_redaction_deduplication_and_bounded_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "opaque-fixture-key-value")
    task = DiagnosticTask()
    token = current_task.set(task)
    try:
        task.start({"request": "API_KEY=your_fixture_secret", "fixture": {"expected": "redacted"}}, Engine(ssh_hosts=[]), {})
        payload = {"nested": {"access_token": "test-key", "Authorization": "Bearer test-placeholder"},
                   "text": "opaque-fixture-key-value", "environment": {"PATH": "hidden"}}
        first = emit("fixture", "succeeded", output=payload)
        emit("fixture", "succeeded", output=payload)
        emit("large", "succeeded", output={"stdout": "汉" * MAX_EVIDENCE_BYTES})
        task.finish("verified")
    finally:
        current_task.reset(token)
    manifest, trace = read_package(task.path)
    rows = [row for row in trace if row["stage"] == "fixture"]
    assert rows[0]["output_ref"] == rows[1]["output_ref"]
    assert manifest["evidence_index"][rows[0]["output_ref"]]["omitted_fields"]
    contents = "\n".join(file.read_text(encoding="utf-8") for file in task.path.rglob("*") if file.is_file())
    for value in ("your_fixture_secret", "test-key", "Bearer test-placeholder", "opaque-fixture-key-value", '"PATH": "hidden"'):
        assert value not in contents
    large = next(row for row in trace if row["stage"] == "large")
    large_ref = evidence(task.path, large)["stdout"]["evidence_ref"]
    assert json.loads((task.path / large_ref).read_text(encoding="utf-8"))["truncated"]
    assert manifest["evidence_index"][large_ref]["truncated"]
    assert (task.path / large_ref).stat().st_size <= MAX_EVIDENCE_BYTES


def test_logging_failure_never_bypasses_safety_or_audit(monkeypatch, tmp_path):
    monkeypatch.setattr(DiagnosticTask, "event", Mock(side_effect=OSError("full")))
    status, package, executor, history, _ = run_task(monkeypatch, tmp_path, reference=plan("system_info", query="system"))
    assert status == "blocked" and not history.last_record["executed"]
    executor.execute_query.assert_not_called()
    # 诊断失效时，原审计的失败仍然阻止文件写入。
    from core.task_plan import TaskPlan, TaskStep
    engine = Mock()
    engine.generate_task_plan.return_value = TaskPlan((TaskStep(operation="create_file", parameters={"path": "never"}),))
    history = Mock()
    history.append.side_effect = PermissionError("audit unavailable")
    assert execute_request(engine, Mock(), history, "创建 never", str(tmp_path), "confirm", input_fn=lambda _: "yes") == "audit_failed"
    assert not (tmp_path / "never").exists()


def test_differences_report_missing_nested_fields():
    assert differences({"p": {"recursive": False}}, {"p": {"recursive": True, "path": "."}}) == [
        {"path": "$.p.path", "actual": "<MISSING>", "expected": "."},
        {"path": "$.p.recursive", "actual": False, "expected": True}]


def test_model_call_error_has_actual_request_and_no_execution(monkeypatch, tmp_path):
    status, package, executor, _, _ = run_task(monkeypatch, tmp_path, replies=[RuntimeError("fixture model unavailable")])
    assert status == "plan_failed"
    failure = json.loads((package / "failure.json").read_text(encoding="utf-8"))
    assert failure["first_failure"]["stage"] == "model_call"
    assert failure["direct_error"]["error_code"] == "model_error"
    assert any(row["stage"] == "model_request" for row in read_package(package)[1])
    assert "RuntimeError" == evidence(package, failure["first_failure"])["exception"]
    executor.execute_query.assert_not_called()


def test_partial_evidence_failure_is_reported_without_changing_success(monkeypatch, tmp_path):
    original = DiagnosticTask.evidence
    calls = []
    def fail_once(self, value):
        calls.append(value)
        if len(calls) == 3:
            raise OSError("fixture disk unavailable")
        return original(self, value)
    monkeypatch.setattr(DiagnosticTask, "evidence", fail_once)
    status, package, executor, _, _ = run_task(monkeypatch, tmp_path)
    assert status == "verified" and executor.execute_query.call_count == 1
    assert read_package(package)[0]["recording_errors"]


def test_diagnostic_initialization_failure_does_not_bypass_rejection(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_diagnostics, "default_diagnostics_root", Mock(side_effect=OSError("unavailable")))
    engine, executor, history = Mock(), Mock(), History()
    from core.task_plan import TaskPlan
    engine.generate_task_plan.return_value = TaskPlan((), refused=True, reason="不支持的请求")
    assert execute_request(engine, executor, history, "fixture request", str(tmp_path), "confirm") == "unsupported"
    executor.execute_query.assert_not_called()


def test_python_tool_records_check_in_tool_and_blocks_later_steps(monkeypatch, tmp_path, diagnostic_storage_and_offline):
    from core.task_plan import TaskPlan, TaskStep
    engine = Mock()
    engine.generate_task_plan.return_value = TaskPlan((
        TaskStep(operation="create_file", parameters={"path": "first"}),
        TaskStep(operation="create_file", parameters={"path": "later"})))
    engine.suggest_fix.return_value = "请检查状态"
    original = Path.open
    def fail_create(self, *args, **kwargs):
        if self.name == "first" and args and args[0] == "xb":
            raise OSError("fixture disk full")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", fail_create)
    status = execute_request(engine, Mock(), History(), "创建两个文件", str(tmp_path), "confirm", input_fn=lambda _: "yes")
    assert status == "execution_failed" and not (tmp_path / "later").exists()
    package = next(diagnostic_storage_and_offline.glob("*"))
    _, trace = read_package(package)
    assert any(row["stage"] == "execution" and row["status"] == "blocked" and row.get("step") == 2 for row in trace)
    assert all(row["status"] == "skipped" for row in trace if row["stage"] == "verification" and row.get("step") != 2)
    assert any(row["stage"] == "verification" and row["status"] == "blocked" and row.get("step") == 2 for row in trace)


def test_agent_steps_share_one_task_and_final_verification(monkeypatch, tmp_path, diagnostic_storage_and_offline):
    from core.agent import execute_agent
    rule = {"id": "a1", "when": [], "step": {"operation": "working_directory", "parameters": {}}}
    engine = Mock(spec=Engine)
    engine.agent_contract.return_value = {"status": "ready", "rules": [rule]}
    engine.agent_decide.side_effect = [{"status": "act", "rule": "a1", "step": rule["step"], "basis": []},
                                       {"status": "finish", "basis": ["a1"]}]
    engine.agent_commands.return_value = {"commands": [{"step": 1, "argv": ["pwd", "-P"], "explanation": "查询"}]}
    engine.command_attempts = []
    executor = Mock()
    executor.execute_query.return_value = (ExecutionResult(0, str(tmp_path) + "\n", "", 0), ["/usr/bin/pwd", "-P"])
    assert execute_agent(engine, executor, History(), "显示工作目录", str(tmp_path), "confirm",
                         input_fn=lambda _: "yes") == "verified"
    packages = list(diagnostic_storage_and_offline.glob("*"))
    assert len(packages) == 1
    _, trace = read_package(packages[0])
    assert len({row["task_id"] for row in trace}) == 1
    assert any(row["stage"] == "final_verification" and row["status"] == "succeeded" for row in trace)
