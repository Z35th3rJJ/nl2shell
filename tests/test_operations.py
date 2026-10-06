import json
import os
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.operations import execute_action, prepare_plan, preparation_changed, trash_entries
from core.task_plan import TaskPlan, TaskStep, parse_operation_plan


def step(operation, **parameters):
    return TaskStep(operation=operation, parameters=parameters)


def run_plan(root, *steps):
    prepared = prepare_plan(TaskPlan(steps), str(root))
    outcomes = [execute_action(prepared, action) for action in prepared["actions"]]
    assert all(outcome["status"] == "verified" for outcome in outcomes), outcomes
    return outcomes


@pytest.mark.parametrize("raw", ["[]", "null", "42", '"ls"', "ls", '{"steps":[{"command":"rm x"}]}'])
def test_primary_parser_never_accepts_shell_or_non_object(raw):
    with pytest.raises(ValueError):
        parse_operation_plan(raw)


@pytest.mark.parametrize("parameters", [
    {"path": "."}, {"path": ".", "recursive": "false"},
    {"path": ".", "recursive": False, "size_gt_bytes": -1},
    {"path": ".", "recursive": False, "modified_within_days": float("inf")},
    {"path": ".", "recursive": False, "command": "ls"},
])
def test_primary_parser_validates_parameter_types_and_fields(parameters):
    with pytest.raises(ValueError):
        parse_operation_plan(json.dumps({"status": "ready", "steps": [
            {"operation": "find_files", "parameters": parameters}]}))


@pytest.mark.parametrize("reference", [0, 2, True, "1"])
def test_reference_cannot_point_forward_or_to_non_query(reference):
    with pytest.raises(ValueError):
        parse_operation_plan(json.dumps({"status": "ready", "steps": [
            {"operation": "create_file", "parameters": {"path": "a"}},
            {"operation": "copy_files", "parameters": {"source_step": reference, "destination": "backup"}}]}))


