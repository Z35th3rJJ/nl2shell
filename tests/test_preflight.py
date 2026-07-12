import shlex

from core.preflight import CommandEdit, apply_edits, default_copy_target, inspect_plan
from core.task_plan import TaskPlan, TaskStep


def _plan(command):
    return TaskPlan((TaskStep(command, "", "", ""),))


def test_missing_source_finds_unique_case_and_extension_candidate(tmp_path):
    (tmp_path / "README.md").write_text("docs", encoding="utf-8")
    issue = inspect_plan(_plan("cat readme"), str(tmp_path)).issues[0]
    assert issue.kind == "missing_source"
    assert issue.candidates == ("README.md",)


def test_multiple_candidates_are_all_reported(tmp_path):
    (tmp_path / "README.md").write_text("", encoding="utf-8")
    (tmp_path / "README.txt").write_text("", encoding="utf-8")
    issue = inspect_plan(_plan("cat readme"), str(tmp_path)).issues[0]
    assert issue.candidates == ("README.md", "README.txt")


def test_copy_requires_target_and_rejects_copying_to_itself(tmp_path):
    (tmp_path / "README.md").write_text("", encoding="utf-8")
    assert inspect_plan(_plan("cp README.md"), str(tmp_path)).issues[0].kind == "missing_target"
    assert inspect_plan(_plan("cp README.md ."), str(tmp_path)).issues[0].kind == "same_source_target"


def test_valid_copy_has_no_preflight_issues(tmp_path):
    (tmp_path / "README.md").write_text("", encoding="utf-8")
    assert inspect_plan(_plan("cp README.md README.backup.md"), str(tmp_path)).ok


def test_shell_glob_is_left_to_bash_and_safety_checks(tmp_path):
    assert inspect_plan(_plan("rm *.log"), str(tmp_path)).ok


def test_apply_edits_quotes_spaces_and_protects_leading_dash():
    plan = _plan("cp draft .")
    edited = apply_edits(plan, [
        CommandEdit(1, 1, "-my draft.md"),
        CommandEdit(1, 2, "backup copy.md"),
    ])
    assert shlex.split(edited.steps[0].command) == ["cp", "--", "-my draft.md", "backup copy.md"]


def test_default_copy_target_preserves_all_suffixes_and_avoids_overwrite(tmp_path):
    (tmp_path / "archive.tar.gz").write_text("", encoding="utf-8")
    assert default_copy_target("archive.tar.gz", str(tmp_path)) == "archive_copy.tar.gz"
    (tmp_path / "archive_copy.tar.gz").write_text("", encoding="utf-8")
    assert default_copy_target("archive.tar.gz", str(tmp_path)) == "archive_copy_2.tar.gz"


def test_directory_has_no_default_copy_target(tmp_path):
    (tmp_path / "docs").mkdir()
    assert default_copy_target("docs", str(tmp_path)) is None


def test_default_target_checks_collisions_in_current_directory(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "README.md").write_text("", encoding="utf-8")
    (tmp_path / "README_copy.md").write_text("", encoding="utf-8")
    assert default_copy_target("source/README.md", str(tmp_path)) == "README_copy_2.md"


def test_apply_edits_preserves_existing_options():
    plan = _plan("cp -p readme .")
    edited = apply_edits(plan, [CommandEdit(1, 2, "README.md"), CommandEdit(1, 3, "README_copy.md")])
    assert shlex.split(edited.steps[0].command) == ["cp", "-p", "README.md", "README_copy.md"]
