import json

from core.history import HistoryStore
from core.redaction import redact_messages, redact_text
from core.structured_log import log_event


def test_redacts_common_credentials_and_private_keys():
    text = (
        "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456 "
        "Authorization: Bearer abc.def.ghi\n"
        "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----"
    )
    result = redact_text(text)
    assert "abcdefghijklmnopqrstuvwxyz" not in result
    assert "abc.def.ghi" not in result
    assert "\nsecret\n" not in result


def test_model_messages_are_redacted_without_mutating_input():
    messages = [{"role": "user", "content": "DB_PASSWORD=hunter2"}]
    safe = redact_messages(messages)
    assert safe[0]["content"] == "DB_PASSWORD=<REDACTED>"
    assert messages[0]["content"] == "DB_PASSWORD=hunter2"


def test_history_never_writes_secret_plaintext(tmp_path):
    store = HistoryStore(tmp_path / "history.jsonl")
    store.append({"input": "TOKEN=very-secret-value", "status": "test"})
    raw = store.path.read_text(encoding="utf-8")
    assert "very-secret-value" not in raw
    assert json.loads(raw)["input"] == "TOKEN=<REDACTED>"


def test_structured_log_uses_same_redaction(tmp_path, monkeypatch):
    path = tmp_path / "runtime.jsonl"
    monkeypatch.setenv("NL2SHELL_LOG_JSON", str(path))
    log_event("test", detail="COOKIE=session-secret")
    assert "session-secret" not in path.read_text(encoding="utf-8")
