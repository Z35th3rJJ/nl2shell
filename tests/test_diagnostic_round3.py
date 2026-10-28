"""第二轮审核反例：落盘证据、因果关系、预算及真实文件系统边界。"""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tracemalloc
from unittest.mock import Mock, patch

import pytest

from cli import execute_request
from core import runtime_diagnostics as diagnostics
from core.execution import BashExecutor, ExecutionResult
from core.llm import chat
from core.task_plan import TaskStep
from tests.test_diagnostic_fixes import query_engine, recording
from tests.test_runtime_diagnostics import History, plan, read_package, run_task


@pytest.mark.parametrize("raw", [
    json.dumps({"credential": 'prefix"ROUND3_SYNTHETIC_SECRET', "note": "keep normal text"}),
    'credential: "prefix\nROUND3_SYNTHETIC_SECRET"; keep normal text',
    json.dumps({"wrapped": json.dumps({"credential": 'prefix"ROUND3_SYNTHETIC_SECRET'}),
                "note": "keep normal text"}),
])
def test_escaped_and_multiline_credentials_never_reach_disk(recording, monkeypatch, raw):
    client = Mock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=raw))])
    monkeypatch.setattr("core.llm._get_client", lambda backend: (client, "fixture-model"))
    assert chat([{"role": "user", "content": "keep normal text"}]) == raw
    recording.finish("verified")
    data = b"\n".join(p.read_bytes() for p in recording.path.rglob("*") if p.is_file())
    assert b"ROUND3_SYNTHETIC_SECRET" not in data
    assert b"keep normal text" in data
    response = next(row for row in recording.events if row["stage"] == "model_response")
    assert recording.index[response["output_ref"]]["redaction"] == "applied"


def test_redaction_error_does_not_publish_applied_evidence(recording, monkeypatch):
    before = set(recording.index)
    monkeypatch.setattr(diagnostics, "_scrub_text", Mock(side_effect=ValueError("synthetic scrub failure")))
    assert recording.attempt(recording.evidence, "credential=ROUND3_SYNTHETIC_SECRET") is None
    assert set(recording.index) == before
    assert b"ROUND3_SYNTHETIC_SECRET" not in b"".join(p.read_bytes() for p in (recording.path / "evidence").iterdir())
    assert recording.recording_errors


def test_refusal_after_clarification_is_the_current_stop_decision(monkeypatch, tmp_path):
    replies = [{"status": "ready", "operations": ["find_files"]}] * 2 + [
        plan("find_files", path=".", recursive=False, modified_within_days=7),
        {"status": "unsupported", "reason_code": "network"},
        {"status": "unsupported", "reason_code": "network"}]
    status, package, executor, _, _ = run_task(
        monkeypatch, tmp_path, replies=replies, operation="find_files",
        request="查找当前目录最近修改的文件", answer="最近三天，并通过网络发送")
    failure = json.loads((package / "failure.json").read_bytes())
    assert status == "unsupported"
    assert failure["first_failure"]["stage"] == "condition_check"
    assert failure["terminal_failure"]["event_id"] != failure["first_failure"]["event_id"]
    assert failure["terminal_failure"]["error_code"] == "model_refusal"
    assert "网络" in failure["terminal_failure"]["reason"]
    assert failure["direct_error"]["event_id"] == failure["terminal_failure"]["event_id"]
    executor.execute_query.assert_not_called()


def test_process_start_exception_has_one_root_and_a_linked_outcome(tmp_path, diagnostic_storage_and_offline):
    engine = query_engine([TaskStep(operation="working_directory")])
    spawn = Mock(side_effect=FileNotFoundError(2, "synthetic missing binary"))
    def fail_start(*args, **kwargs):
        with patch("core.execution.subprocess.Popen", spawn):
            return BashExecutor(bash_path="synthetic-never-started")._execute_argv(["synthetic-never-started"], 1)
    executor = Mock()
    executor.execute_query.side_effect = fail_start
    assert execute_request(engine, executor, History(), "显示目录", str(tmp_path), "confirm",
                           input_fn=lambda _: "yes") == "execution_failed"
    package = next(diagnostic_storage_and_offline.iterdir())
    _, rows = read_package(package)
    roots = [row for row in rows if row["status"] == "failed"]
    assert len(roots) == 1 and roots[0]["stage"] == "execution_boundary"
    outcome = next(row for row in rows if row["stage"] == "execution" and row["status"] != "started")
    assert outcome["status"] == "propagated" and outcome["cause_event_id"] == roots[0]["event_id"]
    assert not any(row["error_code"] == "safety_rejected" for row in rows)
    failure = json.loads((package / "failure.json").read_bytes())
    assert failure["terminal_failure"]["event_id"] == roots[0]["event_id"]
    spawn.assert_called_once()


