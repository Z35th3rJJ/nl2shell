"""JSONL 格式的本地命令历史。"""
from datetime import datetime, timezone
import csv
import json
from pathlib import Path
from uuid import uuid4
from .redaction import redact_value


def default_history_path() -> Path:
    return Path.home() / ".nl2shell" / "history.jsonl"


class HistoryStore:
    def __init__(self, path: Path | None = None):
        self.path = path or default_history_path()

    def append(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = redact_value({
            "record_id": uuid4().hex,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **record,
        })
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def recent(self, limit: int = 20) -> list[dict]:
        return self.query(limit=limit)

    def query(self, limit: int | None = 20, *, status: str | None = None,
              batch_id: str | None = None, since: str | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        records = []
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if status and record.get("status") != status:
                continue
            if batch_id and record.get("batch_id") != batch_id:
                continue
            if since and record.get("timestamp", "") < since:
                continue
            records.append(record)
            if limit is not None and len(records) == limit:
                break
        return records

    def find(self, record_id: str) -> dict | None:
        for record in self.query(limit=None):
            if record.get("record_id") == record_id:
                return record
        return None

    def export(self, records: list[dict], fmt: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "jsonl":
            with path.open("w", encoding="utf-8") as file:
                for record in records:
                    file.write(json.dumps(record, ensure_ascii=False) + "\n")
            return
        if fmt != "csv":
            raise ValueError("导出格式只能是 jsonl 或 csv")
        fields = ("record_id", "timestamp", "input", "command", "cwd", "status", "risk", "executed", "run_mode", "batch_id", "batch_index", "timed_out")
        with path.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(records)
