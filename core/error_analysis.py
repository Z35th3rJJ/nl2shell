"""执行错误的确定性分类。"""
from dataclasses import dataclass
import errno
from pathlib import Path
import re
import time

from .execution import ExecutionResult


@dataclass(frozen=True)
class ErrorAnalysis:
    category: str
    summary: str
    checks: tuple[str, ...]


def classify_error(result: ExecutionResult, *, status="execution_failed", operation="",
                   error_type="", error_number=None, execution_state="", failure_phase="") -> ErrorAnalysis | None:
    if status in {"verified", "no_matches", "cancelled"}:
        return None
    if status in {"blocked", "unsupported"}:
        if failure_phase == "environment":
            return ErrorAnalysis("environment_changed", "执行前环境检查未通过，当前操作未开始", ("核对实际文件与已确认预览",))
        return ErrorAnalysis("safety_rejected", "安全审查拒绝执行", ("核对支持范围与实际预览",))
    if status == "unverified":
        return ErrorAnalysis("verification_failed", "操作已运行，但结果无法验证", ("检查输出是否截断或对象已变化", "停止，不报告任务完成"))
    if status == "interrupted":
        return ErrorAnalysis("interrupted", "用户中断，执行状态不作成功推断", ("核对已完成操作", "不要重放任务"))
    if result.timed_out:
        return ErrorAnalysis("timeout", "准备或检查超时，未开始当前操作" if execution_state == "not_started" else "操作执行或验证超时",
                             ("核对任务阶段和实际结果", "不要重放写操作"))
    if execution_state == "unknown":
        return ErrorAnalysis("state_unknown", "执行证据不完整，无法确定命令是否完成", ("保留错误和当前文件状态", "不要重放任务"))
    if result.exit_code == 0:
        return None
    if operation == "search_text" and result.exit_code == 1 and not result.stderr and not result.stdout:
        return None
    if error_number == errno.ENOENT and error_type == "ExecutionStartError":
        return ErrorAnalysis("command_not_found", "命令程序无法启动", ("检查系统命令是否存在，不自动安装",))
    numbers = {
        errno.ENOENT: ("file_not_found", "文件或路径不存在", ("核对原始路径，不猜测新路径",)),
        errno.EACCES: ("permission_denied", "权限不足", ("检查文件所有者和读取权限，不自动修改",)),
        errno.EPERM: ("permission_denied", "权限不足", ("检查文件所有者和读取权限，不自动修改",)),
        errno.EINVAL: ("invalid_argument", "命令参数无效", ("核对命令版本和获准参数，不放宽规则",)),
        errno.ENOSPC: ("disk_full", "磁盘空间不足", ("检查空间，不自动删除文件",)),
        errno.EFBIG: ("resource_limit", "写入触发文件大小限制", ("检查资源限制和已有目标文件", "不重放写操作")),
    }
    if error_number in numbers:
        return ErrorAnalysis(*numbers[error_number])
    if failure_phase == "preparation":
        return ErrorAnalysis("preparation_failed", "任务准备失败，未进入执行阶段", ("核对原始路径与文件状态",))
    if error_number in {errno.EAGAIN, errno.EINTR, errno.EMFILE, errno.ENFILE}:
        return ErrorAnalysis("transient_error", "临时资源不足或系统调用中断", ("检查资源占用", "状态明确时仅允许一次原范围重试"))
    text = (result.stderr or result.stdout).lower()
    rules = [
        (r"command not found|not recognized", "command_not_found", "命令不存在", ("检查命令拼写", "检查软件是否安装及 PATH")),
        (r"no such file|not found", "file_not_found", "文件或路径不存在", ("检查当前目录", "检查路径大小写与扩展名")),
        (r"permission denied|operation not permitted", "permission_denied", "权限不足", ("检查文件所有者和权限", "不要自动扩大权限")),
        (r"address already in use|port .* in use", "port_in_use", "端口已被占用", ("检查监听端口的进程",)),
        (r"no space left|disk full", "disk_full", "磁盘空间不足", ("检查 df -h", "检查 inode 使用量")),
        (r"network is unreachable|connection refused|could not resolve|timed out", "network_failure", "网络连接失败", ("检查网络和目标地址",)),
        (r"invalid option|unrecognized option|unknown option|usage:", "invalid_argument", "命令参数无效", ("查看命令帮助和版本",)),
        (r"resource temporarily unavailable|temporarily unavailable|interrupted system call|too many open files|input/output error",
         "transient_error", "临时资源或输入输出错误", ("检查资源占用及文件状态", "只读任务最多重试一次")),
    ]
    for pattern, category, summary, checks in rules:
        if re.search(pattern, text):
            return ErrorAnalysis(category, summary, checks)
    return ErrorAnalysis("unknown_error", "操作执行失败", ("检查错误信息与实际执行结果",))


