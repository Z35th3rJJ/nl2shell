"""审核反例：固定模型/执行器，检查真实落盘诊断和原有任务结果。"""
import json
from pathlib import Path
from types import SimpleNamespace
import tracemalloc
from unittest.mock import Mock

import pytest

from cli import execute_request
from core import runtime_diagnostics as diagnostics
from core.agent import execute_agent
from core.engine import Engine
from core.execution import ExecutionResult
from core.llm import chat
from core.task_plan import TaskPlan, TaskStep, parse_operation_plan
from tests.test_runtime_diagnostics import History, evidence, plan, read_package, run_task


@pytest.fixture
def recording():
    task = diagnostics.DiagnosticTask()
    token = diagnostics.current_task.set(task)
    task.start({"user_input": "合成诊断回归"}, Engine(ssh_hosts=[]), {})
    try:
        yield task
    finally:
        diagnostics.current_task.reset(token)


@pytest.mark.parametrize("raw", [
    '{"credential":"your_fixture_secret","note":"keep normal text"}',
    'Authorization: Basic your_fixture_secret\nkeep normal text',
    json.dumps({"wrapped": json.dumps({"api_key": "your_fixture_secret"}), "note": "keep normal text"}),
    'credential=your_fixture_secret; keep normal text',
])
def test_raw_model_secrets_are_absent_from_written_bytes(monkeypatch, recording, raw):
    client = Mock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=raw))])
    monkeypatch.setattr("core.llm._get_client", lambda backend: (client, "fixture-model"))
    assert chat([{"role": "user", "content": "keep normal text"}]) == raw
    recording.finish("verified")
    data = b"\n".join(path.read_bytes() for path in recording.path.rglob("*") if path.is_file())
    assert b"your_fixture_secret" not in data
    assert b"keep normal text" in data
    manifest, _ = read_package(recording.path)
    assert any(meta["omitted_fields"] for meta in manifest["evidence_index"].values())


def test_recovered_parse_is_historical_and_later_rejection_causes_stop(monkeypatch, tmp_path):
    replies = [{"status": "ready", "operations": ["working_directory"]}] * 2 + [
        "not JSON", plan(), plan(),
        {"commands": [{"step": 1, "argv": ["rm", "-rf", "/"], "explanation": "不可信候选"}]}]
    status, package, executor, _, _ = run_task(monkeypatch, tmp_path, replies=replies)
    failure = json.loads((package / "failure.json").read_text(encoding="utf-8"))
    _, rows = read_package(package)
    assert status == "blocked"
    assert failure["first_failure"]["error_code"] == "parse_error"
    assert failure["terminal_failure"]["error_code"] == "safety_rejected"
    assert failure["direct_error"]["event_id"] == failure["terminal_failure"]["event_id"]
    execution = next(row for row in rows if row["stage"] == "execution")
    assert execution["blocked_by"] == failure["terminal_failure"]["event_id"]
    executor.execute_query.assert_not_called()


def test_new_exception_after_recovery_is_not_false_propagation(recording):
    @diagnostics.observe("fixture_generation", "model_error")
    def generate():
        try:
            parse_operation_plan("not JSON")
        except ValueError:
            pass
        raise RuntimeError("new independent model failure")
    with pytest.raises(RuntimeError):
        generate()
    recording.finish("blocked")
    _, rows = read_package(recording.path)
    error = next(row for row in rows if row["stage"] == "fixture_generation" and row["status"] != "started")
    assert error["status"] == "failed"
    assert error["error_code"] == "model_error"
    assert not error.get("cause_event_id")


def test_agent_model_error_does_not_invent_safety_rejection(monkeypatch, tmp_path, diagnostic_storage_and_offline):
    client = Mock()
    client.chat.completions.create.side_effect = RuntimeError("fixture model unavailable")
    monkeypatch.setattr("core.llm._get_client", lambda backend: (client, "fixture-model"))
    executor = Mock()
    assert execute_agent(Engine(ssh_hosts=[]), executor, History(), "显示当前目录", str(tmp_path), "confirm") == "blocked"
    package = next(diagnostic_storage_and_offline.iterdir())
    failure = json.loads((package / "failure.json").read_text(encoding="utf-8"))
    _, rows = read_package(package)
    assert failure["direct_error"]["error_code"] == "model_error"
    assert not any(row["error_code"] == "safety_rejected" for row in rows)
    assert len([row for row in rows if row["status"] == "failed"]) == 1
    executor.execute_query.assert_not_called()


