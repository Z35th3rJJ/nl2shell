"""命令执行前的统一审查 interface。"""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex


SAFE = "SAFE"
WARN = "WARN"
HIGH = "HIGH"

AUTO_ALLOW = "auto-allow"
CONFIRM = "confirm"
STRONG_CONFIRM = "strong-confirm"
BLOCK = "block"

_RISK_ORDER = {SAFE: 0, WARN: 1, HIGH: 2}
_READ_COMMANDS = {
    "ls", "pwd", "cat", "grep", "find", "head", "tail", "wc", "du",
    "df", "free", "ps", "uptime", "whoami", "uname", "ip", "ss",
    "stat", "test", "sort", "uniq", "awk", "sed", "cut",
}
_WRITE_COMMANDS = {"mkdir", "touch", "cp", "mv", "tar", "zip", "chmod", "rmdir"}
_DELETE_COMMANDS = {"rm", "rmdir"}
_NETWORK_COMMANDS = {"curl", "wget", "ping", "ssh", "scp", "rsync"}
_PREFLIGHT_COMMANDS = {"cp", "mv", "cat", "rm"}


@dataclass(frozen=True)
class SafetyFinding:
    rule: str
    level: str
    reason: str
    fragment: str


@dataclass(frozen=True)
class SafetyAssessment:
    level: str
    reason: str = ""
    rule: str = ""
    fragment: str = ""


@dataclass(frozen=True)
class CommandImpact:
    tags: tuple[str, ...]
    read_paths: tuple[str, ...]
    write_paths: tuple[str, ...]
    known: bool
    summary: str

    @property
    def paths(self) -> tuple[str, ...]:
        return self.read_paths + self.write_paths


@dataclass(frozen=True)
class ExecutionDecision:
    level: str
    reason: str
    rule: str = ""


@dataclass(frozen=True)
class PreflightIssue:
    kind: str
    step: int
    path: str
    candidates: tuple[str, ...]
    message: str
    argument_index: int | None = None
    program: str = ""
    source_count: int = 0
    segment: int = 0


@dataclass(frozen=True)
class CommandReview:
    command: str
    cwd: str
    parse_complete: bool
    programs: tuple[str, ...]
    read_paths: tuple[str, ...]
    write_paths: tuple[str, ...]
    deterministic_risk: str
    effective_risk: str
    decision: ExecutionDecision
    verification_allowed: bool
    impact: CommandImpact
    safety: SafetyAssessment
    overwrite_paths: tuple[str, ...] = ()
    preflight_issues: tuple[PreflightIssue, ...] = ()
    path_findings: tuple[PreflightIssue, ...] = ()
    findings: tuple[SafetyFinding, ...] = ()
    primary_finding: SafetyFinding | None = None


@dataclass(frozen=True)
class _ParsedShell:
    tokens: tuple[str, ...]
    segments: tuple[tuple[str, ...], ...]
    operators: tuple[str, ...] = ()


_SAFETY_RULES = (
    ("mkfs", r"mkfs", HIGH, "格式化文件系统，磁盘所有数据将被清除"),
    ("dd-device", r"dd\s+.*of=/dev/", HIGH, "直接写入磁盘设备，可能导致数据损坏或系统崩溃"),
    ("fork-bomb", r":\(\)\s*\{.*\}", HIGH, "Fork 炸弹，会耗尽系统资源导致崩溃"),
    ("chmod-root", r"chmod\b.*\b777\b.*\s[/~](\s|$|[/*])", HIGH, "修改根目录或家目录权限，存在严重安全风险"),
    ("device-write", r">\s*/dev/sd[a-z]", HIGH, "覆写磁盘设备，会导致数据损坏"),
    ("kill-all", r"kill\s+-9\s+-1", HIGH, "强制终止所有进程，系统将立即崩溃"),
    ("pipe-shell", r"\|\s*(bash|sh|zsh|fish)\b", HIGH, "将内容直接交给 Shell 执行，存在代码注入风险"),
    ("truncate-system", r"truncate\s+.*-s\s+0\s+/", HIGH, "清空系统关键文件，可能导致系统损坏"),
    ("find-delete", r"\bfind\b.*-delete\b", WARN, "find -delete 将递归删除匹配文件，请确认目标路径"),
    ("find-rm", r"\bfind\b.*-exec\s+rm\b", WARN, "find -exec rm 将批量删除文件，请确认目标路径"),
    ("power", r"(shutdown|reboot|halt|poweroff)", WARN, "系统电源操作，将影响所有正在运行的程序"),
    ("sudo-rm", r"sudo\s+rm", WARN, "以管理员权限删除文件，请确认目标路径"),
    ("command-substitution", r"\$\([^)]*\)|`[^`]+`", WARN, "命令包含命令替换，运行内容需要明确确认"),
)


