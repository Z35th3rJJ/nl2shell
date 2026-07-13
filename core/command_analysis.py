"""兼容 adapter：新代码应使用 core.command_review。"""
from dataclasses import dataclass

from .command_review import (
    CommandImpact,
    SafetyAssessment,
    raise_risk,
    review_command,
)


@dataclass(frozen=True)
class CommandAnalysis:
    command: str
    impact: CommandImpact
    safety: SafetyAssessment
    overwrite_paths: tuple[str, ...] = ()

    @property
    def known(self) -> bool:
        return self.impact.known


def analyze_command(command: str, cwd: str) -> CommandAnalysis:
    review = review_command(command, cwd)
    return CommandAnalysis(
        command, review.impact, review.safety, review.overwrite_paths,
    )
