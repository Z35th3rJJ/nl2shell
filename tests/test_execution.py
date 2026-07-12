import subprocess
from unittest.mock import Mock, patch

from core.execution import BashExecutor, bash_unavailable_message


def test_executor_collects_process_result():
    completed = Mock(returncode=7, stdout="output", stderr="error")
    with patch("core.execution.subprocess.run", return_value=completed) as run:
        result = BashExecutor(bash_path="bash").execute("false")

    assert result.exit_code == 7
    assert result.stdout == "output"
    assert result.stderr == "error"
    assert run.call_args.args[0] == ["bash", "-lc", "false"]
    assert run.call_args.kwargs["timeout"] == 60


def test_executor_turns_timeout_into_structured_result():
    with patch("core.execution.subprocess.run", side_effect=subprocess.TimeoutExpired("bash", 60)):
        result = BashExecutor(bash_path="bash").execute("sleep 999")

    assert result.timed_out is True
    assert result.exit_code is None


def test_bash_missing_message_guides_configuration():
    assert "BASH_PATH" in bash_unavailable_message()


def test_executor_rejects_unusable_bash():
    completed = Mock(returncode=1)
    with patch("core.execution.subprocess.run", return_value=completed):
        assert BashExecutor(bash_path="bash").is_available() is False
