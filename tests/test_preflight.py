from core.preflight import inspect_plan
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