def _rm_finding(command: str) -> SafetyFinding | None:
    match = re.search(r"\brm\b[^;&|\n]*", command, re.IGNORECASE)
    if not match:
        return None
    fragment = match.group(0)
    recursive = bool(re.search(r"(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)", fragment))
    force = bool(re.search(r"(-[a-zA-Z]*f[a-zA-Z]*|--force)", fragment))
    root_or_home = bool(re.search(
        r"\s(?:[/~](?:\s|$|[/*])|['\"]?\$(?:\{HOME\}|HOME)['\"]?(?:/|\s|$)|~(?:root)?(?:/|\s|$))",
        fragment,
    ))
    wildcard = bool(re.search(r"(\s\*|/\*)", fragment))
    if recursive and root_or_home:
        return SafetyFinding("rm", HIGH, "递归删除根目录或家目录，会造成不可恢复的数据丢失", fragment)
    if recursive and force:
        return SafetyFinding("rm", WARN, "递归强制删除，请仔细确认目标路径", fragment)
    if recursive:
        return SafetyFinding("rm", WARN, "递归删除操作，请仔细确认目标路径", fragment)
    if wildcard:
        return SafetyFinding("rm", WARN, "通配符删除，将删除所有匹配文件，请确认", fragment)
    return None


def collect_safety_findings(command: str) -> tuple[SafetyFinding, ...]:
    findings = []
    rm_finding = _rm_finding(command)
    if rm_finding:
        findings.append(rm_finding)
    for rule, pattern, level, reason in _SAFETY_RULES:
        match = re.search(pattern, command, re.IGNORECASE)
        if match:
            findings.append(SafetyFinding(rule, level, reason, match.group(0)))
    return tuple(findings)


def assess_safety(command: str) -> SafetyAssessment:
    findings = collect_safety_findings(command)
    primary = max(findings, key=lambda item: _RISK_ORDER[item.level], default=None)
    if primary is None:
        return SafetyAssessment(SAFE)
    return SafetyAssessment(primary.level, primary.reason, primary.rule, primary.fragment)


def raise_risk(deterministic: str, advisory: str | None) -> str:
    if advisory not in _RISK_ORDER:
        return deterministic
    return max((deterministic, advisory), key=_RISK_ORDER.get)


def _parse_shell(command: str) -> _ParsedShell | None:
    if any(character in command for character in "$;`"):
        return None
    lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
    lexer.whitespace_split = True
    try:
        tokens = tuple(lexer)
    except ValueError:
        return None
    if not tokens or any(token in {"||", ";", "<", ">>"} for token in tokens):
        return None
    segments: list[list[str]] = [[]]
    operators = []
    for token in tokens:
        if token in {"|", "&&"}:
            if not segments[-1]:
                return None
            operators.append(token)
            segments.append([])
        else:
            segments[-1].append(token)
    if not segments[-1]:
        return None
    return _ParsedShell(tokens, tuple(tuple(segment) for segment in segments), tuple(operators))


def _simple_file_write(command: str, parsed: _ParsedShell | None) -> CommandImpact | None:
    if parsed is None or len(parsed.segments) != 1:
        return None
    parts = parsed.segments[0]
    if len(parts) != 4 or parts[0] not in {"echo", "printf"} or parts[2] != ">":
        return None
    destination = parts[3]
    if not destination or any(character in destination for character in "*?[]"):
        return None
    return CommandImpact(("write",), (), (destination,), True, "该命令将写入文件")


