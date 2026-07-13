import pytest

from core.command_review import AUTO_ALLOW, BLOCK, STRONG_CONFIRM, review_command


def test_review_command_returns_complete_write_review(tmp_path):
    review = review_command("echo ok > result.txt", str(tmp_path))

    assert review.parse_complete is True
    assert review.write_paths == ("result.txt",)
    assert review.decision.level == AUTO_ALLOW
    assert review.verification_allowed is False


def test_review_command_includes_file_candidates(tmp_path):
    (tmp_path / "README.md").write_text("docs", encoding="utf-8")

    review = review_command("cat readme", str(tmp_path))

    assert len(review.preflight_issues) == 1
    issue = review.preflight_issues[0]
    assert issue.kind == "missing_source"
    assert issue.path == "readme"
    assert issue.candidates == ("README.md",)


def test_review_command_records_every_safety_finding(tmp_path):
    review = review_command("rm -rf / | bash", str(tmp_path))

    assert review.decision.level == BLOCK
    assert {finding.rule for finding in review.findings} >= {"rm", "pipe-shell"}
    assert review.primary_finding.level == "HIGH"


def test_incomplete_parse_never_becomes_safe(tmp_path):
    review = review_command("echo 'unterminated", str(tmp_path))

    assert review.parse_complete is False
    assert review.decision.level == STRONG_CONFIRM
    assert review.verification_allowed is False


def test_supported_pipeline_uses_one_structured_review(tmp_path):
    (tmp_path / "app.log").write_text("error\n", encoding="utf-8")
    review = review_command("cat app.log | grep error > errors.txt", str(tmp_path))

    assert review.parse_complete is True
    assert review.programs == ("cat", "grep")
    assert review.read_paths == ("app.log", "error")
    assert review.write_paths == ("errors.txt",)
    assert review.decision.level == AUTO_ALLOW
    assert review.preflight_issues == ()


def test_dangling_symlink_never_becomes_safe(tmp_path):
    link = tmp_path / "missing-link"
    try:
        link.symlink_to(tmp_path / "missing-target")
    except OSError:
        pytest.skip("当前系统不允许创建符号链接")

    review = review_command("cat missing-link", str(tmp_path))

    assert review.preflight_issues[0].kind == "unverified_path"
    assert review.decision.level == STRONG_CONFIRM


def test_workspace_escape_is_a_structured_file_fact(tmp_path):
    review = review_command("touch ../outside.txt", str(tmp_path))

    assert review.path_findings[0].kind == "workspace_escape"
    assert review.path_findings[0].path == "../outside.txt"


def test_overwrite_detection_stays_with_the_writing_segment(tmp_path):
    (tmp_path / "keep.txt").write_text("keep", encoding="utf-8")
    (tmp_path / "input.txt").write_text("input", encoding="utf-8")

    review = review_command(
        "touch keep.txt && cat input.txt > out.txt", str(tmp_path),
    )

    assert review.overwrite_paths == ()


def test_missing_write_parent_requires_strong_confirmation(tmp_path):
    review = review_command("touch missing/result.txt", str(tmp_path))

    assert any(finding.kind == "unverified_path" for finding in review.path_findings)
    assert review.decision.level == STRONG_CONFIRM