def test_exhausted_intent_parse_retains_parse_classification(monkeypatch, tmp_path):
    status, package, _, _, _ = run_task(monkeypatch, tmp_path, replies=["not JSON", "still not JSON"])
    failure = json.loads((package / "failure.json").read_text(encoding="utf-8"))
    assert status == "plan_failed"
    assert failure["terminal_failure"]["error_code"] == "parse_error"


def test_corrected_parse_then_valid_refusal_has_refusal_as_stop_cause(monkeypatch, tmp_path):
    replies = [{"status": "ready", "operations": ["working_directory"]}] * 2 + [
        "not JSON", {"status": "unsupported", "reason": "fixture valid refusal"}]
    status, package, executor, _, _ = run_task(monkeypatch, tmp_path, replies=replies)
    failure = json.loads((package / "failure.json").read_text(encoding="utf-8"))
    assert status == "unsupported"
    assert failure["first_failure"]["error_code"] == "parse_error"
    assert failure["direct_error"]["error_code"] == "model_refusal"
    executor.execute_query.assert_not_called()


def test_advice_error_does_not_replace_execution_failure(monkeypatch, tmp_path, diagnostic_storage_and_offline):
    client = Mock()
    client.chat.completions.create.side_effect = RuntimeError("fixture advice model failure")
    monkeypatch.setattr("core.llm._get_client", lambda backend: (client, "fixture-model"))
    engine = query_engine([TaskStep(operation="working_directory")])
    engine.suggest_fix.side_effect = Engine(ssh_hosts=[]).suggest_fix
    executor = Mock()
    executor.execute_query.return_value = (ExecutionResult(2, "", "fixture process failure", 0), ["/usr/bin/pwd", "-P"])
    assert execute_request(engine, executor, History(), "显示目录", str(tmp_path), "confirm", input_fn=lambda _: "yes") == "execution_failed"
    package = next(diagnostic_storage_and_offline.iterdir())
    failure = json.loads((package / "failure.json").read_text(encoding="utf-8"))
    _, rows = read_package(package)
    assert any(row["error_code"] == "model_error" and row.get("secondary") for row in rows)
    assert failure["direct_error"]["error_code"] == "execution_failed"


def test_reference_success_then_bad_candidate_is_candidate_parse_failure(monkeypatch, tmp_path):
    replies = [{"status": "ready", "operations": ["working_directory"]}] * 2 + [plan(), plan(), "not JSON"]
    status, package, executor, _, _ = run_task(monkeypatch, tmp_path, replies=replies)
    _, rows = read_package(package)
    failure = json.loads((package / "failure.json").read_text(encoding="utf-8"))
    assert status == "blocked"
    assert any(row["stage"] == "independent_reference" and row["status"] == "succeeded" for row in rows)
    assert any(row["stage"] == "strict_comparison" and row["status"] == "succeeded" for row in rows)
    assert failure["terminal_failure"]["stage"] == "candidate_parse"
    executor.execute_query.assert_not_called()


def query_engine(steps):
    engine = Mock(spec=Engine)
    engine.generate_task_plan.return_value = TaskPlan(tuple(steps))
    engine.generate_bash_commands.side_effect = lambda request, cwd, plan, prepared, answers: {
        "commands": [{"step": action["step"], "argv": action["_required_argv"], "explanation": "合成查询"}
                     for action in prepared["actions"]]}
    engine.command_attempts = []
    engine.suggest_fix.return_value = "请检查状态"
    return engine


@pytest.mark.parametrize("stderr,truncated,reason", [("fixture warning", False, "stderr"), ("", True, "truncated")])
def test_verification_gate_failure_is_recorded_with_actual_reason(tmp_path, diagnostic_storage_and_offline, stderr, truncated, reason):
    engine = query_engine([TaskStep(operation="working_directory")])
    executor = Mock()
    executor.execute_query.return_value = (ExecutionResult(0, str(tmp_path) + "\n", stderr, 0, output_truncated=truncated), ["/usr/bin/pwd", "-P"])
    assert execute_request(engine, executor, History(), "显示目录", str(tmp_path), "confirm", input_fn=lambda _: "yes") == "unverified"
    package = next(diagnostic_storage_and_offline.iterdir())
    _, rows = read_package(package)
    failure = json.loads((package / "failure.json").read_text(encoding="utf-8"))
    assert failure["terminal_failure"]["stage"] == "verification_gate"
    gate = failure["terminal_failure"]
    assert reason in gate["reason"]
    blocked = next(row for row in rows if row["stage"] == "verification" and row["status"] == "blocked")
    assert blocked["step"] == 1 and blocked["blocked_by"] == gate["event_id"]


