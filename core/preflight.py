"""在调用 Bash 前用本地文件系统校验并修正简单文件命令。"""
from dataclasses import dataclass, replace
from pathlib import Path
import shlex

from .command_review import PreflightIssue, review_command, rewrite_command_arguments
from .task_plan import TaskPlan, TaskStep


SUPPORTED_COMMANDS = {"cp", "mv", "cat", "rm"}


@dataclass(frozen=True)
class CommandEdit:
    step: int
    argument_index: int | None
    replacement: str
    segment: int = 0


@dataclass(frozen=True)
class PreflightReport:
    issues: tuple[PreflightIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues


def _resolve(path: str, cwd: Path) -> Path:
    value = Path(path).expanduser()
    return value if value.is_absolute() else cwd / value


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
    issues = []
    for step_index, step in enumerate(plan.steps, 1):
        review = review_command(step.command, cwd)
        issues.extend(replace(issue, step=step_index) for issue in review.preflight_issues)
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
        command = rewrite_command_arguments(
            step.command,
            tuple((edit.segment, edit.argument_index, edit.replacement) for edit in step_edits),
        )
        if command is None:
            steps.append(step)
            continue
        steps.append(TaskStep(command, step.explanation, step.expected, step.verification))
    return replace(plan, steps=tuple(steps))


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
