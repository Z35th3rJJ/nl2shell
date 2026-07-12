"""把命令风险、影响范围统一映射为执行决策。"""
from dataclasses import dataclass

from .impact import CommandImpact, paths_stay_in_workspace, reads_stay_in_workspace
from .safety import HIGH, WARN

AUTO_ALLOW = "auto-allow"
CONFIRM = "confirm"
STRONG_CONFIRM = "strong-confirm"
BLOCK = "block"


@dataclass(frozen=True)
class ExecutionDecision:
    level: str
    reason: str


def decide(impact: CommandImpact, risk: str, cwd: str) -> ExecutionDecision:
    tags = set(impact.tags)
    if risk == HIGH:
        return ExecutionDecision(BLOCK, "命令属于毁灭性高危操作，已永久阻止")
    if "privilege" in tags:
        return ExecutionDecision(STRONG_CONFIRM, "命令需要 sudo 或系统级权限")
    if not impact.known or "unknown" in tags:
        return ExecutionDecision(STRONG_CONFIRM, "命令包含未知或复杂 Shell 语法")
    if risk == WARN or "delete" in tags:
        return ExecutionDecision(CONFIRM, "命令包含删除或警告级操作")
    if "network" in tags:
        return ExecutionDecision(CONFIRM, "命令会访问网络")
    if "write" in tags and not paths_stay_in_workspace(impact, cwd):
        return ExecutionDecision(CONFIRM, "命令可能写入当前工作目录之外")
    if impact.read_paths and not reads_stay_in_workspace(impact, cwd):
        return ExecutionDecision(AUTO_ALLOW, "命令会读取工作区外文件，但写入仍在工作区内")
    return ExecutionDecision(AUTO_ALLOW, "已识别的安全操作")
