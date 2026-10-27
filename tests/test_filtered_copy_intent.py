import json
import tempfile
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.engine import Engine, _task_plan_errors
from core.intent import analyze_request
from core.task_plan import parse_operation_plan
from core.operations import prepare_plan, execute_action


class FilteredCopyIntentTests(unittest.TestCase):
    def test_named_search_directory_is_not_a_direct_copy_source(self):
        request = "备份 src 目录内过去两周修改的文件到 archive"
        raw = json.dumps({"status": "ready", "operations": ["find_files", "copy_files"],
                          "named_sources": ["src"], "selection_required": False})
        with patch("core.intent.chat", return_value=raw):
            result = analyze_request(request, [], "local", [])
        self.assertEqual(result["operations"], ["find_files", "copy_files"])

    def test_destination_is_not_treated_as_a_named_source(self):
        request = "把这里和子目录中近七天改过的 .py 复制到 backup，目录层次保持不变"
        raw = json.dumps({"status": "ready", "operations": ["find_files", "copy_files"],
                          "named_sources": ["backup"], "selection_required": False})
        with patch("core.intent.chat", return_value=raw):
            result = analyze_request(request, [], "local", [])
        self.assertEqual(result["operations"], ["find_files", "copy_files"])
        self.assertNotIn("named_sources", result)

    def test_current_directory_is_a_scope_not_a_named_file(self):
        request = "备份当前目录及子目录最近七天修改的 Python 文件到 backup，保留目录结构"
        raw = json.dumps({"status": "ready", "operations": ["find_files", "copy_files"],
                          "named_sources": ["."], "selection_required": False})
        with patch("core.intent.chat", return_value=raw):
            result = analyze_request(request, [], "local", [])
        self.assertEqual(result["operations"], ["find_files", "copy_files"])
        self.assertNotIn("named_sources", result)

    def test_direct_named_copy_keeps_existing_behavior(self):
        request = "不要移动 note.txt，只复制到 backup"
        raw = json.dumps({"status": "ready", "operations": ["find_files", "copy_files"],
                          "named_sources": ["note.txt"], "selection_required": False})
        with patch("core.intent.chat", return_value=raw):
            result = analyze_request(request, [], "local", [])
        self.assertEqual(result["operations"], ["copy_files"])

    def test_plan_cannot_drop_explicit_time_filter(self):
        request = "备份 src 目录内过去两周修改的文件到 archive"
        raw = json.dumps({"status": "ready", "steps": [{"operation": "copy_files",
                          "parameters": {"sources": ["src"], "destination": "archive"}}]})
        with tempfile.TemporaryDirectory(prefix="nl2shell-filter-test-") as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "archive").mkdir()
            with patch("core.engine.analyze_request", return_value={"status": "ready", "operations": ["copy_files"]}), \
                 patch("core.engine.chat", return_value=raw):
                with self.assertRaisesRegex(ValueError, "时间筛选"):
                    Engine(ssh_hosts=[]).generate_task_plan(request, directory)
            self.assertEqual(list((root / "archive").iterdir()), [])

    def test_time_filtered_copy_must_use_the_filtered_results(self):
        request = "备份 src 目录内过去两周修改的文件到 archive"
        find = {"operation": "find_files", "parameters": {"path": "src", "recursive": True,
                                                          "modified_within_days": 14}}
        copy = {"operation": "copy_files", "parameters": {"source_step": 1, "destination": "archive"}}
        plan = parse_operation_plan(json.dumps({"status": "ready", "steps": [find, copy]}))
        self.assertEqual(_task_plan_errors(plan, request, ".", {"find_files", "copy_files"}), [])
        copy["parameters"] = {"sources": ["src"], "destination": "archive"}
        plan = parse_operation_plan(json.dumps({"status": "ready", "steps": [find, copy]}))
        self.assertTrue(any("时间筛选" in error for error in
                            _task_plan_errors(plan, request, ".", {"find_files", "copy_files"})))

    def test_filtered_copy_only_writes_selected_files(self):
        request = "备份 src 目录内过去两周修改的文件到 archive"
        raw = json.dumps({"status": "ready", "steps": [
            {"operation": "find_files", "parameters": {"path": "src", "recursive": True,
                                                        "modified_within_days": 14}},
            {"operation": "copy_files", "parameters": {"source_step": 1, "destination": "archive"}}]})
        with tempfile.TemporaryDirectory(prefix="nl2shell-filter-test-") as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src" / "recent.txt").write_text("recent", encoding="utf-8")
            old = root / "src" / "old.txt"
            old.write_text("old", encoding="utf-8")
            old_time = time.time() - 15 * 86400
            os.utime(old, (old_time, old_time))
            plan = parse_operation_plan(raw)
            self.assertEqual(_task_plan_errors(plan, request, directory, {"find_files", "copy_files"}), [])
            prepared = prepare_plan(plan, directory)
            for action in prepared["actions"]:
                self.assertEqual(execute_action(prepared, action)["status"], "verified")
            self.assertEqual((root / "archive" / "recent.txt").read_text(encoding="utf-8"), "recent")
            self.assertFalse((root / "archive" / "old.txt").exists())
