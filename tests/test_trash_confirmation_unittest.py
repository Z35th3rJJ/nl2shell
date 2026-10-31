"""回收编号必须与用户确认的预览一致，不能在确认后悄悄更换。"""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch

from cli import execute_request
from core.engine import Engine
from core.history import HistoryStore
from core.operations import prepare_plan, preparation_changed, execute_action
from core.task_plan import TaskPlan, TaskStep


class TrashConfirmationTests(unittest.TestCase):
    def test_confirmed_preview_matches_execution_and_recovery_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'renamed.txt').write_bytes(b'hello\n')
            plan = TaskPlan((TaskStep(operation='trash', parameters={'sources': ['renamed.txt']}),))
            engine = Engine(ssh_hosts=[])
            history = HistoryStore(root / 'audit.jsonl')
            identifiers = []
            with redirect_stdout(io.StringIO()) as terminal:
                def confirm(_):
                    identifiers.append(re.findall(r'\.nl2shell-trash[\\/]([0-9a-f]{32})[\\/]data', terminal.getvalue())[-1])
                    self.assertEqual((root / 'renamed.txt').read_bytes(), b'hello\n')
                    self.assertFalse((root / '.nl2shell-trash').exists())
                    return 'yes'
                with patch.object(engine, 'generate_task_plan', return_value=plan):
                    status = execute_request(engine, Mock(), history, '将 renamed.txt 放入回收区',
                                             directory, 'confirm', input_fn=confirm)
            self.assertEqual(status, 'verified')
            self.assertEqual(len(identifiers), 1)
            item = history.last_record['verification'][0]['items'][0]
            self.assertEqual(item['trash_id'], identifiers[0])
            self.assertEqual(Path(item['destination']).as_posix(), f'.nl2shell-trash/{identifiers[0]}/data')
            self.assertEqual(set(re.findall(r'\.nl2shell-trash[\\/]([0-9a-f]{32})[\\/]data', terminal.getvalue())), set(identifiers))
            entry = root / '.nl2shell-trash' / identifiers[0]
            self.assertEqual((entry / 'data').read_bytes(), b'hello\n')
            self.assertEqual(json.loads((entry / 'record.json').read_text(encoding='utf8'))['id'], identifiers[0])
            self.assertFalse((root / 'renamed.txt').exists())

    def test_occupied_preview_entry_stops_without_changing_identifier(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'file.txt').write_bytes(b'source')
            plan = TaskPlan((TaskStep(operation='trash', parameters={'sources': ['file.txt']}),))
            engine = Engine(ssh_hosts=[])
            history = HistoryStore(root / 'audit.jsonl')
            identifiers = []
            with redirect_stdout(io.StringIO()) as terminal:
                def confirm(_):
                    identifier = re.findall(r'\.nl2shell-trash[\\/]([0-9a-f]{32})[\\/]data', terminal.getvalue())[-1]
                    identifiers.append(identifier)
                    entry = root / '.nl2shell-trash' / identifier
                    entry.mkdir(parents=True)
                    (entry / 'data').write_bytes(b'other process')
                    return 'yes'
                with patch.object(engine, 'generate_task_plan', return_value=plan):
                    status = execute_request(engine, Mock(), history, '将 file.txt 放入回收区',
                                             directory, 'confirm', input_fn=confirm)
            self.assertEqual(status, 'blocked')
            self.assertEqual(len(identifiers), 1)
            self.assertEqual((root / 'file.txt').read_bytes(), b'source')
            self.assertEqual((root / '.nl2shell-trash' / identifiers[0] / 'data').read_bytes(), b'other process')
            self.assertEqual(len(list((root / '.nl2shell-trash').iterdir())), 1)
            self.assertEqual(history.last_record['execution_state'], 'not_started')
            self.assertEqual(history.last_record['file_change_state'], 'unchanged')

    def test_source_change_reconfirms_with_the_same_identifier(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'file.txt').write_bytes(b'source')
            plan = TaskPlan((TaskStep(operation='trash', parameters={'sources': ['file.txt']}),))
            engine = Engine(ssh_hosts=[])
            history = HistoryStore(root / 'audit.jsonl')
            identifiers = []
            with redirect_stdout(io.StringIO()) as terminal:
                def confirm(_):
                    identifiers.append(re.findall(r'\.nl2shell-trash[\\/]([0-9a-f]{32})[\\/]data', terminal.getvalue())[-1])
                    if len(identifiers) == 1:
                        (root / 'file.txt').write_bytes(b'changed by another process')
                        return 'yes'
                    return 'no'
                with patch.object(engine, 'generate_task_plan', return_value=plan):
                    status = execute_request(engine, Mock(), history, '将 file.txt 放入回收区',
                                             directory, 'confirm', input_fn=confirm)
            self.assertEqual(status, 'cancelled')
            self.assertEqual(len(identifiers), 2)
            self.assertEqual(identifiers[0], identifiers[1])
            self.assertEqual((root / 'file.txt').read_bytes(), b'changed by another process')
            self.assertFalse((root / '.nl2shell-trash').exists())

    def test_independent_steps_keep_distinct_ids_and_new_tasks_get_new_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ['a.txt', 'b.txt']:
                (root / name).write_bytes(name.encode())
            plan = TaskPlan(tuple(TaskStep(operation='trash', parameters={'sources': [name]}) for name in ['a.txt', 'b.txt']))
            first = prepare_plan(plan, directory)
            second = prepare_plan(plan, directory, now=first['now'], previous=first)
            self.assertFalse(preparation_changed(first, second))
            ids = [action['items'][0]['trash_id'] for action in first['actions']]
            self.assertEqual(len(set(ids)), 2)
            independent = prepare_plan(plan, directory)
            self.assertTrue(preparation_changed(first, independent))
            self.assertFalse((root / '.nl2shell-trash').exists())

    def test_tampered_previous_context_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'file.txt').write_bytes(b'source')
            plan = TaskPlan((TaskStep(operation='trash', parameters={'sources': ['file.txt']}),))
            first = prepare_plan(plan, directory)
            first['actions'][0]['items'][0]['trash_id'] = '../outside'
            with self.assertRaisesRegex(ValueError, '编号无效'):
                prepare_plan(plan, directory, previous=first)
            first['cwd'] = 'other-workspace'
            with self.assertRaisesRegex(ValueError, '其他工作目录'):
                prepare_plan(plan, directory, previous=first)
            self.assertFalse((root / '.nl2shell-trash').exists())

    def test_cancelled_trash_creates_no_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'file.txt').write_bytes(b'source')
            engine = Engine(ssh_hosts=[])
            plan = TaskPlan((TaskStep(operation='trash', parameters={'sources': ['file.txt']}),))
            with patch.object(engine, 'generate_task_plan', return_value=plan), redirect_stdout(io.StringIO()):
                status = execute_request(engine, Mock(), HistoryStore(root / 'audit.jsonl'),
                                         '将 file.txt 放入回收区', directory, 'confirm', input_fn=lambda _: 'no')
            self.assertEqual(status, 'cancelled')
            self.assertEqual((root / 'file.txt').read_bytes(), b'source')
            self.assertFalse((root / '.nl2shell-trash').exists())

    def test_preview_identifier_can_restore_original_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'file.txt').write_bytes(b'source')
            plan = TaskPlan((TaskStep(operation='trash', parameters={'sources': ['file.txt']}),))
            first = prepare_plan(plan, directory)
            confirmed = prepare_plan(plan, directory, previous=first)
            identifier = first['actions'][0]['items'][0]['trash_id']
            outcome = execute_action(confirmed, confirmed['actions'][0])
            self.assertEqual(outcome['status'], 'verified')
            restore = prepare_plan(TaskPlan((TaskStep(operation='restore', parameters={'trash_id': identifier}),)), directory)
            self.assertEqual(execute_action(restore, restore['actions'][0])['status'], 'verified')
            self.assertEqual((root / 'file.txt').read_bytes(), b'source')
            self.assertFalse((root / '.nl2shell-trash' / identifier / 'data').exists())


if __name__ == '__main__':
    unittest.main()
