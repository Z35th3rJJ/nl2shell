"""明确禁止自动改名时，冲突必须在预览和写入之前停止。"""
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from cli import execute_request
from core.engine import Engine
from core.history import HistoryStore
from core.operations import prepare_plan, execute_action
from core.requirements import forbids_automatic_rename
from core.task_plan import TaskPlan, TaskStep


class ConflictIntentTests(unittest.TestCase):
    def test_user_forbids_auto_rename_stops_before_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'backup').mkdir()
            (root / 'sample.txt').write_bytes(b'hello\n')
            (root / 'backup/protected.txt').write_bytes(b'original target\n')
            plan = TaskPlan((TaskStep(operation='copy_files', parameters={
                'sources': ['sample.txt'], 'destination': 'backup', 'destination_name': 'protected.txt'}),))
            engine = Engine(ssh_hosts=[])
            history = HistoryStore(root / 'audit.jsonl')
            confirm = Mock(return_value='no')
            request = '将 sample.txt 复制到 backup 目录并命名为 protected.txt，不要覆盖已有文件，也不要自动改名'
            with patch.object(engine, 'generate_task_plan', return_value=plan), redirect_stdout(io.StringIO()) as terminal:
                status = execute_request(engine, Mock(), history, request, directory, 'confirm', input_fn=confirm)
            self.assertEqual(status, 'blocked')
            confirm.assert_not_called()
            self.assertNotIn('01 / 执行预览', terminal.getvalue())
            self.assertIn('不自动改名', terminal.getvalue())
            self.assertEqual(history.last_record['phase'], 'preparation')
            self.assertEqual(history.last_record['execution_state'], 'not_started')
            self.assertEqual(history.last_record['file_change_state'], 'unchanged')
            self.assertFalse(history.last_record['executed'])
            self.assertFalse(history.last_record['authorized'])
            self.assertEqual((root / 'sample.txt').read_bytes(), b'hello\n')
            self.assertEqual((root / 'backup/protected.txt').read_bytes(), b'original target\n')
            self.assertEqual([p.name for p in (root / 'backup').iterdir()], ['protected.txt'])

    def test_clarification_constraint_reaches_preparation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'sample.txt').write_bytes(b'source')
            (root / 'target.txt').write_bytes(b'keep')
            plan = TaskPlan((TaskStep(operation='copy_files', parameters={
                'sources': ['sample.txt'], 'destination': '.', 'destination_name': 'target.txt'}),))
            engine = Engine(ssh_hosts=[])
            history = HistoryStore(root / 'audit.jsonl')
            answer = Mock(return_value='不要自动改名')
            with patch.object(engine, 'generate_task_plan', side_effect=[
                    TaskPlan((), clarification='目标冲突时如何处理？'), plan]), redirect_stdout(io.StringIO()):
                status = execute_request(engine, Mock(), history, '将 sample.txt 复制为 target.txt',
                                         directory, 'confirm', input_fn=answer)
            self.assertEqual(status, 'blocked')
            self.assertEqual(answer.call_count, 1)
            self.assertIn('用户回答：不要自动改名', history.last_record['clarifications'][0])
            self.assertEqual(sorted(p.name for p in root.glob('target*')), ['target.txt'])
            self.assertEqual((root / 'target.txt').read_bytes(), b'keep')

    def test_new_conflict_during_confirmation_does_not_trigger_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'sample.txt').write_bytes(b'source')
            plan = TaskPlan((TaskStep(operation='copy_files', parameters={
                'sources': ['sample.txt'], 'destination': '.', 'destination_name': 'target.txt'}),))
            engine = Engine(ssh_hosts=[])
            history = HistoryStore(root / 'audit.jsonl')
            def confirm(_):
                self.assertFalse((root / 'target.txt').exists())
                (root / 'target.txt').write_bytes(b'created by another process')
                return 'yes'
            answer = Mock(side_effect=confirm)
            with patch.object(engine, 'generate_task_plan', return_value=plan), redirect_stdout(io.StringIO()):
                status = execute_request(engine, Mock(), history, '复制 sample.txt，不要自动改名',
                                         directory, 'confirm', input_fn=answer)
            self.assertEqual(status, 'blocked')
            self.assertEqual(answer.call_count, 1)
            self.assertEqual(history.last_record['execution_state'], 'not_started')
            self.assertEqual(history.last_record['file_change_state'], 'unchanged')
            self.assertEqual((root / 'target.txt').read_bytes(), b'created by another process')
            self.assertFalse((root / 'target_1.txt').exists())

    def test_plan_internal_collision_stops_all_steps_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'one.txt').write_bytes(b'one')
            (root / 'two.txt').write_bytes(b'two')
            plan = TaskPlan(tuple(TaskStep(operation='copy_files', parameters={
                'sources': [source], 'destination': '.', 'destination_name': 'target.txt'})
                for source in ['one.txt', 'two.txt']))
            with self.assertRaises(FileExistsError):
                prepare_plan(plan, directory, allow_auto_rename=False)
            self.assertEqual(sorted(p.name for p in root.iterdir()), ['one.txt', 'two.txt'])

    def test_no_conflict_preserves_name_and_still_copies(self):
        for operation in ['copy_files', 'move_files']:
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'source.txt').write_bytes(b'source')
                plan = TaskPlan((TaskStep(operation=operation, parameters={
                    'sources': ['source.txt'], 'destination': 'backup', 'destination_name': 'target.txt'}),))
                prepared = prepare_plan(plan, directory, allow_auto_rename=False)
                self.assertEqual(Path(prepared['actions'][0]['items'][0]['destination']).as_posix(), 'backup/target.txt')
                self.assertFalse((root / 'backup').exists())
                self.assertEqual(execute_action(prepared, prepared['actions'][0])['status'], 'verified')
                self.assertEqual((root / 'backup/target.txt').read_bytes(), b'source')
                self.assertEqual((root / 'source.txt').exists(), operation == 'copy_files')

    def test_default_conflict_handling_is_preserved(self):
        for operation in ['copy_files', 'move_files', 'rename', 'create_file', 'create_directory']:
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'source.txt').write_bytes(b'source')
                (root / 'target.txt').write_bytes(b'keep')
                parameters = ({'sources': ['source.txt'], 'destination': '.', 'destination_name': 'target.txt'}
                              if operation in {'copy_files', 'move_files'} else
                              {'source': 'source.txt', 'destination': 'target.txt'} if operation == 'rename' else
                              {'path': 'target.txt'})
                plan = TaskPlan((TaskStep(operation=operation, parameters=parameters),))
                prepared = prepare_plan(plan, directory)
                self.assertEqual(Path(prepared['actions'][0]['items'][0]['destination']).as_posix(), 'target_1.txt')
                with self.assertRaises(FileExistsError):
                    prepare_plan(plan, directory, allow_auto_rename=False)
                self.assertEqual((root / 'source.txt').read_bytes(), b'source')
                self.assertEqual((root / 'target.txt').read_bytes(), b'keep')

    def test_only_user_conditions_disable_automatic_rename(self):
        for text in ['不要自动改名', '禁止重命名', '不允许自动改名', '保留文件名，不要改名']:
            with self.subTest(text=text):
                self.assertTrue(forbids_automatic_rename(text))
        self.assertTrue(forbids_automatic_rename('复制文件', ['问题：冲突怎么办？；用户回答：不要自动改名']))
        self.assertFalse(forbids_automatic_rename('复制文件', ['问题：是否不要自动改名？；用户回答：允许自动改名']))
        self.assertFalse(forbids_automatic_rename('将 "不要自动改名.txt" 复制到 backup 目录'))
        self.assertFalse(forbids_automatic_rename('将 source.txt 复制到 backup，不要覆盖'))


if __name__ == '__main__':
    unittest.main()
