"""兼容 adapter：新代码应使用 core.command_review。"""
from .command_review import (
    CommandImpact,
    analyze_impact,
    deletion_covers_workspace,
    paths_stay_in_workspace,
    reads_stay_in_workspace,
)


def analyze(command: str) -> CommandImpact:
    return analyze_impact(command)
