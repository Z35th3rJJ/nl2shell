"""可选 JSONL 运行日志；默认关闭并对常见敏感字段脱敏。"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from .redaction import redact_value


def log_event(event: str, **fields) -> None:
    destination = os.environ.get("NL2SHELL_LOG_JSON", "").strip()
    if not destination:
        return
    payload = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event}
    for key, value in fields.items():
        if key.lower() in {"api_key", "token", "password", "secret", "environment"}:
            continue
        payload[key] = redact_value(value)
    path = Path(destination).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False) + "\n")