def test_third_step_is_blocked_by_the_second_step_stop(tmp_path, diagnostic_storage_and_offline):
    engine = query_engine([TaskStep(operation="working_directory")] * 3)
    executor = Mock()
    executor.execute_query.side_effect = [
        (ExecutionResult(0, str(tmp_path) + "\n", "", 0), ["/usr/bin/pwd", "-P"]),
        (ExecutionResult(2, "", "synthetic failure", 0), ["/usr/bin/pwd", "-P"])]
    assert execute_request(engine, executor, History(), "三步骤查询", str(tmp_path), "confirm",
                           input_fn=lambda _: "yes") == "execution_failed"
    _, rows = read_package(next(diagnostic_storage_and_offline.iterdir()))
    assert any(row["stage"] == "verification" and row["status"] == "succeeded" and row.get("step") == 1 for row in rows)
    stopped = next(row for row in rows if row["stage"] == "execution" and row["status"] == "failed" and row["step"] == 2)
    blocked = next(row for row in rows if row["stage"] == "execution" and row["status"] == "blocked" and row["step"] == 3)
    assert blocked["blocked_by"] == stopped["event_id"]
    assert any(row["stage"] == "verification" and row["status"] == "blocked" and row.get("step") == 3
               and row["blocked_by"] == blocked["event_id"] for row in rows)
    assert executor.execute_query.call_count == 2


@pytest.mark.parametrize("kind", ["key", "text", "many_values"])
def test_difference_keys_paths_and_values_share_a_bounded_budget(kind):
    if kind == "key":
        key = "k" * (4 * 1024 * 1024)
        actual, expected = {key: "x"}, {key: "y"}
    elif kind == "text":
        actual, expected = {"value": "汉" * (4 * 1024 * 1024)}, {"value": "different"}
    else:
        actual, expected = ["x" * 4000] * 128, ["y" * 4000] * 128
    tracemalloc.start()
    try:
        diff = diagnostics.differences(actual, expected)
        data = json.dumps(diff, ensure_ascii=False).encode("utf-8")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 3 * 1024 * 1024
    assert len(data) <= diagnostics.MAX_EVIDENCE_BYTES
    assert '"truncated": true' in data.decode("utf-8")


@pytest.mark.parametrize("kind", ["file_inside", "file_outside", "directory_inside",
                                  "directory_outside", "dangling", "forged", "unindexed"])
def test_real_evidence_links_and_untrusted_references(tmp_path, monkeypatch, kind):
    task = tmp_path / "task"
    evidence_dir = task / "evidence"
    evidence_dir.mkdir(parents=True)
    inside = task / "payloads"
    outside = tmp_path / "outside"
    inside.mkdir()
    outside.mkdir()
    data = b'{"fixture":"safe evidence"}'
    digest = hashlib.sha256(data).hexdigest()
    filename = digest + ".json"
    ref = "evidence/" + filename
    (inside / filename).write_bytes(data)
    (outside / filename).write_bytes(data)
    index = {ref: {"sha256": digest}}
    try:
        if kind.startswith("directory"):
            evidence_dir.rmdir()
            evidence_dir.symlink_to(inside if kind.endswith("inside") else outside, target_is_directory=True)
        elif kind in {"file_inside", "file_outside", "dangling"}:
            target = (inside if kind == "file_inside" else outside) / filename
            if kind == "dangling":
                target = outside / "missing.json"
            (task / ref).symlink_to(target)
        else:
            (task / ref).write_bytes(data)
            if kind == "forged":
                ref = "../outside/" + filename
                index[ref] = {"sha256": digest}
            else:
                index.clear()
    except OSError as error:
        if os.name == "nt":
            pytest.skip(f"真实链接测试需要 Ubuntu/WSL；Windows 创建链接失败：{error.winerror}")
        raise
    opened = []
    original = Path.open
    def track_open(path, *args, **kwargs):
        opened.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", track_open)
    if kind in {"file_inside", "directory_inside"}:
        assert diagnostics.read_evidence(task, index, ref) == {"fixture": "safe evidence"}
        assert opened and all(path.resolve().is_relative_to(task.resolve()) for path in opened)
    else:
        with pytest.raises((ValueError, FileNotFoundError)):
            diagnostics.read_evidence(task, index, ref)
        assert opened == []


