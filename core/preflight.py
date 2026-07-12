"""在调用 Bash 前用本地文件系统校验并修正简单文件命令。"""
from dataclasses import dataclass
from pathlib import Path
import shlex

from .task_plan import TaskPlan, TaskStep


SUPPORTED_COMMANDS = {"cp", "mv", "cat", "rm"}


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


@dataclass(frozen=True)
class CommandEdit:
    step: int
    argument_index: int | None
    replacement: str


@dataclass(frozen=True)
class PreflightReport:
    issues: tuple[PreflightIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues


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
        exact = entry.name.casefold() == name
        omitted_extension = not requested.suffix and entry.stem.casefold() == name
        if exact or omitted_extension:
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


def _parsed(command: str) -> tuple[list[str], str, list[int]] | None:
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if not parts or parts[0] not in SUPPORTED_COMMANDS:
        return None
    return parts, parts[0], _positional_indexes(parts)


def inspect_plan(plan: TaskPlan, cwd: str) -> PreflightReport:
    root = Path(cwd).resolve(strict=False)
    issues: list[PreflightIssue] = []
    for step_index, step in enumerate(plan.steps, 1):
        parsed = _parsed(step.command)
        if parsed is None:
            continue
        parts, program, argument_indexes = parsed
        if program in {"cp", "mv"}:
            if len(argument_indexes) < 2:
                issues.append(PreflightIssue("missing_target", step_index, "", (), "复制或移动命令缺少目标路径", None, program, len(argument_indexes)))
                source_indexes, destination_index = argument_indexes, None
            else:
                source_indexes, destination_index = argument_indexes[:-1], argument_indexes[-1]
        else:
            source_indexes, destination_index = argument_indexes, None

        for argument_index in source_indexes:
            source = parts[argument_index]
            if any(character in source for character in "*?["):
                continue
            source_path = _resolve(source, root)
            if not source_path.exists():
                issues.append(PreflightIssue(
                    "missing_source", step_index, source, _candidate_names(source, root),
                    f"源路径不存在：{source}", argument_index, program, len(source_indexes),
                ))

        if destination_index is not None:
            destination = parts[destination_index]
            destination_path = _resolve(destination, root)
            for argument_index in source_indexes:
                source_path = _resolve(parts[argument_index], root)
                effective_target = destination_path / source_path.name if destination_path.is_dir() or destination.endswith("/") else destination_path
                if source_path.resolve(strict=False) == effective_target.resolve(strict=False):
                    issues.append(PreflightIssue(
                        "same_source_target", step_index, destination, (), "源路径和目标路径指向同一文件",
                        destination_index, program, len(source_indexes),
                    ))
                    break
    return PreflightReport(tuple(issues))


def apply_edits(plan: TaskPlan, edits: list[CommandEdit]) -> TaskPlan:
    """只对已解析参数做替换或追加，再以 Bash 安全引用重新拼接。"""
    by_step: dict[int, list[CommandEdit]] = {}
    for edit in edits:
        by_step.setdefault(edit.step, []).append(edit)
    steps = []
    for step_index, step in enumerate(plan.steps, 1):
        step_edits = by_step.get(step_index, [])
        if not step_edits:
            steps.append(step)
            continue
        parsed = _parsed(step.command)
        if parsed is None:
            steps.append(step)
            continue
        parts, _, positional = parsed
        for edit in step_edits:
            if edit.argument_index is None:
                parts.append(edit.replacement)
            else:
                parts[edit.argument_index] = edit.replacement
        if "--" not in parts and any(edit.replacement.startswith("-") for edit in step_edits):
            parts.insert(positional[0] if positional else 1, "--")
        steps.append(TaskStep(shlex.join(parts), step.explanation, step.expected, step.verification))
    return TaskPlan(tuple(steps), plan.clarification)


def default_copy_target(source: str, cwd: str) -> str | None:
    """仅为当前工作目录中的单个文件生成不冲突的默认副本名。"""
    path = _resolve(source, Path(cwd).resolve(strict=False))
    if not path.is_file():
        return None
    suffix = "".join(path.suffixes)
    stem = path.name[:-len(suffix)] if suffix else path.name
    destination_root = Path(cwd).resolve(strict=False)
    for number in range(1, 1000):
        marker = "_copy" if number == 1 else f"_copy_{number}"
        candidate = destination_root / f"{stem}{marker}{suffix}"
        if not candidate.exists():
            return candidate.name
    return None


def default_target_for_step(plan: TaskPlan, step: int, cwd: str) -> str | None:
    parsed = _parsed(plan.steps[step - 1].command)
    if parsed is None:
        return None
    parts, program, argument_indexes = parsed
    if program != "cp" or not argument_indexes:
        return None
    source_indexes = argument_indexes if len(argument_indexes) == 1 else argument_indexes[:-1]
    if len(source_indexes) != 1:
        return None
    return default_copy_target(parts[source_indexes[0]], cwd)