def test_conditional_query_and_preserved_backup_use_real_files(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_bytes(b"python source")
    (tmp_path / "src" / "old.py").write_bytes(b"old")
    old = time.time() - 9 * 86400
    os.utime(tmp_path / "src" / "old.py", (old, old))
    (tmp_path / "backup").mkdir()
    (tmp_path / "backup" / "already.py").write_bytes(b"exclude")
    outcomes = run_plan(tmp_path,
                        step("find_files", path=".", recursive=True, pattern="*.py", modified_within_days=7),
                        step("copy_files", source_step=1, destination="backup", preserve_structure=True))
    assert outcomes[0]["files"] == [str(Path("src/a.py"))]
    assert (tmp_path / "backup/src/a.py").read_bytes() == b"python source"
    assert not (tmp_path / "backup/src/old.py").exists()


def test_current_layer_and_size_conditions(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub/deep.pdf").write_bytes(b"abc")
    (tmp_path / "large.pdf").write_bytes(b"abcd")
    (tmp_path / "small.pdf").write_bytes(b"a")
    outcome = run_plan(tmp_path, step("count_files", path=".", recursive=False,
                                     pattern="*.pdf", size_gt_bytes=3))[0]
    assert outcome["count"] == 1
    assert outcome["files"] == ["large.pdf"]


def test_preview_does_not_create_any_file(tmp_path):
    prepare_plan(TaskPlan((step("create_file", path="nested/a.txt"),)), str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_copy_and_create_preserve_existing_content(tmp_path):
    (tmp_path / "source.txt").write_bytes(b"new")
    (tmp_path / "backup").mkdir()
    (tmp_path / "backup/source.txt").write_bytes(b"keep")
    run_plan(tmp_path, step("copy_files", sources=["source.txt"], destination="backup"))
    assert (tmp_path / "backup/source.txt").read_bytes() == b"keep"
    assert (tmp_path / "backup/source_1.txt").read_bytes() == b"new"
    run_plan(tmp_path, step("create_file", path="source.txt"))
    assert (tmp_path / "source.txt").read_bytes() == b"new"
    assert (tmp_path / "source_1.txt").read_bytes() == b""


def test_trash_and_restore_directory_with_name_collision(tmp_path):
    (tmp_path / "folder").mkdir()
    (tmp_path / "folder/file.txt").write_text("keep", encoding="utf-8")
    result = run_plan(tmp_path, step("trash", sources=["folder"]))[0]
    identifier = result["items"][0]["trash_id"]
    assert not (tmp_path / "folder").exists()
    assert trash_entries(tmp_path)[0]["original"] == "folder"
    (tmp_path / "folder").mkdir()
    run_plan(tmp_path, step("restore", trash_id=identifier))
    assert (tmp_path / "folder_1/file.txt").read_text(encoding="utf-8") == "keep"
    assert (tmp_path / "folder").is_dir()
    assert trash_entries(tmp_path) == []


def test_organize_by_type_keeps_all_files(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"pdf")
    (tmp_path / "README").write_bytes(b"readme")
    run_plan(tmp_path, step("organize_files", path=".", recursive=False, group_by="extension"))
    assert (tmp_path / "pdf/a.pdf").read_bytes() == b"pdf"
    assert (tmp_path / "no_extension/README").read_bytes() == b"readme"


@pytest.mark.parametrize("operation,parameters", [
    ("create_file", {"path": "../outside.txt"}),
    ("trash", {"sources": ["."]}),
    ("copy_files", {"sources": ["."], "destination": "backup"}),
    ("create_file", {"path": ".nl2shell-trash/data"}),
    ("copy_files", {"sources": ["folder"], "destination": "folder/backup"}),
])
def test_workspace_root_escape_and_reserved_paths_are_blocked(tmp_path, operation, parameters):
    (tmp_path / "folder").mkdir()
    with pytest.raises(ValueError):
        prepare_plan(TaskPlan((step(operation, **parameters),)), str(tmp_path))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["folder"]


def test_symbolic_link_is_blocked_for_query_and_copy(tmp_path):
    source = tmp_path / "a.txt"
    source.write_bytes(b"keep")
    try:
        (tmp_path / "link").symlink_to(source)
    except OSError:
        pytest.skip("当前 Windows 账号不允许创建符号链接；Linux CI 必须运行")
    for operation in (step("find_files", path=".", recursive=True),
                      step("copy_files", sources=["link"], destination="backup")):
        with pytest.raises(ValueError, match="链接"):
            prepare_plan(TaskPlan((operation,)), str(tmp_path))


def test_files_changed_after_confirmation_are_not_overwritten(tmp_path):
    (tmp_path / "source").write_bytes(b"data")
    plan = TaskPlan((step("copy_files", sources=["source"], destination="backup"),))
    before = prepare_plan(plan, str(tmp_path))
    (tmp_path / "backup").mkdir()
    (tmp_path / "backup/source").write_bytes(b"keep")
    after = prepare_plan(plan, str(tmp_path), now=before["now"])
    assert preparation_changed(before, after)
    outcome = execute_action(before, before["actions"][0])
    assert outcome["status"] == "execution_failed"
    assert (tmp_path / "backup/source").read_bytes() == b"keep"


def test_source_changed_is_blocked_before_copy(tmp_path):
    (tmp_path / "source").write_bytes(b"old")
    prepared = prepare_plan(TaskPlan((step("copy_files", sources=["source"], destination="backup"),)), str(tmp_path))
    (tmp_path / "source").write_bytes(b"new")
    result = execute_action(prepared, prepared["actions"][0])
    assert result["status"] == "execution_failed"
    assert not (tmp_path / "backup").exists()


def test_trash_random_ids_do_not_trigger_false_reconfirmation(tmp_path):
    (tmp_path / "a").write_bytes(b"data")
    plan = TaskPlan((step("trash", sources=["a"]),))
    before = prepare_plan(plan, str(tmp_path))
    after = prepare_plan(plan, str(tmp_path), now=before["now"])
    assert not preparation_changed(before, after)


def test_tampered_restore_record_cannot_escape_workspace(tmp_path):
    (tmp_path / "a").write_bytes(b"data")
    identifier = run_plan(tmp_path, step("trash", sources=["a"]))[0]["items"][0]["trash_id"]
    record = tmp_path / ".nl2shell-trash" / identifier / "record.json"
    record.write_text(json.dumps({"id": identifier, "original": "../outside"}), encoding="utf-8")
    with pytest.raises(ValueError):
        prepare_plan(TaskPlan((step("restore", trash_id=identifier),)), str(tmp_path))
    assert (record.parent / "data").read_bytes() == b"data"


def test_system_queries_use_only_program_selected_command(tmp_path):
    from core.execution import ExecutionResult
    executor = Mock()
    executor.execute.return_value = ExecutionResult(0, "Linux", "", 0.1)
    prepared = prepare_plan(TaskPlan((step("system_info", query="system"),)), str(tmp_path))
    outcome = execute_action(prepared, prepared["actions"][0], executor)
    assert outcome["status"] == "verified"
    assert executor.execute.call_args.args == ("uname -a",)


def test_timeout_does_not_start_a_write(tmp_path):
    prepared = prepare_plan(TaskPlan((step("create_file", path="a"),)), str(tmp_path))
    outcome = execute_action(prepared, prepared["actions"][0], timeout_seconds=0)
    assert outcome["status"] == "execution_failed"
    assert outcome["timed_out"]
    assert not (tmp_path / "a").exists()


@pytest.mark.parametrize("directory", [False, True])
def test_native_move_never_overwrites_an_existing_target(tmp_path, directory):
    from core.operations import _move_no_replace
    source, target = tmp_path / "source", tmp_path / "target"
    if directory:
        source.mkdir()
        target.mkdir()
        (source / "file").write_bytes(b"source")
        (target / "file").write_bytes(b"keep")
    else:
        source.write_bytes(b"source")
        target.write_bytes(b"keep")
    with pytest.raises(OSError):
        _move_no_replace(source, target)
    assert source.exists() and target.exists()
    assert (target / "file" if directory else target).read_bytes() == b"keep"


def test_repeated_organization_does_not_nest_classification_folders(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"pdf")
    operation = step("organize_files", path=".", recursive=True, group_by="extension")
    run_plan(tmp_path, operation)
    run_plan(tmp_path, operation)
    assert (tmp_path / "pdf/a.pdf").read_bytes() == b"pdf"
    assert not (tmp_path / "pdf/pdf").exists()


def test_contradictory_ready_and_clarification_plan_is_rejected():
    with pytest.raises(ValueError):
        parse_operation_plan(json.dumps({"status": "ready", "clarification": "哪些文件？", "steps": [
            {"operation": "create_file", "parameters": {"path": "a"}}]}))


def test_trash_listing_accepts_noncanonical_workspace_path(tmp_path):
    (tmp_path / "a").write_bytes(b"data")
    identifier = run_plan(tmp_path, step("trash", sources=["a"]))[0]["items"][0]["trash_id"]
    entries = trash_entries(tmp_path / ".." / tmp_path.name)
    assert entries[0]["id"] == identifier
