import pytest

from core.settings import (
    AUTO_SAFE, CONFIRM_MODE, PREVIEW, AppSettings, choose_mode,
    first_run_setup, load_settings, save_settings,
)


def _clear_settings(monkeypatch):
    for key in ("RUN_MODE", "SETUP_COMPLETE", "DRY_RUN", "AUTO_EXECUTE"):
        monkeypatch.delenv(key, raising=False)


def test_settings_default_to_confirm(monkeypatch):
    _clear_settings(monkeypatch)
    assert load_settings() == AppSettings(CONFIRM_MODE, False)


@pytest.mark.parametrize("legacy_key, expected", [("DRY_RUN", PREVIEW), ("AUTO_EXECUTE", AUTO_SAFE)])
def test_legacy_settings_are_migrated(monkeypatch, legacy_key, expected):
    _clear_settings(monkeypatch)
    monkeypatch.setenv(legacy_key, "true")
    assert load_settings().run_mode == expected


def test_invalid_run_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("RUN_MODE", "unsafe")
    with pytest.raises(ValueError):
        load_settings()


def test_choose_mode_retries_invalid_input():
    answers = iter(["bad", "3"])
    messages = []
    assert choose_mode(CONFIRM_MODE, lambda _: next(answers), messages.append) == AUTO_SAFE
    assert any("请输入 1、2、3 或 0" in message for message in messages)


def test_first_run_setup_saves_mode_and_preserves_unrelated_env(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "LOCAL_MODEL=qwen\nDEEPSEEK_API_KEY=secret\n# keep comment\n"
        "AGENT_MODE=true\nEXECUTION_POLICY=workspace\nDRY_RUN=true\nAUTO_EXECUTE=false\n",
        encoding="utf-8",
    )
    selected = first_run_setup(
        AppSettings(PREVIEW, False), input_fn=lambda _: "2",
        output_fn=lambda _: None, env_path=env_path,
    )
    assert selected == AppSettings(CONFIRM_MODE, True)
    text = env_path.read_text(encoding="utf-8")
    assert "LOCAL_MODEL=qwen" in text
    assert "DEEPSEEK_API_KEY=secret" in text
    assert "# keep comment" in text
    assert "RUN_MODE=confirm" in text
    assert "SETUP_COMPLETE=true" in text
    assert "AGENT_MODE" not in text
    assert "EXECUTION_POLICY" not in text
    assert "DRY_RUN" not in text
    assert "AUTO_EXECUTE" not in text


def test_completed_setup_skips_wizard():
    current = AppSettings(AUTO_SAFE, True)
    assert first_run_setup(current, input_fn=lambda _: pytest.fail("不应询问")) == current


def test_save_does_not_create_missing_env(tmp_path):
    assert save_settings(AppSettings(CONFIRM_MODE, True), tmp_path / ".env") is False
