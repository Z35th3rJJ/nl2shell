"""保守分析 Bash 命令的影响范围，不尝试解释未知语法。"""
from dataclasses import dataclass
from pathlib import Path
import shlex


READ_COMMANDS = {"ls", "pwd", "cat", "grep", "find", "head", "tail", "wc", "du", "df", "free", "ps", "uptime", "whoami", "uname", "ip", "ss", "stat", "test"}
WRITE_COMMANDS = {"mkdir", "touch", "cp", "mv", "tar", "zip", "chmod", "rmdir"}
DELETE_COMMANDS = {"rm", "rmdir"}
NETWORK_COMMANDS = {"curl", "wget", "ping", "ssh", "scp", "rsync"}


@dataclass(frozen=True)
class CommandImpact:
    tags: tuple[str, ...]
    read_paths: tuple[str, ...]
    write_paths: tuple[str, ...]
    known: bool
    summary: str

    @property
    def paths(self) -> tuple[str, ...]:
        """兼容旧调用方；新决策应使用带角色的路径。"""
        return self.read_paths + self.write_paths


def _path_roles(program: str, arguments: list[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if program in {"cp", "mv"} and len(arguments) >= 2:
        return tuple(arguments[:-1]), (arguments[-1],)
    if program in DELETE_COMMANDS or program in WRITE_COMMANDS:
        return (), tuple(arguments)
    if program in READ_COMMANDS:
        return tuple(arguments), ()
    return (), ()


def analyze(command: str) -> CommandImpact:
    """返回命令的保守影响标签，并区分读取路径与写入路径。"""
    try:
        parts = shlex.split(command)
    except ValueError:
        return CommandImpact(("unknown",), (), (), False, "命令引号不完整，无法可靠分析")
    if not parts:
        return CommandImpact(("unknown",), (), (), False, "空命令无法分析")
    if any(token in {"|", "&&", "||", ";", ">", ">>", "<"} for token in parts) or any(ch in command for ch in "|;&><`$"):
        return CommandImpact(("unknown",), (), (), False, "包含管道、重定向或 Shell 展开，无法可靠分析")

    program = parts[0]
    arguments = [item for item in parts[1:] if not item.startswith("-")]
    if program == "sudo":
        return CommandImpact(("privilege", "unknown"), (), (), False, "需要提权，自动执行已禁止")

    tags: list[str] = []
    if program in NETWORK_COMMANDS:
        tags.append("network")
    elif program in DELETE_COMMANDS:
        tags.extend(("delete", "write"))
    elif program in WRITE_COMMANDS:
        tags.append("write")
    elif program in READ_COMMANDS:
        tags.append("read")
    else:
        return CommandImpact(("unknown",), (), (), False, f"未识别命令：{program}")

    if program == "find" and "-delete" in parts:
        tags = ["delete", "write"]
    read_paths, write_paths = _path_roles(program, arguments)
    summary = "、".join({"read": "读取", "write": "写入", "delete": "删除", "network": "访问网络"}[tag] for tag in tags)
    return CommandImpact(tuple(tags), read_paths, write_paths, True, f"该命令将{summary}" if summary else "无可识别影响")


def _inside_workspace(path: str, cwd: str) -> bool:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate
    try:
        candidate.resolve(strict=False).relative_to(Path(cwd).resolve(strict=False))
        return True
    except ValueError:
        return False


def paths_stay_in_workspace(impact: CommandImpact, cwd: str) -> bool:
    """只判断命令实际写入的路径是否位于工作区。"""
    return all(_inside_workspace(path, cwd) for path in impact.write_paths)


def reads_stay_in_workspace(impact: CommandImpact, cwd: str) -> bool:
    return all(_inside_workspace(path, cwd) for path in impact.read_paths)
