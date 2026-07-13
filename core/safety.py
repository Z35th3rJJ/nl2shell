"""兼容 adapter：新代码应使用 core.command_review。"""
from .command_review import HIGH, SAFE, WARN, SafetyAssessment, assess_safety


def assess(command: str) -> SafetyAssessment:
    return assess_safety(command)


def check(command: str) -> tuple[str, str]:
    result = assess(command)
    return result.level, result.reason
