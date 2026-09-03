"""面向小模型的 JSON 任务计划与旧格式兼容解析。"""
from dataclasses import dataclass, field
import json
import re


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
    command: str
    explanation: str
    expected: str
    verification: str


@dataclass(frozen=True)
class TaskPlan:
    steps: tuple[TaskStep, ...]
    clarification: str = ""
    intent: str = "UNKNOWN"
    operation: str = ""
    entities: dict[str, object] = field(default_factory=dict)
    risk_advisory: str = "SAFE"
    refused: bool = False


def parse_task_plan(raw: str) -> TaskPlan:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.IGNORECASE)
    # 小模型偶尔只输出半个代码围栏，例如合法 JSON 末尾多一个反引号。
    text = text.strip().strip("`").strip()
    try:
        payload = json.loads(text)
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