def _composite_impact(command: str, parsed: _ParsedShell | None) -> CommandImpact | None:
    if parsed is None or not any(token in {"|", "&&", ">"} for token in parsed.tokens):
        return None

    impacts = []
    for raw_segment in parsed.segments:
        segment = list(raw_segment)
        write_paths: tuple[str, ...] = ()
        if ">" in segment:
            if segment.count(">") != 1 or segment.index(">") != len(segment) - 2:
                return None
            destination = segment[-1]
            if any(character in destination for character in "*?[]"):
                return None
            write_paths = (destination,)
            segment = segment[:-2]
        if not segment:
            return None
        nested = _ParsedShell(tuple(segment), (tuple(segment),))
        impact = _analyze_impact(shlex.join(segment), nested)
        if not impact.known:
            return None
        tags = tuple(dict.fromkeys((*impact.tags, *(("write",) if write_paths else ()))))
        impacts.append(CommandImpact(
            tags, impact.read_paths, (*impact.write_paths, *write_paths), True, impact.summary,
        ))
    tags = tuple(dict.fromkeys(tag for impact in impacts for tag in impact.tags))
    reads = tuple(path for impact in impacts for path in impact.read_paths)
    writes = tuple(path for impact in impacts for path in impact.write_paths)
    return CommandImpact(tags, reads, writes, True, "该组合命令的结构和影响已识别")


