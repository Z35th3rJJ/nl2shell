"""执行策略：策略只决定是否可执行，不替代原有高危命令检测。"""
from dataclasses import dataclass
from .impact import CommandImpact, paths_stay_in_workspace

READ_ONLY = "read-only"
WORKSPACE = "workspace"
MANUAL = "manual"
POLICIES = (READ_ONLY, WORKSPACE, MANUAL)


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    requires_confirmation: bool
    reason: str


def evaluate(impact: CommandImpact, policy: str, cwd: str) -> PolicyDecision:
    tags = set(impact.tags)
    if policy == MANUAL:
        return PolicyDecision(True, True, "人工确认策略")
    if not impact.known or "unknown" in tags:
        return PolicyDecision(False, False, "未知命令不允许在受控策略下执行")
    if "privilege" in tags or "network" in tags:
        return PolicyDecision(False, False, "受控策略禁止提权和网络访问")
    if policy == READ_ONLY:
        return PolicyDecision(tags == {"read"}, False, "只读策略只允许已识别的读取命令")
    if policy == WORKSPACE:
        if not paths_stay_in_workspace(impact, cwd):
            return PolicyDecision(False, False, "命令路径可能越出当前工作目录")
        return PolicyDecision(True, "delete" in tags, "删除操作仍需人工确认" if "delete" in tags else "工作目录策略允许")
    return PolicyDecision(False, False, "未知策略")