def inspect_failure_files(prepared, outcome):
    """只观察实际源与目标，不能以此自动续写或重新执行。"""
    from .operations import workspace_path, snapshot, _check_deadline
    if prepared is None:
        return {"state": "unknown", "paths": []}
    root = Path(prepared["cwd"])
    paths, state = [], "unchanged"
    deadline = time.monotonic() + 5
    from .llm import request_deadline
    deadline = min(deadline, request_deadline.get() or deadline)
    try:
        if not root.is_dir():
            raise FileNotFoundError("工作目录已不存在")
        for action in prepared["actions"]:
            for name, previous in action.get("_identities", {}).items():
                _check_deadline(deadline)
                path = workspace_path(root, name)
                info = path.stat()
                current = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                same = tuple(previous) == current
                paths.append({"path": name, "unchanged": same})
                if not same:
                    state = "changed"
            if not prepared["read_only"]:
                for item in action["items"]:
                    _check_deadline(deadline)
                    row = {"operation": action["operation"]}
                    for key in ("source", "destination"):
                        if key in item:
                            path = workspace_path(root, item[key], internal=action["operation"] in {"trash", "restore"})
                            row[key] = item[key]
                            row[key + "_exists"] = path.exists()
                            if path.exists() and item.get("identity"):
                                row[key + "_matches_original"] = snapshot(path, deadline) == item["identity"]["content"]
                    if row.get("destination_exists"):
                        state = "possible_partial_changes"
                    paths.append(row)
        return {"state": state, "paths": paths}
    except (OSError, ValueError, RuntimeError) as error:
        return {"state": "unknown", "paths": paths, "inspection_error": str(error)}


def assess_failure(outcome, prepared):
    result = ExecutionResult(outcome.get("exit_code"), outcome.get("stdout", ""),
                             outcome.get("stderr", "") or outcome.get("detail", ""),
                             outcome.get("duration_seconds", 0), outcome.get("timed_out", False),
                             outcome.get("output_truncated", False))
    analysis = classify_error(result, status=outcome["status"], operation=outcome.get("operation", ""),
                              error_type=outcome.get("error_type", ""), error_number=outcome.get("error_number"),
                              execution_state=outcome.get("execution_state", ""), failure_phase=outcome.get("failure_phase", ""))
    if analysis is None:
        return None
    files = inspect_failure_files(prepared, outcome)
    eligible = (prepared is not None and prepared["read_only"] and analysis.category == "transient_error"
                and outcome.get("execution_state") in {"not_started", "exited"}
                and outcome.get("failure_phase") == "execution" and files["state"] == "unchanged"
                and not result.timed_out and not result.output_truncated)
    return {"category": analysis.category, "summary": analysis.summary, "checks": list(analysis.checks),
            "file_state": files, "read_only": bool(prepared and prepared["read_only"]),
            "repair_eligible": eligible, "evidence_untrusted": {
                key: outcome.get(key) for key in ("operation", "status", "execution_state", "failure_phase",
                "exit_code", "stdout", "stderr", "detail", "timed_out", "output_truncated", "error_type", "error_number", "items")}}


def safe_advice(text, diagnosis):
    if isinstance(text, str) and diagnosis.get("category") == "resource_limit" and re.search("磁盘|空间", text):
        return "；".join(diagnosis["checks"])
    if (not isinstance(text, str) or not text.strip() or re.search(
            r"\b(?:sudo|chmod|chown|apt|yum|dnf|pip|rm|curl|wget|bash|sh)\b|修改权限|安装|删除|覆盖|脚本|已修复|已成功|已经完成", text, re.I)):
        return "；".join(diagnosis["checks"])
    return text.strip()
