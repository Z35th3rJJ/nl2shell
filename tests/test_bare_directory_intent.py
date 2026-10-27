import json
import unittest
from unittest.mock import patch

from core.intent import analyze_request
from core.requirements import resolve_request


class BareDirectoryIntentTests(unittest.TestCase):
    def test_bare_listing_has_a_single_directory_operation(self):
        plan = resolve_request("列出当前目录")
        self.assertIsNotNone(plan)
        self.assertEqual([(step.operation, step.parameters) for step in plan.steps],
                         [("list_directory", {"path": ".", "include_hidden": False})])

    def test_extra_path_query_does_not_become_required(self):
        raw = json.dumps({"status": "ready", "operations": ["working_directory", "list_directory"]})
        attempts = []
        with patch("core.intent.chat", return_value=raw) as model:
            intent = analyze_request("列出当前目录", [], "local", attempts)
        self.assertEqual(intent["operations"], ["list_directory"])
        model.assert_called_once()

    def test_explicit_path_query_and_listing_keep_both_operations(self):
        raw = json.dumps({"status": "ready", "operations": ["working_directory", "list_directory"]})
        with patch("core.intent.chat", return_value=raw):
            intent = analyze_request("先显示当前路径，再列出当前目录的内容", [], "local", [])
        self.assertEqual(intent["operations"], ["working_directory", "list_directory"])

    def test_hidden_condition_is_not_lost(self):
        plan = resolve_request("列出当前目录，包括隐藏文件")
        self.assertEqual(plan.steps[0].parameters, {"path": ".", "include_hidden": True})

    def test_path_query_is_not_reinterpreted_as_listing(self):
        self.assertIsNone(resolve_request("显示当前工作目录"))

    def test_unrelated_extra_operation_is_not_removed(self):
        raw = json.dumps({"status": "ready", "operations": ["system_info", "list_directory"]})
        with patch("core.intent.chat", return_value=raw):
            with self.assertRaisesRegex(ValueError, "结构化意图"):
                analyze_request("列出当前目录", [], "local", [])
