import pytest

from core.error_analysis import classify_error
from core.execution import ExecutionResult


@pytest.mark.parametrize("stderr,category", [
    ("bash: docker: command not found", "command_not_found"),
    ("No such file or directory", "file_not_found"),
    ("Permission denied", "permission_denied"),
    ("Address already in use", "port_in_use"),
    ("No space left on device", "disk_full"),
    ("Connection refused", "network_failure"),
    ("unrecognized option '--bad'", "invalid_argument"),
])
def test_classifies_common_failures(stderr, category):
    result = ExecutionResult(1, "", stderr, 0.1)
    assert classify_error(result).category == category


def test_timeout_has_dedicated_category():
    assert classify_error(ExecutionResult(None, "", "", 1, True)).category == "timeout"
