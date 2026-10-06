"""面向小模型的 JSON 任务计划与旧格式兼容解析。"""
from dataclasses import dataclass, field
import json
import re
import math


INTENTS = {
    "FILE_QUERY", "FILE_MODIFY", "SYSTEM_MONITOR", "PROCESS_MANAGE",
    "NETWORK_QUERY", "SOFTWARE_MANAGE", "GIT_OPERATION", "DOCKER_OPERATION",
    "COMMAND_EXPLAIN", "ERROR_FIX", "UNKNOWN",
}


def _intent(value) -> str:
    normalized = str(value or "UNKNOWN").upper()
    return normalized if normalized in INTENTS else "UNKNOWN"


def _risk(value) -> str:
    normalized = str(value or "SAFE").upper()
    return normalized if normalized in {"SAFE", "WARN", "HIGH"} else "SAFE"


@dataclass(frozen=True)
class TaskStep:
    command: str = ""
    explanation: str = ""
    expected: str = ""
    verification: str = ""
    operation: str = ""
    parameters: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TaskPlan:
    steps: tuple[TaskStep, ...]
    clarification: str = ""
    intent: str = "UNKNOWN"
    operation: str = ""
    entities: dict[str, object] = field(default_factory=dict)
    risk_advisory: str = "SAFE"
    refused: bool = False
    status: str = "ready"
    reason: str = ""


OPERATIONS = {
    "find_files", "count_files", "create_file", "create_directory", "copy_files",
    "move_files", "rename", "trash", "restore", "list_trash", "organize_files", "system_info",
}
READ_OPERATIONS = {"find_files", "count_files", "list_trash", "system_info"}


def parse_operation_plan(raw: str) -> TaskPlan:
    """主执行入口仅接受操作计划；旧命令格式不可执行。"""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I)
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("计划必须是 JSON 对象")
    if set(payload) - {"status", "clarification", "reason", "steps"}:
        raise ValueError("计划包含不支持的字段")
    status = payload.get("status")
    if status in {"need_clarification", "unsupported"}:
        key = "clarification" if status == "need_clarification" else "reason"
        message = payload.get(key)
        if set(payload) - {"status", key, "steps"} or not isinstance(message, str) or not message.strip() or payload.get("steps"):
            raise ValueError("追问或拒绝计划必须包含说明，不能包含执行步骤")
        return TaskPlan((), clarification=message.strip() if key == "clarification" else "",
                        refused=status == "unsupported", status=status,
                        reason=message.strip() if key == "reason" else "")
    steps = payload.get("steps")
    if set(payload) - {"status", "steps"} or status != "ready" or not isinstance(steps, list) or not 1 <= len(steps) <= 3:
        raise ValueError("完整计划必须包含 1 到 3 个步骤")
    result = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or set(step) - {"operation", "parameters", "explanation"}:
            raise ValueError("步骤只能包含 operation、parameters 和 explanation")
        operation, parameters = step.get("operation"), step.get("parameters")
        explanation = step.get("explanation", "")
        if not isinstance(operation, str) or operation not in OPERATIONS:
            raise ValueError("不支持的操作")
        if not isinstance(parameters, dict) or not isinstance(explanation, str):
            raise ValueError("操作参数必须是对象，说明必须是字符串")
        validate_parameters(operation, parameters, index, result)
        result.append(TaskStep(operation=operation, parameters=parameters, explanation=explanation))
    return TaskPlan(tuple(result))