def _path_roles(program: str, arguments: list[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if program in {"cp", "mv"} and len(arguments) >= 2:
        return tuple(arguments[:-1]), (arguments[-1],)
    if program in _DELETE_COMMANDS or program in _WRITE_COMMANDS:
        return (), tuple(arguments)
    if program in _READ_COMMANDS:
        return tuple(arguments), ()
    return (), ()


def _analyze_impact(command: str, parsed: _ParsedShell | None) -> CommandImpact:
    simple_write = _simple_file_write(command, parsed)
    if simple_write is not None:
        return simple_write
    composite = _composite_impact(command, parsed)
    if composite is not None:
        return composite
    try:
        parts = shlex.split(command)
    except ValueError:
        return CommandImpact(("unknown",), (), (), False, "命令引号不完整，无法可靠分析")
    if not parts:
        return CommandImpact(("unknown",), (), (), False, "空命令无法分析")
    if any(token in {"|", "&&", "||", ";", ">", ">>", "<"} for token in parts) or any(
        character in command for character in "|;&><`$"
    ):
        return CommandImpact(("unknown",), (), (), False, "包含管道、重定向或 Shell 展开，无法可靠分析")

    program = parts[0]
    arguments = [item for item in parts[1:] if not item.startswith("-")]
    if program == "sudo":
        return CommandImpact(("privilege", "unknown"), (), (), False, "需要提权，自动执行已禁止")

    if program in _NETWORK_COMMANDS:
        tags = ["network"]
    elif program in _DELETE_COMMANDS:
        tags = ["delete", "write"]
    elif program in _WRITE_COMMANDS:
        tags = ["write"]
    elif program in _READ_COMMANDS:
        tags = ["read"]
    else:
        return CommandImpact(("unknown",), (), (), False, f"未识别命令：{program}")

    if program == "find" and "-delete" in parts:
        tags = ["delete", "write"]
    read_paths, write_paths = _path_roles(program, arguments)
    if program == "find" and "-delete" in parts and arguments:
        minimum_depth = None
        if "-mindepth" in parts:
            index = parts.index("-mindepth")
            if index + 1 < len(parts) and parts[index + 1].isdigit():
                minimum_depth = int(parts[index + 1])
        target = str(Path(arguments[0]) / "*") if minimum_depth and minimum_depth >= 1 else arguments[0]
        read_paths, write_paths = (), (target,)
    labels = {"read": "读取", "write": "写入", "delete": "删除", "network": "访问网络"}
    summary = "、".join(labels[tag] for tag in tags)
    return CommandImpact(tuple(tags), read_paths, write_paths, True, f"该命令将{summary}")


def analyze_impact(command: str) -> CommandImpact:
    return _analyze_impact(command, _parse_shell(command))


def _inside_workspace(path: str, cwd: str) -> bool:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate
    try:
        candidate.resolve(strict=False).relative_to(Path(cwd).resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _workspace_findings(impact: CommandImpact, cwd: str) -> tuple[PreflightIssue, ...]:
    root = Path(cwd).resolve(strict=False)
    findings = []
    for value in impact.write_paths:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = root / path
        try:
            resolved = path.resolve(strict=False)
            resolved.relative_to(root)
        except ValueError:
            findings.append(PreflightIssue(
                "workspace_escape", 1, value, (), f"写入路径位于工作区之外：{value}",
            ))
        except OSError:
            findings.append(PreflightIssue(
                "unverified_path", 1, value, (), f"路径事实无法验证：{value}",
            ))
    return tuple(findings)


def _write_access_findings(impact: CommandImpact, cwd: str) -> tuple[PreflightIssue, ...]:
    root = Path(cwd).resolve(strict=False)
    findings = []
    for value in impact.write_paths:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = root / path
        try:
            parent = path if path.exists() and path.is_dir() else path.parent
            if not parent.is_dir() or not os.access(parent, os.W_OK):
                findings.append(PreflightIssue(
                    "unverified_path", 1, value, (), f"写入目标的父目录不可访问：{value}",
                ))
        except OSError:
            findings.append(PreflightIssue(
                "unverified_path", 1, value, (), f"路径事实无法验证：{value}",
            ))
    return tuple(findings)


def paths_stay_in_workspace(impact: CommandImpact, cwd: str) -> bool:
    return all(_inside_workspace(path, cwd) for path in impact.write_paths)


def reads_stay_in_workspace(impact: CommandImpact, cwd: str) -> bool:
    return all(_inside_workspace(path, cwd) for path in impact.read_paths)


def deletion_covers_workspace(impact: CommandImpact, cwd: str) -> bool:
    if "delete" not in impact.tags:
        return False
    workspace = Path(cwd).expanduser().resolve(strict=False)
    for value in impact.write_paths:
        target = Path(value).expanduser()
        if not target.is_absolute():
            target = workspace / target
        try:
            target = target.resolve(strict=False)
        except OSError:
            return True
        if target == workspace:
            return True
        try:
            workspace.relative_to(target)
            return True
        except ValueError:
            continue
    return False


def decide_execution(impact: CommandImpact, risk: str, cwd: str,
                     overwrite_paths: tuple[str, ...] = (), *, path_incomplete: bool = False) -> ExecutionDecision:
    tags = set(impact.tags)
    if risk == HIGH:
        return ExecutionDecision(BLOCK, "命令属于毁灭性高危操作，已永久阻止", "high_risk")
    if deletion_covers_workspace(impact, cwd):
        return ExecutionDecision(BLOCK, "禁止删除当前工作区本身或其父目录", "workspace_root_delete")
    if path_incomplete:
        return ExecutionDecision(STRONG_CONFIRM, "路径事实无法验证", "incomplete_path")
    if "privilege" in tags:
        return ExecutionDecision(STRONG_CONFIRM, "命令需要 sudo 或系统级权限")
    if not impact.known or "unknown" in tags:
        return ExecutionDecision(STRONG_CONFIRM, "命令包含未知或复杂 Shell 语法")
    if risk == WARN or "delete" in tags:
        return ExecutionDecision(CONFIRM, "命令包含删除或警告级操作")
    if overwrite_paths:
        return ExecutionDecision(CONFIRM, "命令会覆盖已有文件", "file_overwrite")
    if "network" in tags:
        return ExecutionDecision(CONFIRM, "命令会访问网络")
    if "write" in tags and not paths_stay_in_workspace(impact, cwd):
        return ExecutionDecision(CONFIRM, "命令可能写入当前工作目录之外")
    if impact.read_paths and not reads_stay_in_workspace(impact, cwd):
        return ExecutionDecision(AUTO_ALLOW, "命令会读取工作区外文件，但写入仍在工作区内")
    return ExecutionDecision(AUTO_ALLOW, "已识别的安全操作")


def _resolve(path: str, cwd: Path) -> Path:
    value = Path(path).expanduser()
    return value if value.is_absolute() else cwd / value


def _candidate_names(path: str, cwd: Path) -> tuple[str, ...]:
    requested = _resolve(path, cwd)
    parent = requested.parent
    if not parent.is_dir():
        return ()
    name = requested.name.casefold()
    matches = []
    for entry in parent.iterdir():
        if entry.name.casefold() == name or (not requested.suffix and entry.stem.casefold() == name):
            try:
                matches.append(str(entry.relative_to(cwd)))
            except ValueError:
                matches.append(str(entry))
    return tuple(sorted(matches, key=str.casefold))


def _positional_indexes(parts: list[str]) -> list[int]:
    indexes, after_separator = [], False
    for index, part in enumerate(parts[1:], 1):
        if part == "--" and not after_separator:
            after_separator = True
        elif after_separator or not part.startswith("-"):
            indexes.append(index)
    return indexes


def _parsed_simple(parts: list[str]) -> tuple[list[str], str, list[int]] | None:
    if not parts or parts[0] not in _PREFLIGHT_COMMANDS:
        return None
    return parts, parts[0], _positional_indexes(parts)


def _inspect_simple_file_facts(parts: list[str], cwd: str,
                               segment_index: int) -> tuple[PreflightIssue, ...]:
    simple = _parsed_simple(parts)
    if simple is None:
        return ()
    root = Path(cwd).resolve(strict=False)
    parts, program, argument_indexes = simple
    issues = []
    if program in {"cp", "mv"}:
        if len(argument_indexes) < 2:
            issues.append(PreflightIssue(
                "missing_target", 1, "", (), "复制或移动命令缺少目标路径",
                None, program, len(argument_indexes), segment_index,
            ))
            source_indexes, destination_index = argument_indexes, None
        else:
            source_indexes, destination_index = argument_indexes[:-1], argument_indexes[-1]
    else:
        source_indexes, destination_index = argument_indexes, None

    for argument_index in source_indexes:
        source = parts[argument_index]
        if any(character in source for character in "*?["):
            continue
        try:
            source_path = _resolve(source, root)
            exists = source_path.exists()
            if source_path.is_symlink() and not exists:
                issues.append(PreflightIssue(
                    "unverified_path", 1, source, (), f"路径事实无法验证：{source}",
                    argument_index, program, len(source_indexes), segment_index,
                ))
                continue
            candidates = () if exists else _candidate_names(source, root)
        except OSError:
            issues.append(PreflightIssue(
                "unverified_path", 1, source, (), f"路径事实无法验证：{source}",
                argument_index, program, len(source_indexes), segment_index,
            ))
            continue
        if not exists:
            issues.append(PreflightIssue(
                "missing_source", 1, source, candidates, f"源路径不存在：{source}",
                argument_index, program, len(source_indexes), segment_index,
            ))

    if destination_index is not None:
        destination = parts[destination_index]
        destination_path = _resolve(destination, root)
        for argument_index in source_indexes:
            source_path = _resolve(parts[argument_index], root)
            effective_target = (
                destination_path / source_path.name
                if destination_path.is_dir() or destination.endswith("/")
                else destination_path
            )
            try:
                same_path = source_path.resolve(strict=False) == effective_target.resolve(strict=False)
            except OSError:
                issues.append(PreflightIssue(
                    "unverified_path", 1, destination, (), f"路径事实无法验证：{destination}",
                    destination_index, program, len(source_indexes), segment_index,
                ))
                break
            if same_path:
                issues.append(PreflightIssue(
                    "same_source_target", 1, destination, (), "源路径和目标路径指向同一文件",
                    destination_index, program, len(source_indexes), segment_index,
                ))
                break
    return tuple(issues)


def inspect_file_facts(command: str, cwd: str,
                       parsed: _ParsedShell | None = None) -> tuple[PreflightIssue, ...]:
    parsed = parsed or _parse_shell(command)
    if parsed is None:
        return ()
    issues = []
    for segment_index, raw_segment in enumerate(parsed.segments):
        segment = list(raw_segment)
        if ">" in segment:
            if segment.count(">") != 1 or segment.index(">") != len(segment) - 2:
                continue
            segment = segment[:-2]
        issues.extend(_inspect_simple_file_facts(segment, cwd, segment_index))
    return tuple(issues)


def _programs(parsed: _ParsedShell | None) -> tuple[str, ...]:
    if parsed is None:
        return ()
    return tuple(segment[0] for segment in parsed.segments if segment)


def _render_segment(segment: tuple[str, ...] | list[str]) -> str:
    return " ".join(token if token == ">" else shlex.quote(token) for token in segment)


def rewrite_command_arguments(
    command: str, edits: tuple[tuple[int, int | None, str], ...],
) -> str | None:
    """按分段和参数位置安全重写命令；解析失败时返回 None。"""
    parsed = _parse_shell(command)
    if parsed is None:
        return None
    segments = [list(segment) for segment in parsed.segments]
    by_segment: dict[int, list[tuple[int | None, str]]] = {}
    for segment, argument_index, replacement in edits:
        if not 0 <= segment < len(segments):
            return None
        by_segment.setdefault(segment, []).append((argument_index, replacement))
    for segment_index, segment_edits in by_segment.items():
        parts = segments[segment_index]
        positional = _positional_indexes(parts)
        for argument_index, replacement in segment_edits:
            if argument_index is None:
                parts.append(replacement)
            elif not 0 <= argument_index < len(parts):
                return None
            else:
                parts[argument_index] = replacement
        if "--" not in parts and any(value.startswith("-") for _, value in segment_edits):
            parts.insert(positional[0] if positional else 1, "--")
    rendered = []
    for index, segment in enumerate(segments):
        if index:
            rendered.append(parsed.operators[index - 1])
        rendered.append(_render_segment(segment))
    return " ".join(rendered)


def _overwrite_paths(parsed: _ParsedShell | None,
                     cwd: str) -> tuple[tuple[str, ...], tuple[PreflightIssue, ...]]:
    overwrites = []
    issues = []
    if parsed is None:
        return (), ()
    excluded = {"touch", "mkdir", "chmod", "rmdir"}
    for segment in parsed.segments:
        nested = _ParsedShell(tuple(segment), (tuple(segment),))
        impact = _analyze_impact(_render_segment(segment), nested)
        if not segment or segment[0] in excluded or "delete" in impact.tags:
            continue
        for value in impact.write_paths:
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = Path(cwd) / path
            try:
                if path.exists() and path.is_file():
                    overwrites.append(str(path.resolve()))
            except OSError:
                issues.append(PreflightIssue(
                    "unverified_path", 1, value, (), f"路径事实无法验证：{value}",
                ))
    return tuple(overwrites), tuple(issues)


def review_command(command: str, cwd: str, advisory: str = SAFE) -> CommandReview:
    findings = collect_safety_findings(command)
    primary = max(findings, key=lambda item: _RISK_ORDER[item.level], default=None)
    deterministic_risk = primary.level if primary else SAFE
    safety = SafetyAssessment(
        deterministic_risk,
        primary.reason if primary else "",
        primary.rule if primary else "",
        primary.fragment if primary else "",
    )
    effective_risk = raise_risk(deterministic_risk, advisory)
    parsed = _parse_shell(command)
    impact = _analyze_impact(command, parsed)
    preflight_issues = inspect_file_facts(command, cwd, parsed)
    overwrite_paths, overwrite_issues = _overwrite_paths(parsed, cwd)
    preflight_issues = (*preflight_issues, *overwrite_issues)
    path_findings = (
        *_workspace_findings(impact, cwd),
        *_write_access_findings(impact, cwd),
        *overwrite_issues,
    )
    path_incomplete = any(
        issue.kind == "unverified_path" for issue in (*preflight_issues, *path_findings)
    )
    decision = decide_execution(
        impact, effective_risk, cwd, overwrite_paths, path_incomplete=path_incomplete,
    )
    return CommandReview(
        command=command,
        cwd=cwd,
        parse_complete=impact.known,
        programs=_programs(parsed),
        read_paths=impact.read_paths,
        write_paths=impact.write_paths,
        deterministic_risk=deterministic_risk,
        effective_risk=effective_risk,
        decision=decision,
        verification_allowed=(
            impact.known and set(impact.tags) == {"read"} and deterministic_risk == SAFE
            and not path_incomplete
        ),
        impact=impact,
        safety=safety,
        overwrite_paths=overwrite_paths,
        preflight_issues=preflight_issues,
        path_findings=path_findings,
        findings=findings,
        primary_finding=primary,
    )
