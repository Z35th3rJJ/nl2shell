"""JSONL 格式的本地命令历史。"""
from datetime import datetime, timezone
import json
from pathlib import Path


def default_history_path() -> Path:
    return Path.home() / ".nl2shell" / "history.jsonl"


class HistoryStore:
    def __init__(self, path: Path | None = None):
        self.path = path or default_history_path()

    def append(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"timestamp": datetime.now(timezone.utc).isoformat(), **record}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def recent(self, limit: int = 20) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        records = []
        for line in reversed(lines):
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(records) == limit:
                break
        return records
