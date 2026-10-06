"""固定文件操作、工作区保护及独立结果检查；不执行模型生成的 Shell。"""
from datetime import datetime, timezone
import ctypes
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
from uuid import uuid4

from .task_plan import READ_OPERATIONS, TaskPlan, validate_parameters

TRASH = ".nl2shell-trash"


def workspace_path(root: Path, value: str, *, internal: bool = False) -> Path:
    root = root.expanduser().resolve(strict=True)
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError("文件路径无效")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        raise ValueError("路径位于工作目录之外") from None
    if not internal and TRASH in relative.parts:
        raise ValueError("回收区只能通过查看或恢复操作访问")
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("第一版不支持符号链接")
    if candidate.resolve(strict=False) != candidate:
        raise ValueError("路径包含重定向或符号链接")
    return candidate


def file_digest(path: Path, deadline: float | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            _check_deadline(deadline)
            digest.update(chunk)
    return digest.hexdigest()


def _check_deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("文件操作超时，后续步骤已停止")


def snapshot(path: Path, deadline=None) -> list:
    """同时检查特殊文件和目录内链接，防止复制时跟随链接。"""
    paths = [path]
    result = []
    while paths:
        _check_deadline(deadline)
        current = paths.pop()
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError("只支持普通文件和目录，不支持链接或设备文件")
        name = str(current.relative_to(path))
        if current.is_dir():
            result.append([name, "directory"])
            paths.extend(sorted(current.iterdir(), reverse=True))
        else:
            result.append([name, "file", info.st_size, file_digest(current, deadline)])
    return sorted(result)


def _identity(path: Path, deadline=None) -> dict:
    info = path.lstat()
    return {"device": info.st_dev, "inode": info.st_ino, "modified_ns": info.st_mtime_ns,
            "content": snapshot(path, deadline)}


def unique_target(path: Path, reserved: set[Path]) -> Path:
    suffix = "".join(path.suffixes) if not path.is_dir() else ""
    stem = path.name[:-len(suffix)] if suffix else path.name
    number = 0
    candidate = path
    while candidate.exists() or candidate.is_symlink() or candidate in reserved:
        number += 1
        candidate = path.with_name(f"{stem}_{number}{suffix}")
    reserved.add(candidate)
    return candidate


def find_files(root: Path, p: dict, now: float, excluded=(), deadline=None) -> list[Path]:
    start = workspace_path(root, p["path"])
    if not start.is_dir():
        raise ValueError("查找路径必须是已有目录")
    found, directories = [], [start]
    while directories:
        _check_deadline(deadline)
        directory = directories.pop()
        for path in sorted(directory.iterdir()):
            _check_deadline(deadline)
            if path.name == TRASH or any(path == target or target in path.parents for target in excluded):
                continue
            if path.is_symlink():
                raise ValueError("查找范围包含符号链接，请先选择不含链接的目录")
            if path.is_dir():
                if p["recursive"]:
                    directories.append(path)
                continue
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("查找范围包含设备或其他特殊文件")
            if not fnmatch.fnmatchcase(path.name, p.get("pattern", "*")):
                continue
            if "modified_within_days" in p and not now - p["modified_within_days"] * 86400 <= info.st_mtime <= now:
                continue
            if "size_gt_bytes" in p and info.st_size <= p["size_gt_bytes"]:
                continue
            found.append(path)
    return sorted(found)


def trash_entries(root: Path) -> list[dict]:
    trash = workspace_path(root, TRASH, internal=True)
    if not trash.exists():
        return []
    if not trash.is_dir():
        raise ValueError("回收区路径不是目录")
    entries = []
    for entry in sorted(trash.iterdir()):
        if not re.fullmatch(r"[0-9a-f]{32}", entry.name):
            continue
        record = workspace_path(root, str(entry / "record.json"), internal=True)
        payload = workspace_path(root, str(entry / "data"), internal=True)
        if not payload.exists():
            continue
        data = json.loads(record.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("id") != entry.name or not isinstance(data.get("original"), str):
            raise ValueError("回收记录无效；文件保留在回收区")
        workspace_path(root, data["original"])
        entries.append(data)
    return entries


def prepare_plan(plan: TaskPlan, cwd: str, *, now=None, timeout_seconds=60, excluded_paths=()) -> dict:
    """只读预览：冻结文件清单和实际目标名称。"""
    root = Path(cwd).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("工作目录无效")
    if plan.refused or plan.clarification or not 1 <= len(plan.steps) <= 3:
        raise ValueError("计划尚不可执行")
    now = time.time() if now is None else now
    deadline = time.monotonic() + timeout_seconds
    reserved, selections, actions = set(), {}, []
    excluded = [Path(path).resolve(strict=False) for path in excluded_paths]
    for step in plan.steps:
        if step.operation == "copy_files" and "source_step" in step.parameters:
            excluded.append(workspace_path(root, step.parameters["destination"]))
    for index, step in enumerate(plan.steps, 1):
        _check_deadline(deadline)
        if step.command or step.verification:
            raise ValueError("第一版不执行自由 Shell 命令或模型验证命令")
        validate_parameters(step.operation, step.parameters, index - 1, plan.steps[:index - 1])
        p, operation = step.parameters, step.operation
        action = {"step": index, "operation": operation, "parameters": p,
                  "explanation": step.explanation, "items": [], "files": []}
        if operation in {"find_files", "count_files", "organize_files"}:
            query = p if operation != "organize_files" else {"path": p["path"], "recursive": p["recursive"]}
            files = find_files(root, query, now, excluded, deadline)
            selections[index] = (files, workspace_path(root, p["path"]))
            action["files"] = [str(path.relative_to(root)) for path in files]
            action["identities"] = [_identity(path, deadline) for path in files]
            if operation != "organize_files":
                actions.append(action)
                continue
            sources = files
        elif operation in {"copy_files", "move_files", "trash"}:
            if "source_step" in p:
                sources, base = selections[p["source_step"]]
            else:
                sources = [workspace_path(root, source) for source in p["sources"]]
                base = root
            if len(set(sources)) != len(sources):
                raise ValueError("源路径重复")
            if any(a != b and a in b.parents for a in sources for b in sources):
                raise ValueError("源路径不能同时包含父目录及其子文件")
        elif operation in {"create_file", "create_directory"}:
            destination = workspace_path(root, p["path"])
            if destination == root:
                raise ValueError("不能替换工作目录本身")
            destination = unique_target(destination, reserved)
            action["items"] = [{"destination": str(destination.relative_to(root))}]
            actions.append(action)
            continue
        elif operation == "rename":
            sources = [workspace_path(root, p["source"])]
        elif operation == "restore":
            identifier = p["trash_id"]
            if not re.fullmatch(r"[0-9a-f]{32}", identifier):
                raise ValueError("恢复编号无效")
            entry = next((item for item in trash_entries(root) if item["id"] == identifier), None)
            if entry is None:
                raise ValueError("未找到可恢复的文件")
            source = workspace_path(root, f"{TRASH}/{identifier}/data", internal=True)
            destination = unique_target(workspace_path(root, entry["original"]), reserved)
            action["items"] = [{"source": str(source.relative_to(root)),
                                "destination": str(destination.relative_to(root)),
                                "identity": _identity(source, deadline), "trash_id": identifier}]
            actions.append(action)
            continue
        elif operation == "list_trash":
            action["entries"] = trash_entries(root)
            actions.append(action)
            continue
        elif operation == "system_info":
            actions.append(action)
            continue
        else:
            raise ValueError("不支持的操作")

        for source in sources:
            source = workspace_path(root, str(source))
            if source == root:
                raise ValueError("不能移动、复制或删除工作目录本身")
            identity = _identity(source, deadline)
            item = {"source": str(source.relative_to(root)), "identity": identity}
            if operation == "trash":
                identifier = uuid4().hex
                item["trash_id"] = identifier
                destination = workspace_path(root, f"{TRASH}/{identifier}/data", internal=True)
            elif operation == "organize_files":
                group = source.suffix.lower().lstrip(".") or "no_extension"
                if not re.fullmatch(r"[a-z0-9_-]+", group):
                    raise ValueError("文件扩展名不适合用作分类目录")
                if source.parent.name == group:
                    continue
                destination = source.parent / group / source.name
                destination = unique_target(workspace_path(root, str(destination)), reserved)
            elif operation == "rename":
                destination = unique_target(workspace_path(root, p["destination"]), reserved)
            else:
                directory = workspace_path(root, p["destination"])
                if directory.exists() and not directory.is_dir():
                    raise ValueError("复制或移动的 destination 必须是目录")
                relative = source.relative_to(base) if p.get("preserve_structure") else Path(source.name)
                destination = unique_target(workspace_path(root, str(directory / relative)), reserved)
            if source == destination or source in destination.parents:
                raise ValueError("目标不能位于源目录内部")
            item["destination"] = str(destination.relative_to(root))
            action["items"].append(item)
        actions.append(action)
    return {"cwd": str(root), "now": now, "actions": actions, "excluded": [str(path) for path in excluded],
            "read_only": all(step.operation in READ_OPERATIONS for step in plan.steps)}


def preview_details(prepared: dict) -> list[dict]:
    return [{key: value for key, value in action.items() if key not in {"identities", "items"}} |
            {"items": [{key: value for key, value in item.items() if key != "identity"}
                       for item in action["items"]]} for action in prepared["actions"]]


def preparation_changed(old: dict, new: dict) -> bool:
    # 回收编号是随机的，授权比较只看路径、文件事实和其他参数。
    def comparable(prepared):
        actions = []
        for action in prepared["actions"]:
            action = dict(action, items=[dict(item) for item in action["items"]])
            if action["operation"] == "trash":
                for item in action["items"]:
                    item.pop("trash_id", None)
                    item.pop("destination", None)
            actions.append(action)
        return actions
    return comparable(old) != comparable(new)


def _copy_file(source: Path, destination: Path, deadline):
    # 独占创建：即使确认后出现同名文件，也不会覆盖。
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
            _check_deadline(deadline)
            outgoing.write(chunk)
    shutil.copystat(source, destination, follow_symlinks=False)


def _copy(source: Path, destination: Path, deadline):
    mode = source.lstat().st_mode
    if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
        raise ValueError("复制期间出现链接或特殊文件，已停止")
    if source.is_dir():
        destination.mkdir()
        for child in sorted(source.iterdir()):
            _check_deadline(deadline)
            if child.is_symlink():
                raise ValueError("复制期间出现符号链接，已停止")
            _copy(child, destination / child.name, deadline)
    else:
        _copy_file(source, destination, deadline)


def _move_no_replace(source: Path, destination: Path):
    if os.name == "nt":
        os.rename(source, destination)  # Windows 的 rename 在目标存在时失败。
        return
    # Linux 原生的“不替换目标”重命名，避免检查与移动之间出现同名文件。
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise RuntimeError("当前系统不支持安全移动；需要 Linux renameat2")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(destination))


def execute_action(prepared: dict, action: dict, executor=None, *, timeout_seconds=60) -> dict:
    root = Path(prepared["cwd"])
    operation = action["operation"]
    deadline = time.monotonic() + timeout_seconds
    outcome = {"step": action["step"], "operation": operation, "status": "verified",
               "detail": "实际结果检查通过", "items": [], "executed": False}
    started = time.monotonic()
    try:
        _check_deadline(deadline)
        if operation in {"find_files", "count_files"}:
            outcome["executed"] = True
            current = find_files(root, action["parameters"], prepared["now"],
                                 [Path(path) for path in prepared["excluded"]], deadline)
            if [str(path.relative_to(root)) for path in current] != action["files"]:
                raise ValueError("查询文件清单已变化，请重新确认")
            outcome["files"] = action["files"]
            outcome["count"] = len(action["files"])
            for filename, identity in zip(action["files"], action["identities"]):
                if _identity(workspace_path(root, filename), deadline) != identity:
                    raise ValueError("查询结果已变化，请重新生成任务")
        elif operation == "list_trash":
            outcome["entries"] = trash_entries(root)
            outcome["executed"] = True
        elif operation == "system_info":
            commands = {"disk": "df -h", "memory": "free -h", "system": "uname -a"}
            if executor is None or not executor.is_available():
                raise RuntimeError("系统查询需要可用的 Linux Bash")
            result = executor.execute(commands[action["parameters"]["query"]], cwd=str(root),
                                      timeout_seconds=timeout_seconds)
            outcome.update(executed=True, stdout=result.stdout, stderr=result.stderr,
                           timed_out=result.timed_out, output_truncated=result.output_truncated)
            if result.timed_out or result.exit_code != 0:
                raise RuntimeError("系统查询失败或超时")
            if result.output_truncated or not result.stdout.strip():
                outcome.update(status="unverified", detail="查询输出为空或被截断，尚未验证")
        for item in action["items"]:
            _check_deadline(deadline)
            progress = {key: value for key, value in item.items() if key != "identity"}
            progress["status"] = "pending"
            outcome["items"].append(progress)
            destination = workspace_path(root, item["destination"], internal=operation == "trash")
            if destination.exists() or destination.is_symlink():
                raise FileExistsError("目标已存在，请重新确认新名称")
            source = None
            if "source" in item:
                source = workspace_path(root, item["source"], internal=operation == "restore")
                if _identity(source, deadline) != item["identity"]:
                    raise ValueError("源文件已变化，请重新确认")
            if operation == "trash":
                destination.parent.mkdir(parents=True, exist_ok=False)
                record = {"id": item["trash_id"], "original": item["source"],
                          "timestamp": datetime.now(timezone.utc).isoformat()}
                record_path = workspace_path(root, str(destination.parent / "record.json"), internal=True)
                with record_path.open("x", encoding="utf-8") as file:
                    json.dump(record, file, ensure_ascii=False)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
            workspace_path(root, str(destination), internal=operation == "trash")
            outcome["executed"] = True
            progress["status"] = "executing"
            if operation == "create_file":
                with destination.open("xb"):
                    pass
                valid = destination.is_file() and destination.stat().st_size == 0
            elif operation == "create_directory":
                destination.mkdir()
                valid = destination.is_dir()
            elif operation == "copy_files":
                _copy(source, destination, deadline)
                valid = snapshot(destination, deadline) == item["identity"]["content"]
            else:
                # ponytail: 同一工作区内重命名；跨文件系统移动明确失败，不做隐式复制删除。
                _move_no_replace(source, destination)
                valid = not source.exists() and snapshot(destination, deadline) == item["identity"]["content"]
            if not valid:
                raise RuntimeError("文件操作完成，但实际结果检查失败")
            progress["status"] = "verified"
        if action["items"] == [] and operation not in READ_OPERATIONS:
            outcome["detail"] = "没有符合条件的文件，未修改任何文件"
    except (OSError, ValueError, RuntimeError, KeyboardInterrupt) as error:
        outcome.update(status="execution_failed", detail=str(error), timed_out=isinstance(error, TimeoutError))
        if isinstance(error, KeyboardInterrupt):
            outcome.update(status="interrupted", detail="用户中断；已完成操作保留，未完成操作停止")
        if outcome["items"]:
            outcome["items"][-1]["status"] = "failed"
    outcome["duration_seconds"] = time.monotonic() - started
    return outcome
