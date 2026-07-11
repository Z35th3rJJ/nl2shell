from unittest.mock import Mock

from core.execution import ExecutionResult
from core.verification import verify


def test_verification_uses_successful_read_only_command():
    executor = Mock()
    executor.execute.return_value = ExecutionResult(0, "", "", 0.1)
    result = verify(executor, ExecutionResult(0, "", "", 0.1), "test -e result.txt")
    assert result.status == "verified"


def test_verification_rejects_write_command():
    result = verify(Mock(), ExecutionResult(0, "", "", 0.1), "touch result.txt")
    assert result.status == "invalid_verifier"


def test_failed_main_command_skips_verifier():
    result = verify(Mock(), ExecutionResult(1, "", "error", 0.1), "test -e result.txt")
    assert result.status == "command_failed"
