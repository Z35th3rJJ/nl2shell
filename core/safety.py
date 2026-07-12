import re

SAFE = "SAFE"
WARN = "WARN"
HIGH = "HIGH"


def _has_rm_recursive(cmd: str) -> bool:
    return bool(re.search(r"(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)", cmd))


def _has_rm_force(cmd: str) -> bool:
    return bool(re.search(r"(-[a-zA-Z]*f[a-zA-Z]*|--force)", cmd))


def _targets_root_or_home(cmd: str) -> bool:
    literal = bool(re.search(r"\s[/~](\s|$|[/*])", cmd))
    expanded_home = bool(re.search(
        r"\s(?:['\"]?\$(?:\{HOME\}|HOME)['\"]?|~(?:root)?)(?:/|\s|$)", cmd,
    ))
    return literal or expanded_home


def _has_wildcard(cmd: str) -> bool:
    return bool(re.search(r"(\s\*|/\*)", cmd))


def _check_rm(cmd: str) -> tuple[str, str]:
    if not re.search(r"\brm\b", cmd):
        return SAFE, ""

    has_recursive = _has_rm_recursive(cmd)
    has_force     = _has_rm_force(cmd)
    targets_danger = _targets_root_or_home(cmd)
    has_wildcard   = _has_wildcard(cmd)

    if has_recursive and targets_danger:
        return HIGH, "递归删除根目录或家目录，会造成不可恢复的数据丢失"
    if has_recursive and has_force:
        return WARN, "递归强制删除，请仔细确认目标路径"
    if has_recursive:
        return WARN, "递归删除操作，请仔细确认目标路径"
    if has_wildcard:
        return WARN, "通配符删除，将删除所有匹配文件，请确认"
    return SAFE, ""


# (正则模式, 风险等级, 风险说明)
_RULES = [
    (r"mkfs",                             HIGH, "格式化文件系统，磁盘所有数据将被清除"),
    (r"dd\s+.*of=/dev/",                 HIGH, "直接写入磁盘设备，可能导致数据损坏或系统崩溃"),
    (r":\(\)\s*\{.*\}",                  HIGH, "Fork 炸弹，会耗尽系统资源导致崩溃"),
    (r"chmod\b.*\b777\b.*\s[/~](\s|$|[/*])", HIGH, "修改根目录或家目录权限，存在严重安全风险"),
    (r">\s*/dev/sd[a-z]",                HIGH, "覆写磁盘设备，会导致数据损坏"),
    (r"kill\s+-9\s+-1",                  HIGH, "强制终止所有进程，系统将立即崩溃"),
    (r"\|\s*(bash|sh|zsh|fish)\b",       HIGH, "将内容直接交给 Shell 执行，存在代码注入风险"),
    (r"truncate\s+.*-s\s+0\s+/",         HIGH, "清空系统关键文件，可能导致系统损坏"),
    # find -delete / find -exec rm：破坏力等同 rm -rf，需拦截
    (r"\bfind\b.*-delete\b",             WARN, "find -delete 将递归删除匹配文件，请确认目标路径"),
    (r"\bfind\b.*-exec\s+rm\b",          WARN, "find -exec rm 将批量删除文件，请确认目标路径"),
    (r"(shutdown|reboot|halt|poweroff)",  WARN, "系统电源操作，将影响所有正在运行的程序"),
    (r"sudo\s+rm",                        WARN, "以管理员权限删除文件，请确认目标路径"),
]


def check(cmd: str) -> tuple[str, str]:
    """检查命令风险，返回 (风险等级, 风险说明)。"""
    # rm 单独处理，逻辑比正则更准确
    level, reason = _check_rm(cmd)
    if level != SAFE:
        return level, reason

    for pattern, level, reason in _RULES:
        if re.search(pattern, cmd, re.IGNORECASE):
            return level, reason

    return SAFE, ""