def test_bash_availability_does_not_decode_unused_utf16_output(monkeypatch):
    raw = "wsl: 检测到代理配置\r\n".encode("utf-16-le")
    def probe(*args, **kwargs):
        if kwargs.get("text") or kwargs.get("encoding"):
            raw.decode("utf-8")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=raw)
    monkeypatch.setattr("core.execution.subprocess.run", probe)
    assert BashExecutor(bash_path="synthetic-bash").is_available() is True


@pytest.mark.parametrize("raw,expected", [
    ('{"note":"first", "cred\\u0065ntial" : "R4_SYNTHETIC_PAYLOAD_90647", "tail":2}',
     '{"note":"first", "cred\\u0065ntial" : "<REDACTED>", "tail":2}'),
    ('{"first":1, "credential.value":"R4_SYNTHETIC_PAYLOAD_90647", "last":["a","b"]}',
     '{"first":1, "credential.value":"<REDACTED>", "last":["a","b"]}'),
    ('{"outer":{"cred\\u0065ntial":"R4_SYNTHETIC_PAYLOAD_90647","note":"inside"},"tail":true}',
     '{"outer":{"cred\\u0065ntial":"<REDACTED>","note":"inside"},"tail":true}'),
    (json.dumps({"wrapped": '{"cred\\u0065ntial":"R4_SYNTHETIC_PAYLOAD_90647","note":"keep"}', "tail": 3}),
     json.dumps({"wrapped": '{"cred\\u0065ntial":"<REDACTED>","note":"keep"}', "tail": 3})),
    ('{"note":"keep\\nnormal", "cred\\u0065ntial":"before\\\"quote\\nR4_SYNTHETIC_PAYLOAD_90647", "tail":false}',
     '{"note":"keep\\nnormal", "cred\\u0065ntial":"<REDACTED>", "tail":false}'),
    ('{"credential.value":{"owner":"R4_SYNTHETIC_PAYLOAD_90647","nested":[1,{"x":2}]},"note":"keep"}',
     '{"credential.value":"<REDACTED>","note":"keep"}'),
    ('{"env":"R4_SYNTHETIC_PAYLOAD_90647","api.key":"R4_SYNTHETIC_PAYLOAD_90647","note":"keep"}',
     '{"env":"<REDACTED>","api.key":"<REDACTED>","note":"keep"}'),
])
def test_json_sensitive_field_names_are_redacted_in_written_bytes(recording, monkeypatch, raw, expected):
    client = Mock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=raw))])
    monkeypatch.setattr("core.llm._get_client", lambda backend: (client, "fixture-model"))
    assert chat([{"role": "user", "content": "synthetic field regression"}]) == raw
    recording.finish("verified")
    contents = b"\n".join(p.read_bytes() for p in recording.path.rglob("*") if p.is_file())
    assert b"R4_SYNTHETIC_PAYLOAD_90647" not in contents
    response = next(row for row in recording.events if row["stage"] == "model_response")
    saved = json.loads((recording.path / response["output_ref"]).read_bytes())
    assert saved["content"] == expected
    assert recording.index[response["output_ref"]]["redaction"] == "applied"
    assert not recording.recording_errors


def test_non_sensitive_json_keeps_exact_order_escapes_and_whitespace(recording, monkeypatch):
    raw = ' {\n  "z": "keep\\nnormal", "n\\u006fte": "quoted \\\"text\\\"", "a": [3, 2, 1]\n}\n'
    client = Mock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=raw))])
    monkeypatch.setattr("core.llm._get_client", lambda backend: (client, "fixture-model"))
    assert chat([{"role": "user", "content": "synthetic unchanged input"}]) == raw.strip()
    recording.finish("verified")
    response = next(row for row in recording.events if row["stage"] == "model_response")
    assert json.loads((recording.path / response["output_ref"]).read_bytes())["content"] == raw