def test_second_step_failure_has_its_own_blocked_verification(tmp_path, diagnostic_storage_and_offline):
    engine = query_engine([TaskStep(operation="working_directory"), TaskStep(operation="working_directory")])
    executor = Mock()
    executor.execute_query.side_effect = [
        (ExecutionResult(0, str(tmp_path) + "\n", "", 0), ["/usr/bin/pwd", "-P"]),
        (ExecutionResult(2, "", "fixture execution failure", 0), ["/usr/bin/pwd", "-P"])]
    assert execute_request(engine, executor, History(), "合成两步查询", str(tmp_path), "confirm", input_fn=lambda _: "yes") == "execution_failed"
    _, rows = read_package(next(diagnostic_storage_and_offline.iterdir()))
    assert any(row["stage"] == "verification" and row["status"] == "succeeded" and row.get("step") == 1 for row in rows)
    assert any(row["stage"] == "verification" and row["status"] == "blocked" and row.get("step") == 2 and row.get("blocked_by") for row in rows)
    assert executor.execute_query.call_count == 2


def test_untrusted_reference_is_not_promoted_and_indexed_reference_can_be_read(recording):
    read = getattr(diagnostics, "read_evidence", None)
    assert callable(read), "必须提供检查索引和目录边界的证据读取入口"
    ref = recording.evidence({"reason": {"evidence_ref": "../../outside.json"}, "note": "keep"})
    stored = read(recording.path, recording.index, ref)
    assert "evidence_ref" not in stored["reason"]
    assert stored["reason"]["untrusted_evidence_ref"] == "../../outside.json"


@pytest.mark.parametrize("reference", ["../../outside.json", "/outside.json", "evidence/" + "0" * 64 + ".json"])
def test_reader_rejects_traversal_absolute_and_unindexed_references(recording, reference):
    read = getattr(diagnostics, "read_evidence", None)
    assert callable(read)
    with pytest.raises(ValueError):
        read(recording.path, recording.index, reference)


def test_reader_rejects_resolved_link_outside_task(recording, monkeypatch, tmp_path):
    read = getattr(diagnostics, "read_evidence", None)
    assert callable(read)
    ref = recording.evidence({"safe": True})
    original = Path.resolve
    target = recording.path / ref
    # 在文件系统解析边界模拟目录外链接，不依赖 Windows 的创建链接权限。
    monkeypatch.setattr(Path, "resolve", lambda path, *a, **k: tmp_path / "outside.json" if path == target else original(path, *a, **k))
    with pytest.raises(ValueError):
        read(recording.path, recording.index, ref)


def test_reader_rejects_modified_indexed_bytes(recording):
    read = getattr(diagnostics, "read_evidence", None)
    assert callable(read)
    ref = recording.evidence({"safe": True})
    (recording.path / ref).write_bytes(b'{"safe":false}')
    with pytest.raises(ValueError):
        read(recording.path, recording.index, ref)


@pytest.mark.parametrize("kind", ["text", "collection", "dataclass"])
def test_large_inputs_have_bounded_processing_and_marked_truncation(recording, kind):
    value = {"stdout": "汉" * (4 * 1024 * 1024)} if kind == "text" else {"values": ["normal text"] * 100000}
    if kind == "dataclass":
        value = TaskStep(operation="copy_files", parameters={"sources": ["normal text"] * 100000})
    tracemalloc.start()
    try:
        ref = recording.evidence(value)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 3 * 1024 * 1024
    assert recording.index[ref]["truncated"]
    assert all((recording.path / name).stat().st_size <= diagnostics.MAX_EVIDENCE_BYTES for name in recording.index)
    assert recording.index[ref]["content_sha256_scope"] == "bounded_redacted_content"


def test_large_comparison_differences_are_bounded():
    actual = ["extra"] * 100000
    tracemalloc.start()
    try:
        diff = diagnostics.differences(actual, [])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 3 * 1024 * 1024
    assert len(diff) <= diagnostics.MAX_NODES + 1
    assert any(item.get("truncated") for item in diff)


@pytest.mark.parametrize("requested,expected", [(False, 0), (True, 1)])
def test_format_correction_counts_only_actual_requests(recording, requested, expected):
    diagnostics.emit("format_correction", "requested" if requested else "skipped", reason="合成计数夹具")
    recording.finish("verified")
    manifest, _ = read_package(recording.path)
    assert manifest["format_corrections"] == expected
