import pytest

from core.automation import should_auto_execute
from core.safety import HIGH, SAFE, WARN


@pytest.mark.parametrize("risk, expected", [
    (SAFE, True),
    (WARN, False),
    (HIGH, False),
])
def test_auto_mode_only_allows_safe_commands(risk, expected):
    assert should_auto_execute(True, risk) is expected


def test_interactive_mode_never_auto_executes():
    assert should_auto_execute(False, SAFE) is False
