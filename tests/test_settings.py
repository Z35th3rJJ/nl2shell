import pytest

from core.settings import AppSettings, choose_runtime_settings, load_settings, save_runtime_settings


def test_settings_use_safe_defaults(monkeypatch):
    for key in ("AUTO_EXECUTE", "EXECUTION_POLICY", "DRY_RUN", "AGENT_MODE"):
        monkeypatch.delenv(key, raising=False)
    assert load_settings() == AppSettings(False, "manual", False, False)


def test_settings_reject_invalid_policy(monkeypatch):
    monkeypatch.setenv("EXECUTION_POLICY", "unsafe")
    with pytest.raises(ValueError):
        load_settings()


def test_menu_uses_existing_settings_when_direct_start_selected():
    current = AppSettings(False, "workspace", True, True)
    assert choose_runtime_settings(current, input_fn=lambda _: "1", output_fn=lambda _: None) == current


def test_menu_retries_invalid_input_and_saves_selected_settings(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("LOCAL_MODEL=qwen\n# keep this comment\nAGENT_MODE=false\n", encoding="utf-8")
    answers = iter(["bad", "2", "y", "2", "n", "n", "y"])
    messages = []
    selected = choose_runtime_settings(
        AppSettings(False, "manual", False, False),
        input_fn=lambda _: next(answers),
        output_fn=messages.append,
        env_path=env_path,
    )
    assert selected == AppSettings(False, "workspace", False, True)
    text = env_path.read_text(encoding="utf-8")
    assert "LOCAL_MODEL=qwen" in text
    assert "# keep this comment" in text
    assert "AGENT_MODE=true" in text
    assert "EXECUTION_POLICY=workspace" in text
    assert "DRY_RUN=false" in text
    assert "AUTO_EXECUTE=false" in text
    assert any("请输入 1、2 或 0" in message for message in messages)


def test_menu_does_not_write_when_save_is_declined(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("AGENT_MODE=false\n", encoding="utf-8")
    answers = iter(["2", "y", "", "", "", "n"])
    choose_runtime_settings(AppSettings(False, "manual", False, False), input_fn=lambda _: next(answers), output_fn=lambda _: None, env_path=env_path)
    assert env_path.read_text(encoding="utf-8") == "AGENT_MODE=false\n"


def test_save_does_not_create_missing_env(tmp_path):
    assert save_runtime_settings(AppSettings(False, "manual", False, False), tmp_path / ".env") is False


def test_menu_exits_cleanly_on_end_of_input():
    assert choose_runtime_settings(
        AppSettings(False, "manual", False, False),
        input_fn=lambda _: (_ for _ in ()).throw(EOFError),
        output_fn=lambda _: None,
    ) is None
