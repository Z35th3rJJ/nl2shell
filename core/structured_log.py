"""可选 JSONL 运行日志；默认关闭并对常见敏感字段脱敏。"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re


_SECRET = re.compile(r"(?i)(api[_-]?key|token|password|secret)\s*[=:]\s*[^\s]+")


def _redact(value: str) -> str:
    return _SECRET.sub(lambda match: match.group(0).split(match.group(1), 1)[0] + match.group(1) + "=[REDACTED]", value)


def log_event(event: str, **fields) -> None:
    destination = os.environ.get("NL2SHELL_LOG_JSON", "").strip()
    if not destination:
        return
    payload = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event}
    for key, value in fields.items():
        if key.lower() in {"api_key", "token", "password", "secret", "environment"}:
            continue
        payload[key] = _redact(value) if isinstance(value, str) else value
    path = Path(destination).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False) + "\n")
