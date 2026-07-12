from core.decision import AUTO_ALLOW, BLOCK, CONFIRM, STRONG_CONFIRM, decide
from core.impact import analyze
from core.safety import check


def _decide(command):
    risk, _ = check(command)
    return decide(analyze(command), risk, "/work")


def test_read_and_workspace_write_are_auto_allowed():
    assert _decide("ls -la").level == AUTO_ALLOW
    assert _decide("touch result.txt").level == AUTO_ALLOW


def test_delete_network_and_outside_write_require_confirmation():
    assert _decide("rm result.txt").level == CONFIRM
    assert _decide("curl https://example.com").level == CONFIRM
    assert _decide("touch ../outside.txt").level == CONFIRM


def test_sudo_and_unknown_syntax_require_strong_confirmation():
    assert _decide("sudo apt update").level == STRONG_CONFIRM
    assert _decide("ls | wc -l").level == STRONG_CONFIRM


def test_destructive_command_is_always_blocked():
    assert _decide("rm -rf /").level == BLOCK
