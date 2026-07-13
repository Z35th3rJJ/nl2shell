"""执行错误的确定性分类。"""
from dataclasses import dataclass
import re

from .execution import ExecutionResult


@dataclass(frozen=True)
class ErrorAnalysis:
    category: str
    summary: str
    checks: tuple[str, ...]


def classify_error(result: ExecutionResult) -> ErrorAnalysis | None:
    if result.timed_out:
        return ErrorAnalysis("timeout", "命令执行超时", ("检查命令是否等待输入或持续运行", "检查网络、锁和资源占用"))
    if result.exit_code == 0:
        return None
    text = f"{result.stderr}\n{result.stdout}".lower()
    rules = [
        (r"command not found|not recognized", "command_not_found", "命令不存在", ("检查命令拼写", "检查软件是否安装及 PATH")),
        (r"no such file|not found", "file_not_found", "文件或路径不存在", ("检查当前目录", "检查路径大小写与扩展名")),
        (r"permission denied|operation not permitted", "permission_denied", "权限不足", ("检查文件所有者和权限", "不要自动扩大权限")),
        (r"address already in use|port .* in use", "port_in_use", "端口已被占用", ("检查监听端口的进程",)),
        (r"no space left|disk full", "disk_full", "磁盘空间不足", ("检查 df -h", "检查 inode 使用量")),
        (r"network is unreachable|connection refused|could not resolve|timed out", "network_failure", "网络连接失败", ("检查网络和目标地址",)),
        (r"invalid option|unrecognized option|unknown option|usage:", "invalid_argument", "命令参数无效", ("查看命令帮助和版本",)),
    ]
    for pattern, category, summary, checks in rules:
        if re.search(pattern, text):
            return ErrorAnalysis(category, summary, checks)
    return ErrorAnalysis("unknown_error", "命令执行失败", ("检查退出码和 stderr",))
