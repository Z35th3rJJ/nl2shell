"""最大文件查询：通过公共接口核对范围、命令及实际结果。"""
import json
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from cli import show_outcome
from core.agent import observed_result
from core.bash_queries import command_request, prepare_queries, review_commands, execute_query, same_query_plan
from core.execution import BashExecutor, ExecutionResult
from core.engine import Engine
from core.requirements import resolve_request
from core.task_plan import parse_operation_plan


class LargestFileTests(unittest.TestCase):
    def plan(self, path='.', recursive=False):
        return parse_operation_plan(json.dumps({'status': 'ready', 'steps': [
            {'operation': 'largest_file', 'parameters': {'path': path, 'recursive': recursive}}]}))

    def prepared(self, root, path='.', recursive=False, **options):
        plan = self.plan(path, recursive)
        prepared = prepare_queries(plan, str(root), **options)
        rules = json.loads(command_request(plan, prepared)[1]['content'])['rules'][0]
        review_commands(prepared, {'commands': [{'step': 1, 'argv': rules['required_argv'], 'explanation': '核对最大文件'}]})
        return prepared

    def test_plan_supports_explicit_directory_and_scope(self):
        plan = parse_operation_plan(json.dumps({'status': 'ready', 'steps': [
            {'operation': 'largest_file', 'parameters': {'path': '.', 'recursive': False}}]}))
        self.assertEqual(plan.steps[0].operation, 'largest_file')
        self.assertEqual(plan.steps[0].parameters, {'path': '.', 'recursive': False})

    @unittest.skipUnless(sys.platform == 'linux', '需要 Linux/GNU find')
    def test_command_result_matches_largest_regular_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'small.txt').write_bytes(b'small')
            (root/'large.bin').write_bytes(b'\0\xff' * 500)
            plan = parse_operation_plan(json.dumps({'status': 'ready', 'steps': [
                {'operation': 'largest_file', 'parameters': {'path': '.', 'recursive': False}}]}))
            prepared = prepare_queries(plan, directory)
            review_commands(prepared, {'commands': [{'step': 1,
                'argv': prepared['actions'][0]['_required_argv'], 'explanation': '读取大小，核对最大文件'}]})
            result = execute_query(prepared, prepared['actions'][0], BashExecutor())
            self.assertEqual(result['status'], 'verified', result)
            self.assertEqual(result['largest_files'], [{'path': 'large.bin', 'size_bytes': 1000}])
            self.assertEqual((root/'large.bin').read_bytes(), b'\0\xff' * 500)

    @unittest.skipUnless(sys.platform == 'linux', '需要 Linux/GNU find')
    def test_scope_ties_and_user_visible_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'sub').mkdir()
            (root/'项目 文件.bin').write_bytes(b'A' * 100)
            (root/'--large.bin').write_bytes(b'B' * 100)
            (root/'sub/bigger.bin').write_bytes(b'C' * 200)
            prepared = self.prepared(root)
            result = execute_query(prepared, prepared['actions'][0], BashExecutor())
            self.assertEqual(result['status'], 'verified', result)
            self.assertEqual({item['path'] for item in result['largest_files']}, {'项目 文件.bin', '--large.bin'})
            self.assertEqual({item['size_bytes'] for item in result['largest_files']}, {100})
            visible = io.StringIO()
            with contextlib.redirect_stdout(visible):
                show_outcome(result)
            self.assertIn('最大文件：', visible.getvalue())
            self.assertIn('项目 文件.bin', visible.getvalue())
            self.assertIn('--large.bin', visible.getvalue())
            self.assertNotIn('\0', visible.getvalue())
            self.assertEqual(observed_result({'id': 'a1'}, prepared, result, False)['largest_files'], result['largest_files'])
            prepared = self.prepared(root, recursive=True)
            result = execute_query(prepared, prepared['actions'][0], BashExecutor())
            self.assertEqual(result['largest_files'], [{'path': 'sub/bigger.bin', 'size_bytes': 200}])

    @unittest.skipUnless(sys.platform == 'linux', '需要 Linux/GNU find')
    def test_empty_directory_and_zero_byte_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = self.prepared(root)
            result = execute_query(prepared, prepared['actions'][0], BashExecutor())
            self.assertEqual(result['status'], 'no_matches', result)
            self.assertEqual(result['largest_files'], [])
            for name in ('a.txt', 'b.txt'):
                (root/name).touch()
            prepared = self.prepared(root)
            result = execute_query(prepared, prepared['actions'][0], BashExecutor())
            self.assertEqual(result['status'], 'verified', result)
            self.assertEqual(len(result['largest_files']), 2)
            self.assertEqual({item['size_bytes'] for item in result['largest_files']}, {0})

    def test_strict_parameters_and_reference_scope(self):
        for parameters in ({'path': '.'}, {'path': '.', 'recursive': 'false'},
                           {'path': '.', 'recursive': False, 'pattern': '*.log'}):
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                parse_operation_plan(json.dumps({'status': 'ready', 'steps': [
                    {'operation': 'largest_file', 'parameters': parameters}]}))
        self.assertFalse(same_query_plan(self.plan(), self.plan(recursive=True)))
        self.assertFalse(same_query_plan(self.plan(), self.plan(path='other')))

    def test_original_request_controls_scope_in_all_stages(self):
        for request, path, recursive in (('找一下当前目录下最大的文件', '.', False),
                                          ('查找 tie 目录中最大的文件', 'tie', False),
                                          ('查找 logs 目录及子目录中最大的文件', 'logs', True),
                                          ('查找“日志 目录”目录当前层最大的文件', '日志 目录', False)):
            with self.subTest(request=request):
                plan = resolve_request(request)
                self.assertIsNotNone(plan)
                self.assertEqual(plan.steps[0].parameters, {'path': path, 'recursive': recursive})
        for request in ('查找 logs 目录及子目录当前层最大的文件',
                        '查找 logs 目录中最大的文件，并复制到 backup',
                        '查找 logs 目录中最大的三个文件',
                        '查找 logs 目录中最近三天最大的文件'):
            with self.subTest(request=request):
                self.assertIsNone(resolve_request(request))
        self.assertIsNone(resolve_request('查找 logs 目录中最大的文件', ['问题：范围？；用户回答：其他']))
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'tie').mkdir()
            wrong = json.dumps({'status': 'ready', 'steps': [{'operation': 'largest_file',
                'parameters': {'path': 'tie', 'recursive': True}}]})
            intent = json.dumps({'status': 'ready', 'operations': ['largest_file']})
            with patch('core.intent.chat', return_value=intent), patch('core.engine.chat', return_value=wrong) as model:
                engine = Engine(ssh_hosts=[])
                with self.assertRaisesRegex(ValueError, '原始需求及统一默认规则不一致'):
                    engine.generate_task_plan('查找 tie 目录中最大的文件', directory)
                self.assertLessEqual(model.call_count, 2)
                self.assertTrue(engine.plan_attempts[0]['validation_errors'])

    def test_planner_receives_request_facts_and_returns_checked_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'tie').mkdir()
            intent = json.dumps({'status': 'ready', 'operations': ['largest_file']})
            response = json.dumps({'status': 'ready', 'steps': [{'operation': 'largest_file',
                'parameters': {'path': 'tie', 'recursive': False}}]}, ensure_ascii=False)
            def model(messages, **options):
                system = messages[0]['content']
                self.assertIn('以下完整需求约束由原始请求及统一规则解析', system)
                self.assertIn('"path": "tie", "recursive": false', system)
                return response
            with patch('core.intent.chat', return_value=intent), patch('core.engine.chat', side_effect=model) as planner:
                engine = Engine(ssh_hosts=[])
                plan = engine.generate_task_plan('查找 tie 目录中最大的文件', directory)
                self.assertEqual(plan.status, 'ready')
                self.assertEqual(plan.steps[0].parameters, {'path': 'tie', 'recursive': False})
                self.assertEqual(planner.call_count, 1)

    @unittest.skipUnless(sys.platform == 'linux', '需要 Linux/GNU find')
    def test_sensitive_and_audit_files_do_not_become_largest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'ordinary.bin').write_bytes(b'\0' * 10)
            for name in ('.env', 'audit.jsonl', 'secret.key'):
                (root/name).write_bytes(b'X' * 1000)
            prepared = self.prepared(root, excluded_paths=[root/'audit.jsonl'])
            result = execute_query(prepared, prepared['actions'][0], BashExecutor())
            self.assertEqual(result['status'], 'verified', result)
            self.assertEqual(result['largest_files'], [{'path': 'ordinary.bin', 'size_bytes': 10}])

    def test_invalid_paths_and_unreviewed_or_changed_plan_do_not_execute(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            for path in (outside, '../outside'):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    self.prepared(root, path)
            (root/'a.txt').write_bytes(b'a')
            executor = Mock()
            prepared = prepare_queries(self.plan(), directory)
            result = execute_query(prepared, prepared['actions'][0], executor)
            self.assertFalse(result['executed'])
            prepared = self.prepared(root)
            (root/'a.txt').write_bytes(b'changed size')
            result = execute_query(prepared, prepared['actions'][0], executor)
            self.assertEqual(result['status'], 'blocked', result)
            executor.execute_query.assert_not_called()

    @unittest.skipUnless(sys.platform == 'linux', '需要 Linux 文件系统链接')
    def test_symlink_hardlink_and_unsafe_candidate_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            link = root/'linked'
            link.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                self.prepared(root, recursive=True)
            link.unlink()
            (root/'a.txt').write_bytes(b'fixture')
            (root/'hard.txt').hardlink_to(root/'a.txt')
            with self.assertRaises(ValueError):
                self.prepared(root)
            (root/'hard.txt').unlink()
            prepared = self.prepared(root)
            with self.assertRaises(ValueError):
                review_commands(prepared, {'commands': [{'step': 1,
                    'argv': ['find', '.', '-type', 'f', '-delete'], 'explanation': '不可执行'}]})
            executor = Mock()
            self.assertFalse(execute_query(prepared, prepared['actions'][0], executor)['executed'])
            executor.execute_query.assert_not_called()

    def test_false_or_incomplete_output_cannot_publish_largest_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'a.txt').write_bytes(b'fixture')
            for output, stderr, truncated in (('999 ./a.txt\0', '', False),
                                               ('7 ./a.txt\0', 'warning', False),
                                               ('7 ./a.txt\0', '', True)):
                with self.subTest(output=output, stderr=stderr, truncated=truncated):
                    prepared = self.prepared(root)
                    executor = Mock()
                    executor.execute_query.return_value = (
                        ExecutionResult(0, output, stderr, 0, output_truncated=truncated), ['/usr/bin/find'])
                    result = execute_query(prepared, prepared['actions'][0], executor)
                    self.assertEqual(result['status'], 'unverified', result)
                    self.assertNotIn('largest_files', result)

    @unittest.skipUnless(sys.platform == 'linux', '需要 Linux/GNU find、stat')
    def test_existing_find_count_and_inode_commands_still_verify(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'a.txt').write_bytes(b'fixture')
            for operation, parameters in (('find_files', {'path': '.', 'recursive': False}),
                                           ('count_files', {'path': '.', 'recursive': False}),
                                           ('file_inode', {'path': 'a.txt'})):
                with self.subTest(operation=operation):
                    plan = parse_operation_plan(json.dumps({'status': 'ready', 'steps': [
                        {'operation': operation, 'parameters': parameters}]}))
                    prepared = prepare_queries(plan, directory)
                    review_commands(prepared, {'commands': [{'step': 1,
                        'argv': prepared['actions'][0]['_required_argv'], 'explanation': '回归原有只读查询'}]})
                    result = execute_query(prepared, prepared['actions'][0], BashExecutor())
                    self.assertEqual(result['status'], 'verified', result)


if __name__ == '__main__':
    unittest.main()
