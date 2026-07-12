from core.history import HistoryStore


def test_history_persists_and_reads_latest_records(tmp_path):
    store = HistoryStore(tmp_path / "history.jsonl")
    store.append({"input": "查看文件", "command": "ls", "executed": True})
    store.append({"input": "查看目录", "command": "pwd", "executed": False})

    records = store.recent(1)

    assert len(records) == 1
    assert records[0]["command"] == "pwd"
    assert records[0]["timestamp"]
    assert records[0]["record_id"]


def test_history_filters_and_exports_records(tmp_path):
    store = HistoryStore(tmp_path / "history.jsonl")
    store.append({"input": "a", "command": "ls", "status": "verified", "batch_id": "b1"})
    store.append({"input": "b", "command": "false", "status": "command_failed", "batch_id": "b2"})

    records = store.query(limit=None, status="verified", batch_id="b1")
    csv_path = tmp_path / "history.csv"
    store.export(records, "csv", csv_path)

    assert len(records) == 1
    assert store.find(records[0]["record_id"])["input"] == "a"
    assert "record_id" in csv_path.read_text(encoding="utf-8")
