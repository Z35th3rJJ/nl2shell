from core.history import HistoryStore


def test_history_persists_and_reads_latest_records(tmp_path):
    store = HistoryStore(tmp_path / "history.jsonl")
    store.append({"input": "查看文件", "command": "ls", "executed": True})
    store.append({"input": "查看目录", "command": "pwd", "executed": False})

    records = store.recent(1)

    assert len(records) == 1
    assert records[0]["command"] == "pwd"
    assert records[0]["timestamp"]
