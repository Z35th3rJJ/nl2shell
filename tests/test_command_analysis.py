from core.command_analysis import analyze_command, raise_risk


def test_existing_write_target_is_reported(tmp_path):
    target = tmp_path / "config.txt"
    target.write_text("old", encoding="utf-8")
    result = analyze_command("echo new > config.txt", str(tmp_path))
    assert result.overwrite_paths == (str(target.resolve()),)


def test_advisory_risk_cannot_lower_deterministic_risk():
    assert raise_risk("HIGH", "SAFE") == "HIGH"
    assert raise_risk("SAFE", "WARN") == "WARN"


def test_touch_existing_file_is_not_reported_as_content_overwrite(tmp_path):
    (tmp_path / "result.txt").write_text("keep", encoding="utf-8")
    assert analyze_command("touch result.txt", str(tmp_path)).overwrite_paths == ()
