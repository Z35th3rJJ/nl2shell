from core.decision import AUTO_ALLOW, BLOCK, CONFIRM, STRONG_CONFIRM, decide
from core.impact import analyze, deletion_covers_workspace
from core.safety import check


def _decide(command):
    risk, _ = check(command)
    return decide(analyze(command), risk, "/work")


def test_read_and_workspace_write_are_auto_allowed():
    assert _decide("ls -la").level == AUTO_ALLOW
    assert _decide("touch result.txt").level == AUTO_ALLOW


def test_workspace_absolute_write_is_allowed(tmp_path):
    command = f"cp /outside/source.txt {(tmp_path / 'result.txt').as_posix()}"
    risk, _ = check(command)
    assert decide(analyze(command), risk, str(tmp_path)).level == AUTO_ALLOW


def test_delete_network_and_outside_write_require_confirmation():
    assert _decide("rm result.txt").level == CONFIRM
    assert _decide("curl https://example.com").level == CONFIRM
    assert _decide("touch ../outside.txt").level == CONFIRM


def test_copy_only_uses_destination_for_write_boundary(tmp_path):
    inside = analyze(f"cp /outside/source.txt {(tmp_path / 'result.txt').as_posix()}")
    outside = analyze(f"cp source.txt {(tmp_path.parent / 'outside.txt').as_posix()}")
    inside_decision = decide(inside, "SAFE", str(tmp_path))
    assert inside_decision.level == AUTO_ALLOW
    assert "读取工作区外文件" in inside_decision.reason
    assert decide(outside, "SAFE", str(tmp_path)).level == CONFIRM


def test_sudo_and_unknown_syntax_require_strong_confirmation():
    assert _decide("sudo apt update").level == STRONG_CONFIRM
    assert _decide("ls | wc -l").level == STRONG_CONFIRM


def test_destructive_command_is_always_blocked():
    assert _decide("rm -rf /").level == BLOCK


def test_deleting_workspace_or_parent_is_blocked(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    for target in (workspace.as_posix(), ".", "./", "..", tmp_path.as_posix()):
        impact = analyze(f"rm -rf -- {target}")
        decision = decide(impact, "WARN", str(workspace))
        assert decision.level == BLOCK
        assert decision.rule == "workspace_root_delete"


def test_deleting_workspace_child_still_requires_confirmation(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    for command in ("rm -rf child", "rm file.txt", "find . -mindepth 1 -delete"):
        risk, _ = check(command)
        assert decide(analyze(command), risk, str(workspace)).level == CONFIRM


def test_symlink_to_workspace_is_protected(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    link = tmp_path / "project-link"
    try:
        link.symlink_to(workspace, target_is_directory=True)
    except OSError:
        return
    assert deletion_covers_workspace(analyze(f"rm -rf {link.as_posix()}"), str(workspace)) is True
