"""inode 查询的公共契约及真实 Linux 命令验证。"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

from core.agent import observed_result
from core.bash_queries import prepare_queries, review_commands, execute_query
from core.execution import BashExecutor, ExecutionResult
from core.task_plan import parse_operation_plan


class FileInodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'sample.txt').write_bytes(b'inode fixture\n')

    def prepare(self, path='sample.txt', **options):
        plan = parse_operation_plan(json.dumps({'status': 'ready', 'steps': [
            {'operation': 'file_inode', 'parameters': {'path': path}}]}))
        return prepare_queries(plan, str(self.root), **options)

    def review(self, prepared, path='sample.txt'):
        return review_commands(prepared, {'commands': [{'step': 1,
            'argv': ['stat', '-c', '%i', '--', './' + path], 'explanation': '查询文件 inode'}]})

    @unittest.skipUnless(sys.platform == 'linux', '需要 Linux/GNU stat')
    def test_user_can_query_inode_with_reviewed_linux_command(self):
        plan = parse_operation_plan(json.dumps({'status': 'ready', 'steps': [
            {'operation': 'file_inode', 'parameters': {'path': 'sample.txt'}}]}))
        prepared = prepare_queries(plan, str(self.root))
        payload = {'commands': [{'step': 1, 'argv': ['stat', '-c', '%i', '--', './sample.txt'],
                                 'explanation': '查询文件 inode，不读取内容'}]}
        review_commands(prepared, payload)
        result = execute_query(prepared, prepared['actions'][0], BashExecutor())
        self.assertEqual(result['status'], 'verified', result)
        self.assertTrue(result['executed'])
        self.assertEqual(result['inode'], (self.root / 'sample.txt').stat().st_ino)
        self.assertEqual(result['actual_argv'][1:], ['-c', '%i', '--', './sample.txt'])
        self.assertEqual((self.root / 'sample.txt').read_bytes(), b'inode fixture\n')
        self.assertEqual(observed_result({'id': 'a1'}, prepared, result, False)['inode'], result['inode'])

    def test_rejects_missing_or_extra_parameters(self):
        for parameters in ({}, {'path': ''}, {'path': 'sample.txt', 'follow_links': True}):
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                parse_operation_plan(json.dumps({'status': 'ready', 'steps': [
                    {'operation': 'file_inode', 'parameters': parameters}]}))

    def test_rejects_directories_sensitive_paths_and_audit_files(self):
        (self.root / '.env').write_text('test-placeholder')
        (self.root / 'audit.jsonl').write_text('{}')
        for path in ('.', '.env', 'audit.jsonl'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.prepare(path, excluded_paths=[self.root / 'audit.jsonl'])
        with self.assertRaises(FileNotFoundError):
            self.prepare('missing.txt')

    def test_rejects_paths_outside_workspace(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / 'outside.txt'
            target.write_text('outside fixture')
            for path in (str(target), '../' + target.parent.name + '/outside.txt'):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    self.prepare(path)

    @unittest.skipUnless(sys.platform == 'linux', '需要 Linux 文件系统链接和 FIFO')
    def test_rejects_symlinks_hardlinks_and_fifo(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / 'outside.txt'
            target.write_text('outside fixture')
            (self.root / 'linked').symlink_to(target)
            (self.root / 'linked_dir').symlink_to(target.parent, target_is_directory=True)
            os.link(self.root / 'sample.txt', self.root / 'hardlink')
            os.mkfifo(self.root / 'pipe.fifo')
            for path in ('linked', 'linked_dir/outside.txt', 'hardlink', 'sample.txt', 'pipe.fifo'):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    self.prepare(path)

    def test_rejects_changed_format_path_and_extra_options(self):
        for argv in (['stat', '-L', '-c', '%i', '--', './sample.txt'],
                     ['stat', '-c', '%n', '--', './sample.txt'],
                     ['stat', '-c', '%i', '--', './other.txt']):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                review_commands(self.prepare(), {'commands': [
                    {'step': 1, 'argv': argv, 'explanation': '查询'}]})
        executor = Mock()
        result = execute_query(self.prepare(), self.prepare()['actions'][0], executor)
        self.assertFalse(result['executed'])
        executor.execute_query.assert_not_called()

    def test_changed_file_is_rejected_before_execution(self):
        prepared = self.review(self.prepare())
        (self.root / 'sample.txt').write_bytes(b'changed fixture, longer\n')
        executor = Mock()
        result = execute_query(prepared, prepared['actions'][0], executor)
        self.assertEqual(result['status'], 'blocked', result)
        self.assertFalse(result['executed'])
        executor.execute_query.assert_not_called()

    def test_wrong_inode_output_is_not_verified(self):
        prepared = self.review(self.prepare())
        executor = Mock()
        executor.execute_query.return_value = (
            ExecutionResult(0, 'not an inode\n', '', 0), ['/usr/bin/stat', '-c', '%i', '--', './sample.txt'])
        result = execute_query(prepared, prepared['actions'][0], executor)
        self.assertTrue(result['executed'])
        self.assertEqual(result['status'], 'unverified', result)
        self.assertNotIn('inode', result)

    @unittest.skipUnless(sys.platform == 'linux', '需要 Linux/GNU stat')
    def test_binary_file_with_special_name_uses_metadata_only(self):
        name = '--项目 文件.bin'
        path = self.root / name
        path.write_bytes(b'\0\xff' * (1024 * 1024))
        path.chmod(0)
        self.addCleanup(path.chmod, 0o600)
        prepared = self.review(self.prepare(name), name)
        result = execute_query(prepared, prepared['actions'][0], BashExecutor())
        self.assertEqual(result['status'], 'verified', result)
        self.assertEqual(result['inode'], path.stat().st_ino)


if __name__ == '__main__':
    unittest.main()
