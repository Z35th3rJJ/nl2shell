"""命令执行后的保守验证器。"""
from dataclasses import dataclass
from .execution import BashExecutor, ExecutionResult
from .impact import analyze


@dataclass(frozen=True)
class VerificationResult:
    status: str
    detail: str
    result: ExecutionResult | None = None


def verify(executor: BashExecutor, command_result: ExecutionResult, verification_command: str) -> VerificationResult:
    if command_result.exit_code != 0:
        return VerificationResult("command_failed", "主命令退出码非 0")
    if not verification_command:
        return VerificationResult("exit_code_only", "未提供验证命令，已按退出码判定")
    impact = analyze(verification_command)
    if not impact.known or set(impact.tags) != {"read"}:
        return VerificationResult("invalid_verifier", "验证命令不是已识别的只读命令")
    result = executor.execute(verification_command, timeout_seconds=10)
    if result.exit_code == 0:
        return VerificationResult("verified", "验证命令执行成功", result)
    return VerificationResult("verification_failed", "验证命令退出码非 0", result)
