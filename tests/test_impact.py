import pytest

from core.impact import analyze


def test_simple_echo_redirect_is_known_workspace_write():
    impact = analyze("echo 'admin' > admin.txt")
    assert impact.known is True
    assert impact.tags == ("write",)
    assert impact.write_paths == ("admin.txt",)


def test_simple_printf_redirect_is_known_write():
    impact = analyze("printf admin > admin.txt")
    assert impact.known is True
    assert impact.tags == ("write",)


def test_known_pipeline_and_sequence_are_analyzed():
    pipeline = analyze("find . -name '*.log' | sort | head -n 3")
    sequence = analyze("mkdir out && touch out/result.txt")
    assert pipeline.known is True
    assert sequence.known is True
    assert "write" in sequence.tags


@pytest.mark.parametrize("command", [
    "echo $USER > admin.txt",
    "echo $(whoami) > admin.txt",
    "echo admin >> admin.txt",
    "echo admin | tee admin.txt",
    "echo admin > one.txt > two.txt",
    "echo admin > *.txt",
])
def test_dynamic_or_complex_redirect_remains_unknown(command):
    assert analyze(command).known is False
