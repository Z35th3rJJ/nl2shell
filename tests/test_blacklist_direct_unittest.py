import json
import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import Mock, patch

from cli import execute_request
from core.engine import Engine
from core.history import HistoryStore
from core import command_policy
from core.execution import BashExecutor, ExecutionResult


class DirectBlacklistTests(unittest.TestCase):
    def test_read_request_does_not_enter_plan_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(ssh_hosts=[])
            engine.command_policy = 'blacklist'
            raw = json.dumps({'status': 'ready', 'argv': ['pwd'], 'explanation': '显示路径'})
            with patch('core.command_policy.chat', return_value=raw), \
                 patch.object(engine, 'generate_task_plan', side_effect=AssertionError('不应生成计划')), \
                 patch('core.command_policy.sandbox_argv', return_value=['sandbox']):
                status = execute_request(engine, Mock(), HistoryStore(Path(directory) / 'history.jsonl'),
                                         '列出当前目录的内容', directory, 'confirm', input_fn=lambda _: 'no')
            self.assertEqual(status, 'cancelled')

    def test_write_commands_are_direct_and_non_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            argv, writable = command_policy.review_command(['cp', 'source.txt', 'backup.txt'], directory)
            self.assertTrue(writable)
            self.assertEqual(argv[0], 'cp')
            self.assertIn('--no-clobber', argv)
            self.assertEqual(argv[-2:], ['source.txt', 'backup.txt'])

    def test_blacklist_still_blocks_danger_and_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            for argv in (['rm', '-rf', '.'], ['cp', '-f', 'a', 'b'], ['cp', 'a', '-f', 'b'],
                         ['mv', 'a', '../outside'], ['cp', 'a', '.env'], ['mkdir', '../outside']):
                with self.subTest(argv=argv), self.assertRaises(ValueError):
                    command_policy.review_command(argv, directory)

    def test_builtin_recovery_is_not_routed_to_structured_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(ssh_hosts=[])
            engine.command_policy = 'blacklist'
            history = HistoryStore(Path(directory) / 'history.jsonl')
            with patch.object(engine, 'generate_task_plan', side_effect=AssertionError('不应生成计划')), \
                 patch('core.command_policy.chat', side_effect=AssertionError('内置命令不应调用模型')):
                status = execute_request(engine, Mock(), history, '/trash', directory, 'confirm')
            self.assertEqual(status, 'unsupported')
            self.assertFalse(history.last_record['executed'])

    def test_copy_is_not_executed_when_confirmation_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'source.txt').write_text('data', encoding='utf8')
            engine = Engine(ssh_hosts=[])
            engine.command_policy = 'blacklist'
            executor = Mock()
            raw = json.dumps({'status': 'ready', 'argv': ['cp', 'source.txt', 'backup.txt'], 'explanation': '复制'})
            with patch('core.command_policy.chat', return_value=raw), \
                 patch('core.command_policy.sandbox_argv', return_value=['sandbox']), \
                 patch.object(engine, 'generate_task_plan', side_effect=AssertionError('不应生成计划')):
                status = execute_request(engine, executor, HistoryStore(root / 'history.jsonl'),
                                         '复制 source.txt 到 backup.txt', directory, 'confirm', input_fn=lambda _: 'no')
            self.assertEqual(status, 'cancelled')
            executor.execute_policy_command.assert_not_called()
            self.assertFalse((root / 'backup.txt').exists())

    def test_confirmed_write_reports_unverified_and_unknown_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'source.txt').write_text('data', encoding='utf8')
            engine = Engine(ssh_hosts=[])
            engine.command_policy = 'blacklist'
            executor = Mock()
            executor.execute_policy_command.return_value = (ExecutionResult(0, '', '', 0.01, process_state='exited'), ['sandbox'])
            history = HistoryStore(root / 'history.jsonl')
            raw = json.dumps({'status': 'ready', 'argv': ['cp', 'source.txt', 'backup.txt'], 'explanation': '复制'})
            with patch('core.command_policy.chat', return_value=raw), \
                 patch('core.command_policy.sandbox_argv', return_value=['sandbox']), \
                 patch.object(engine, 'generate_task_plan', side_effect=AssertionError('不应生成计划')):
                status = execute_request(engine, executor, history, '复制文件', directory, 'confirm', input_fn=lambda _: 'yes')
            self.assertEqual(status, 'unverified')
            self.assertTrue(history.last_record['executed'])
            self.assertEqual(history.last_record['file_change_state'], 'unknown')
            self.assertEqual(executor.execute_policy_command.call_args.args[0],
                             ['cp', '--no-clobber', '--', 'source.txt', 'backup.txt'])

    def test_placeholder_is_not_asked_as_a_real_question(self):
        raw = json.dumps({'status': 'need_clarification', 'clarification': '具体问题'})
        with patch('core.command_policy.chat', return_value=raw), self.assertRaises(ValueError):
            command_policy.generate_candidate('显示当前目录', '.', [], 'local')

    @unittest.skipUnless(sys.platform == 'linux', '需要 Linux 沙箱')
    def test_native_copy_and_overwrite_protection_in_sandbox(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'source.txt').write_text('new', encoding='utf8')
            (root / 'existing.txt').write_text('original', encoding='utf8')
            executor = BashExecutor()
            result, launcher = executor.execute_policy_command(['cp', 'source.txt', 'copy.txt'], directory)
            self.assertEqual(result.exit_code, 0, result.stderr)
            self.assertEqual((root / 'copy.txt').read_text(encoding='utf8'), 'new')
            self.assertIn('/usr/bin/bwrap', launcher)
            result, _ = executor.execute_policy_command(['cp', 'source.txt', 'existing.txt'], directory)
            self.assertEqual((root / 'existing.txt').read_text(encoding='utf8'), 'original')


if __name__ == '__main__':
    unittest.main()
