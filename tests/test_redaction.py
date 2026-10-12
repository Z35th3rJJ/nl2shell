import json

from core.history import HistoryStore
from core.redaction import redact_messages, redact_text
from core.structured_log import log_event


def test_redacts_common_credentials_and_private_keys():
    token = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"
    bearer = ".".join(["abc", "def", "ghi"])
    key = "-----BEGIN " + "PRIVATE KEY-----\nsecret\n-----END " + "PRIVATE KEY-----"
    text = f"OPENAI_API_KEY={token} Authorization: Bearer {bearer}\n{key}"
    result = redact_text(text)
    assert "abcdefghijklmnopqrstuvwxyz" not in result
    assert "abc.def.ghi" not in result
    assert "\nsecret\n" not in result


def test_model_messages_are_redacted_without_mutating_input():
    original = "=".join(["DB_PASSWORD", "hunter2"])
    messages = [{"role": "user", "content": original}]
    safe = redact_messages(messages)
    assert safe[0]["content"] == "DB_PASSWORD=<REDACTED>"
    assert messages[0]["content"] == original


def test_history_never_writes_secret_plaintext(tmp_path):
    store = HistoryStore(tmp_path / "history.jsonl")
    store.append({"input": "=".join(["TOKEN", "very-secret-value"]), "status": "test"})
    raw = store.path.read_text(encoding="utf-8")
    assert "very-secret-value" not in raw
    assert json.loads(raw)["input"] == "TOKEN=<REDACTED>"


def test_structured_log_uses_same_redaction(tmp_path, monkeypatch):
    path = tmp_path / "runtime.jsonl"
    monkeypatch.setenv("NL2SHELL_LOG_JSON", str(path))
    log_event("test", detail="=".join(["COOKIE", "session-secret"]))
    assert "session-secret" not in path.read_text(encoding="utf-8")
def test_quoted_secret_assignments_are_redacted():
    from core.redaction import redact_text
    for value in ["=".join(["TOKEN", "'private value'"]), "=".join(["PASSWORD", '"secret text"'])]:
        assert "private" not in redact_text(value)
        assert "secret text" not in redact_text(value)
        assert "<REDACTED>" in redact_text(value)


def test_fine_grained_github_token_is_redacted():
    value = "github_" + "pat_" + "a" * 40
    assert value not in redact_text(value)


def test_url_credentials_are_redacted():
    username, value = "test-user", "test-password"
    text = f"https://{username}:{value}@example.invalid/path"
    assert redact_text(text) == "https://<REDACTED>@example.invalid/path"
