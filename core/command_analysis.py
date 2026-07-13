"""统一命令分析结果，供预览、安全决策和历史审计复用。"""
from dataclasses import dataclass
from pathlib import Path
import shlex

from .impact import CommandImpact, analyze
from .safety import SafetyAssessment, assess


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
    impact = analyze(command)
    overwrites = []
    try:
        program = shlex.split(command)[0]
    except (ValueError, IndexError):
        program = ""
    overwrite_capable = "delete" not in impact.tags and program not in {"touch", "mkdir", "chmod", "rmdir"}
    for value in impact.write_paths if overwrite_capable else ():
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path(cwd) / path
        if path.exists() and path.is_file():
            overwrites.append(str(path.resolve()))
    return CommandAnalysis(command, impact, assess(command), tuple(overwrites))


def raise_risk(deterministic: str, advisory: str | None) -> str:
    """LLM/外部建议只能提高风险，不能降低确定性结果。"""
    order = {"SAFE": 0, "WARN": 1, "HIGH": 2}
    if advisory not in order:
        return deterministic
    return max((deterministic, advisory), key=order.get)