def validate_parameters(operation: str, p: dict, index: int, previous) -> None:
    allowed = {
        "find_files": {"path", "recursive", "pattern", "modified_within_days", "size_gt_bytes"},
        "count_files": {"path", "recursive", "pattern", "modified_within_days", "size_gt_bytes"},
        "create_file": {"path"}, "create_directory": {"path"},
        "copy_files": {"sources", "source_step", "destination", "preserve_structure"},
        "move_files": {"sources", "source_step", "destination"},
        "rename": {"source", "destination"}, "trash": {"sources", "source_step"},
        "restore": {"trash_id"}, "list_trash": set(),
        "organize_files": {"path", "recursive", "group_by"}, "system_info": {"query"},
    }
    if operation not in allowed or set(p) - allowed[operation]:
        raise ValueError("不支持的操作参数")
    required = {
        "find_files": {"path", "recursive"}, "count_files": {"path", "recursive"},
        "create_file": {"path"}, "create_directory": {"path"},
        "copy_files": {"destination"}, "move_files": {"destination"},
        "rename": {"source", "destination"}, "restore": {"trash_id"},
        "organize_files": {"path", "recursive", "group_by"}, "system_info": {"query"},
    }
    if required.get(operation, set()) - set(p):
        raise ValueError("缺少关键操作参数，必须向用户追问")
    for key in {"path", "source", "destination", "pattern", "trash_id", "query", "group_by"} & p.keys():
        if not isinstance(p[key], str) or not p[key].strip() or "\0" in p[key]:
            raise ValueError(f"{key} 必须是非空字符串")
    for key in {"recursive", "preserve_structure"} & p.keys():
        if type(p[key]) is not bool:
            raise ValueError(f"{key} 必须是布尔值")
    for key in {"modified_within_days", "size_gt_bytes"} & p.keys():
        if type(p[key]) not in {int, float} or p[key] <= 0 or p[key] > (
            36500 if key == "modified_within_days" else 2**63 - 1
        ) or (type(p[key]) is float and not math.isfinite(p[key])):
            raise ValueError(f"{key} 必须是有限的正数")
    if operation in {"copy_files", "move_files", "trash"}:
        if ("sources" in p) == ("source_step" in p):
            raise ValueError("必须且只能提供 sources 或 source_step")
        if "sources" in p and (not isinstance(p["sources"], list) or not p["sources"] or
                               any(not isinstance(s, str) or not s.strip() or "\0" in s for s in p["sources"])):
            raise ValueError("sources 必须是非空路径列表")
        if "source_step" in p:
            ref = p["source_step"]
            if type(ref) is not int or not 1 <= ref <= index or previous[ref - 1].operation != "find_files":
                raise ValueError("source_step 只能引用前面的文件查找步骤，编号从 1 开始")
    if operation == "organize_files" and p["group_by"] != "extension":
        raise ValueError("第一版只支持按文件类型整理")
    if operation == "system_info" and p["query"] not in {"disk", "memory", "system"}:
        raise ValueError("不支持的系统查询")


def plan_payload(plan: TaskPlan) -> dict:
    if plan.refused:
        return {"status": "unsupported", "reason": plan.reason or "暂不支持这项操作"}
    if plan.clarification:
        return {"status": "need_clarification", "clarification": plan.clarification}
    return {"status": "ready", "steps": [
        {"operation": s.operation, "parameters": s.parameters, "explanation": s.explanation}
        for s in plan.steps
    ]}


def parse_task_plan(raw: str) -> TaskPlan:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.IGNORECASE)
    # 小模型偶尔只输出半个代码围栏，例如合法 JSON 末尾多一个反引号。
    text = text.strip().strip("`").strip()
    try:
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("计划必须是对象")
        clarification = payload.get("clarification", "")
        if isinstance(clarification, str) and clarification.strip():
            return TaskPlan(
                (), clarification.strip(),
                _intent(payload.get("intent")),
                str(payload.get("operation", "")),
                payload.get("entities", {}) if isinstance(payload.get("entities", {}), dict) else {},
                _risk(payload.get("risk_advisory")),
            )
        raw_steps = payload["steps"]
        if not isinstance(raw_steps, list) or len(raw_steps) > 3:
            raise ValueError("steps 必须是 1 到 3 项")
        risk = _risk(payload.get("risk_advisory"))
        intent = _intent(payload.get("intent"))
        operation = str(payload.get("operation", ""))
        entities = payload.get("entities", {})
        if not isinstance(entities, dict):
            raise ValueError("entities 必须是对象")
        if len(raw_steps) == 0:
            # 模型识别到危险操作时输出空 steps + 高 risk_advisory，表示“拒绝执行”。
            if risk in {"WARN", "HIGH"}:
                return TaskPlan((), "", intent, operation, entities, risk, refused=True)
            raise ValueError("steps 必须是 1 到 3 项")
        steps = []
        for item in raw_steps:
            if not isinstance(item, dict) or not isinstance(item.get("command"), str):
                raise ValueError("每步必须包含 command")
            values = {key: item.get(key, "") for key in ("explanation", "expected", "verification")}
            if not all(isinstance(value, str) for value in values.values()):
                raise ValueError("步骤字段必须为字符串")
            command = item["command"].strip()
            if command:
                steps.append(TaskStep(
                    command, values["explanation"].strip(),
                    values["expected"].strip(), values["verification"].strip(),
                ))
        # 小模型对危险操作的另一种表达：step 存在但 command 为空。
        # 若风险高且没有一条有效命令，同样视为“拒绝执行”。
        if not steps and risk in {"WARN", "HIGH"}:
            return TaskPlan((), "", intent, operation, entities, risk, refused=True)
        if not steps:
            raise ValueError("steps 必须是 1 到 3 项")
        return TaskPlan(
            tuple(steps), "", intent, operation, entities, risk,
        )
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        if text.lstrip().startswith(("{", "[")):
            raise ValueError("JSON 任务计划格式不合法")
        lines = raw.strip().split("\n", 1)
        command = lines[0].strip("` ")
        if not command:
            raise ValueError("无法解析任务计划")
        return TaskPlan((TaskStep(command, lines[1].strip() if len(lines) > 1 else "", "", ""),))
