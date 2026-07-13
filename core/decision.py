"""兼容 adapter：新代码应使用 core.command_review。"""
from .command_review import (
    AUTO_ALLOW,
    BLOCK,
    CONFIRM,
    STRONG_CONFIRM,
    CommandImpact,
    ExecutionDecision,
    decide_execution,
)


def decide(impact: CommandImpact, risk: str, cwd: str,
           overwrite_paths: tuple[str, ...] = ()) -> ExecutionDecision:
    return decide_execution(impact, risk, cwd, overwrite_paths)
