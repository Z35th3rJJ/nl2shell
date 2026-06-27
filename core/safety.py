import re

SAFE = "SAFE"
WARN = "WARN"
HIGH = "HIGH"

# (正则模式, 风险等级, 风险说明)
_RULES = [
    (r"rm\s+\S*-\S*r\S*f.*\s[/~]",      HIGH, "递归强制删除根目录或家目录，会造成不可恢复的数据丢失"),
    (r"rm\s+\S*-\S*r\S*f",              WARN, "递归强制删除，请仔细确认目标路径"),
    (r"mkfs",                            HIGH, "格式化文件系统，磁盘所有数据将被清除"),
    (r"dd\s+.*of=/dev/",                HIGH, "直接写入磁盘设备，可能导致数据损坏或系统崩溃"),
    (r":\(\)\s*\{.*\}",                 HIGH, "Fork 炸弹，会耗尽系统资源导致崩溃"),
    (r"chmod\s+\S*7{3}\s+/",            HIGH, "修改根目录权限，存在严重安全风险"),
    (r">\s*/dev/sd[a-z]",               HIGH, "覆写磁盘设备，会导致数据损坏"),
    (r"mv\s+.*/\s+/dev/null",           HIGH, "将根目录文件移入黑洞，系统将无法运行"),
    (r"kill\s+-9\s+-1",                 HIGH, "强制终止所有进程，系统将立即崩溃"),
    (r"\|\s*(bash|sh|zsh|fish)\b",      HIGH, "将内容直接交给 Shell 执行，存在代码注入风险"),
    (r"(shutdown|reboot|halt|poweroff)", WARN, "系统电源操作，将影响所有正在运行的程序"),
    (r"sudo\s+rm",                       WARN, "以管理员权限删除文件，请确认目标路径"),
    (r"truncate\s+.*-s\s+0\s+/",        HIGH, "清空系统文件，可能导致系统损坏"),
]


def check(cmd: str) -> tuple[str, str]:
    """检查命令风险，返回 (风险等级, 风险说明)。"""
    for pattern, level, reason in _RULES:
        if re.search(pattern, cmd, re.IGNORECASE):
            return level, reason
    return SAFE, ""