@pytest.mark.parametrize("kind", ["field_budget", "invalid_key_escape", "container_depth"])
def test_unverifiable_json_response_is_not_published(recording, monkeypatch, kind):
    if kind == "field_budget":
        raw = '{' + ','.join(f'"n{i}":0' for i in range(diagnostics.MAX_ITEMS + 1)) + ',"cred\\u0065ntial":"R4_SYNTHETIC_PAYLOAD_90647"}'
    elif kind == "invalid_key_escape":
        raw = '{"cred\\uZZZZential":"R4_SYNTHETIC_PAYLOAD_90647"}'
    else:
        raw = '{"credential.value":' + '[' * (diagnostics.MAX_DEPTH + 1) + '"R4_SYNTHETIC_PAYLOAD_90647"' + ']' * (diagnostics.MAX_DEPTH + 1) + '}'
    client = Mock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=raw))])
    monkeypatch.setattr("core.llm._get_client", lambda backend: (client, "fixture-model"))
    assert chat([{"role": "user", "content": "synthetic budget input"}]) == raw
    recording.finish("verified")
    assert recording.recording_errors
    assert not any(row["stage"] == "model_response" for row in recording.events)
    assert b"R4_SYNTHETIC_PAYLOAD_90647" not in b"".join(
        p.read_bytes() for p in recording.path.rglob("*") if p.is_file())


def test_raw_model_scrub_failure_does_not_publish_sensitive_evidence(recording, monkeypatch):
    raw = '{"credential.value":"R4_SYNTHETIC_PAYLOAD_90647"}'
    original = diagnostics._scrub_text
    def fail_sensitive(text):
        if "R4_SYNTHETIC_PAYLOAD_90647" in text:
            raise ValueError("synthetic redaction validation failure")
        return original(text)
    monkeypatch.setattr(diagnostics, "_scrub_text", fail_sensitive)
    client = Mock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=raw))])
    monkeypatch.setattr("core.llm._get_client", lambda backend: (client, "fixture-model"))
    assert chat([{"role": "user", "content": "synthetic failure input"}]) == raw
    recording.finish("verified")
    assert recording.recording_errors
    assert not any(row["stage"] == "model_response" for row in recording.events)
    assert b"R4_SYNTHETIC_PAYLOAD_90647" not in b"".join(
        p.read_bytes() for p in recording.path.rglob("*") if p.is_file())


def test_new_clarification_is_terminal_while_first_failure_is_preserved(monkeypatch, tmp_path):
    question = "请明确找到文件后要执行什么操作"
    replies = [{"status": "ready", "operations": ["find_files"]}] * 2 + [
        plan("find_files", path=".", recursive=False, modified_within_days=7)] + [
        {"status": "need_clarification", "clarification": question}] * 3
    status, package, executor, history, _ = run_task(
        monkeypatch, tmp_path, replies=replies, operation="find_files",
        request="查找当前目录最近修改的文件", answer="最近三天，还要进一步处理")
    failure = json.loads((package / "failure.json").read_bytes())
    _, rows = read_package(package)
    assert status == "needs_clarification" and history.last_record["block_reason"] == question
    first, terminal = failure["first_failure"], failure["terminal_failure"]
    assert first["stage"] == "condition_check" and "时间范围" in first["reason"]
    assert first == next(row for row in rows if row["event_id"] == first["event_id"])
    assert terminal["event_id"] != first["event_id"]
    assert terminal["stage"] == "task_resolution" and terminal["error_code"] == "parameter_error"
    assert terminal["reason"] == question
    assert failure["direct_error"]["event_id"] == terminal["event_id"]
    assert all(row["blocked_by"] == terminal["event_id"] for row in rows if row["status"] == "blocked")
    assert not any(row["error_code"] in {"model_error", "safety_rejected"} for row in rows)
    executor.execute_query.assert_not_called()
    executor.execute.assert_not_called()
