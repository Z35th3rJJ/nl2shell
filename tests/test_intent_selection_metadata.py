"""回放真实响应检查意图字段边界；真实模型验收另行运行。"""
import json
import unittest
from unittest.mock import patch

from core.intent import analyze_request


class SelectionMetadataTests(unittest.TestCase):
    def test_recorded_find_response_keeps_operation_without_source_binding(self):
        response = {"status": "ready", "operations": ["find_files"],
                    "named_sources": ["logs/*.log"], "selection_required": False}
        attempts = []
        with patch("core.intent.chat", return_value=json.dumps(response)) as model:
            result = analyze_request("递归查找 logs 目录中名为 *.log 的普通文件", [], "local", attempts)
        self.assertEqual(result, {"status": "ready", "operations": ["find_files"]})
        self.assertEqual(model.call_count, 2)
        self.assertEqual(json.loads(attempts[0]["raw_output"]), response)
        self.assertIn("resolution", attempts[0])

    def test_count_does_not_bind_model_composed_source(self):
        response = {"status": "ready", "operations": ["count_files"],
                    "named_sources": ["logs/*.log"], "selection_required": False}
        with patch("core.intent.chat", return_value=json.dumps(response)):
            result = analyze_request("统计 logs 目录及子目录中日志文件的数量", [], "local", [])
        self.assertEqual(result, {"status": "ready", "operations": ["count_files"]})

    def test_normal_query_retains_existing_call_budget(self):
        response = {"status": "ready", "operations": ["find_files"]}
        attempts = []
        with patch("core.intent.chat", return_value=json.dumps(response)) as model:
            result = analyze_request("递归查找 logs 目录中名为 *.log 的普通文件", [], "local", attempts)
        self.assertEqual(result, response)
        self.assertEqual(model.call_count, 2)
        self.assertNotIn("resolution", attempts[0])

    def test_invented_copy_source_is_still_rejected(self):
        response = {"status": "ready", "operations": ["copy_files"],
                    "named_sources": ["invented.txt"], "selection_required": False}
        with patch("core.intent.chat", return_value=json.dumps(response)) as model:
            with self.assertRaisesRegex(ValueError, "无法取得有效的结构化意图"):
                analyze_request("将 note.txt 复制到 backup", [], "local", [])
        self.assertEqual(model.call_count, 2)

    def test_mixed_query_and_copy_still_checks_source_provenance(self):
        response = {"status": "ready", "operations": ["find_files", "copy_files"],
                    "named_sources": ["logs/*.log"], "selection_required": False}
        with patch("core.intent.chat", return_value=json.dumps(response)):
            with self.assertRaisesRegex(ValueError, "无法取得有效的结构化意图"):
                analyze_request("查找 logs 目录中的日志文件，再复制到 backup", [], "local", [])

    def test_invalid_metadata_types_are_still_rejected(self):
        responses = [
            {"status": "ready", "operations": ["find_files"], "selection_required": "false"},
            {"status": "ready", "operations": ["find_files"], "named_sources": "logs/*.log"},
        ]
        for response in responses:
            with self.subTest(response=response), patch("core.intent.chat", return_value=json.dumps(response)):
                with self.assertRaisesRegex(ValueError, "无法取得有效的结构化意图"):
                    analyze_request("递归查找 logs 目录中名为 *.log 的普通文件", [], "local", [])

    def test_explicit_copy_source_metadata_is_preserved(self):
        response = {"status": "ready", "operations": ["copy_files"],
                    "named_sources": ["note.txt"], "selection_required": False}
        with patch("core.intent.chat", return_value=json.dumps(response)):
            result = analyze_request("将 note.txt 复制到 backup", [], "local", [])
        self.assertEqual(result, response)


if __name__ == "__main__":
    unittest.main()
