"""保守分析 Bash 命令的影响范围，不尝试解释未知语法。"""
from dataclasses import dataclass
import os
import shlex


READ_COMMANDS = {"ls", "pwd", "cat", "grep", "find", "head", "tail", "wc", "du", "df", "free", "ps", "uptime", "whoami", "uname", "ip", "ss", "stat", "test"}
WRITE_COMMANDS = {"mkdir", "touch", "cp", "mv", "tar", "zip", "chmod", "rmdir"}
DELETE_COMMANDS = {"rm", "rmdir"}
NETWORK_COMMANDS = {"curl", "wget", "ping", "ssh", "scp", "rsync"}


@dataclass(frozen=True)
class CommandImpact:
    tags: tuple[str, ...]
    paths: tuple[str, ...]
    known: bool
    summary: str


def analyze(command: str) -> CommandImpact:
    """返回命令的保守影响标签；管道、重定向和复杂控制语法视为未知。"""
    try:
        parts = shlex.split(command)
    except ValueError:
        return CommandImpact(("unknown",), (), False, "命令引号不完整，无法可靠分析")
    if not parts:
        return CommandImpact(("unknown",), (), False, "空命令无法分析")
    if any(token in {"|", "&&", "||", ";", ">", ">>", "<"} for token in parts) or any(ch in command for ch in "|;&><`$"):
        return CommandImpact(("unknown",), (), False, "包含管道、重定向或 Shell 展开，无法可靠分析")

    program = parts[0]
    arguments = [item for item in parts[1:] if not item.startswith("-")]
    tags: list[str] = []
    if program == "sudo":
        return CommandImpact(("privilege", "unknown"), tuple(arguments), False, "需要提权，自动执行已禁止")
    if program in NETWORK_COMMANDS:
        tags.append("network")
    elif program in DELETE_COMMANDS:
        tags.extend(("delete", "write"))
    elif program in WRITE_COMMANDS:
        tags.append("write")
    elif program in READ_COMMANDS:
        tags.append("read")
    else:
        return CommandImpact(("unknown",), tuple(arguments), False, f"未识别命令：{program}")

    if program == "find" and "-delete" in parts:
        tags = ["delete", "write"]
    summary = "、".join({"read": "读取", "write": "写入", "delete": "删除", "network": "访问网络"}[tag] for tag in tags)
    return CommandImpact(tuple(tags), tuple(arguments), True, f"该命令将{summary}" if summary else "无可识别影响")


def paths_stay_in_workspace(impact: CommandImpact, cwd: str) -> bool:
    """仅接受相对且不含 .. 的路径；选项和值无法可靠区分时按保守规则处理。"""
    for path in impact.paths:
        if path in {".", "./"}:
            continue
        if path.startswith("~") or os.path.isabs(path) or ".." in path.split("/"):
            return False
    return True
