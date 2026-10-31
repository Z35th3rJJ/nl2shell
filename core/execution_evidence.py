"""汇总本任务的启动证据与业务文件修改证据，不从计划或错误文案推断。"""
from .task_plan import READ_OPERATIONS


def summarize_execution(outcomes, repairs=()):
    attempts = list(outcomes)
    for repair in repairs:
        for key in ("initial_outcome", "outcome"):
            if repair.get(key):
                attempts.append(repair[key])
    executed = any(row.get("executed") is True for row in attempts)
    unknown = any(row.get("execution_state", "started" if row.get("executed") else "unknown") == "unknown"
                  for row in attempts)
    state = "unknown" if unknown else "not_started" if not executed else (
        "completed" if outcomes and all(
            not row.get("timed_out") and (
                row.get("executed") is True and row.get("execution_state") in {"completed", "exited"}
                or "execution_state" not in row and row.get("status") in {"verified", "no_matches"})
            for row in outcomes) else "stopped")
    changes = []
    for row in attempts:
        change = row.get("file_change_state")
        if change is None:
            change = ("unchanged" if row.get("execution_state") == "not_started"
                      or row.get("operation") in READ_OPERATIONS else "unknown")
        changes.append(change)
    change = "changed" if "changed" in changes else "unknown" if "unknown" in changes else "unchanged"
    return {"executed": executed, "execution_state": state, "file_change_state": change}


def execution_description(evidence):
    state = evidence.get("execution_state")
    if state == "not_started":
        return "未执行（未进入执行阶段）"
    if state == "unknown":
        return "已执行部分操作；其余执行状态不确定" if evidence.get("executed") else "无法确定是否已执行"
    if state == "completed":
        return "已执行（执行过程已结束，任务结果见验证状态）"
    if evidence.get("executed"):
        return "已执行，请核对各步结果"
    return "未确认执行（旧记录缺少启动证据）"


def change_description(evidence):
    return {
        "unchanged": "本任务未执行文件写入",
        "changed": "已确认发生文件修改，请核对各项结果；未完成项不得自动重放",
        "unknown": "无法确定是否发生文件修改，请核对实际文件状态；不得自动重放",
    }[evidence["file_change_state"]]
