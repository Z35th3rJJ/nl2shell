"""在调用 Bash 前用本地文件系统校验模型生成的文件命令。"""
from dataclasses import dataclass
from pathlib import Path
import shlex

from .task_plan import TaskPlan


SUPPORTED_COMMANDS = {"cp", "mv", "cat", "rm"}


@dataclass(frozen=True)
class PreflightIssue:
    kind: str
    step: int
    path: str
    candidates: tuple[str, ...]
    message: str


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


def _arguments(command: str) -> tuple[str, list[str]] | None:
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if not parts or parts[0] not in SUPPORTED_COMMANDS:
        return None
    return parts[0], [part for part in parts[1:] if not part.startswith("-")]


def inspect_plan(plan: TaskPlan, cwd: str) -> PreflightReport:
    root = Path(cwd).resolve(strict=False)
    issues: list[PreflightIssue] = []
    for index, step in enumerate(plan.steps, 1):
        parsed = _arguments(step.command)
        if parsed is None:
            continue
        program, arguments = parsed
        if program in {"cp", "mv"}:
            if len(arguments) < 2:
                issues.append(PreflightIssue("missing_target", index, "", (), "复制或移动命令缺少目标路径"))
                continue
            sources, destination = arguments[:-1], arguments[-1]
        else:
            sources, destination = arguments, ""

        for source in sources:
            if any(character in source for character in "*?["):
                continue
            source_path = _resolve(source, root)
            if not source_path.exists():
                candidates = _candidate_names(source, root)
                message = f"源路径不存在：{source}"
                issues.append(PreflightIssue("missing_source", index, source, candidates, message))

        if program in {"cp", "mv"} and sources:
            destination_path = _resolve(destination, root)
            for source in sources:
                source_path = _resolve(source, root)
                effective_target = destination_path / source_path.name if destination_path.is_dir() or destination.endswith("/") else destination_path
                if source_path.resolve(strict=False) == effective_target.resolve(strict=False):
                    issues.append(PreflightIssue("same_source_target", index, destination, (), "源路径和目标路径指向同一文件"))
                    break
    return PreflightReport(tuple(issues))
