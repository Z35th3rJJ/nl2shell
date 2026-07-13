from unittest.mock import Mock, patch

from core.execution import BashExecutor, bash_unavailable_message


def test_executor_collects_process_result():
    process = Mock(returncode=7)
    process.wait.return_value = 7

    def popen(*args, **kwargs):
        kwargs["stdout"].write(b"output")
        kwargs["stderr"].write(b"error")
        return process

    with patch("core.execution.subprocess.Popen", side_effect=popen) as spawn:
        result = BashExecutor(bash_path="bash").execute("false")

    assert result.exit_code == 7
    assert result.stdout == "output"
    assert result.stderr == "error"
    assert spawn.call_args.args[0] == ["bash", "-lc", "false"]
    process.wait.assert_called_once_with(timeout=60)


def test_executor_turns_timeout_into_structured_result():
    process = Mock(returncode=None)
    process.wait.side_effect = __import__("subprocess").TimeoutExpired("bash", 60)
    with patch("core.execution.subprocess.Popen", return_value=process), \
         patch("core.execution._terminate_process_tree") as terminate:
        result = BashExecutor(bash_path="bash").execute("sleep 999")

    assert result.timed_out is True
    assert result.exit_code is None
    terminate.assert_called_once_with(process)


def test_executor_truncates_large_output():
    process = Mock(returncode=0)
    process.wait.return_value = 0

    def popen(*args, **kwargs):
        kwargs["stdout"].write(b"a" * 100)
        return process

    with patch("core.execution.subprocess.Popen", side_effect=popen):
        result = BashExecutor(bash_path="bash", output_limit=20).execute("generate")
    assert result.output_truncated is True
    assert "输出已截断" in result.stdout


def test_bash_missing_message_guides_configuration():
    assert "BASH_PATH" in bash_unavailable_message()


def test_executor_rejects_unusable_bash():
    completed = Mock(returncode=1)
    with patch("core.execution.subprocess.run", return_value=completed):
        assert BashExecutor(bash_path="bash").is_available() is False
