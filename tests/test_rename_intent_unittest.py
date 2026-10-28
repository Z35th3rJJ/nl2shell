import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.engine import Engine, _task_plan_errors
from core.operations import prepare_plan, execute_action
from core.task_plan import TaskPlan, TaskStep


class RenameIntentTests(unittest.TestCase):
    def test_complete_target_path_is_preserved_and_other_path_is_rejected(self):
        request = '将 archive/old.txt 重命名为 other/new.txt'
        with tempfile.TemporaryDirectory() as directory:
            good = TaskPlan((TaskStep(operation='rename', parameters={'source': 'archive/old.txt', 'destination': 'other/new.txt'}),))
            bad = TaskPlan((TaskStep(operation='rename', parameters={'source': 'archive/old.txt', 'destination': 'new.txt'}),))
            self.assertEqual(_task_plan_errors(good, request, directory, {'rename'}), [])
            self.assertTrue(_task_plan_errors(bad, request, directory, {'rename'}))

    def test_wrong_source_or_operation_is_rejected(self):
        request = '将 archive/old.txt 重命名为 restored.txt'
        with tempfile.TemporaryDirectory() as directory:
            for step in (TaskStep(operation='rename', parameters={'source': 'other.txt', 'destination': 'archive/restored.txt'}),
                         TaskStep(operation='copy_files', parameters={'sources': ['archive/old.txt'], 'destination': 'archive'})):
                self.assertTrue(_task_plan_errors(TaskPlan((step,)), request, directory, {step.operation}))

    def test_existing_target_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'archive').mkdir()
            (root / 'archive/old.txt').write_text('source', encoding='utf8')
            (root / 'archive/restored.txt').write_text('keep', encoding='utf8')
            plan = TaskPlan((TaskStep(operation='rename', parameters={'source': 'archive/old.txt', 'destination': 'archive/restored.txt'}),))
            prepared = prepare_plan(plan, directory)
            self.assertNotEqual(Path(prepared['actions'][0]['items'][0]['destination']).as_posix(), 'archive/restored.txt')
            self.assertEqual(execute_action(prepared, prepared['actions'][0])['status'], 'verified')
            self.assertEqual((root / 'archive/restored.txt').read_text(encoding='utf8'), 'keep')

    def test_outside_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = TaskPlan((TaskStep(operation='rename', parameters={'source': 'archive/old.txt', 'destination': '../outside.txt'}),))
            with self.assertRaises(ValueError):
                _task_plan_errors(plan, '将 archive/old.txt 重命名为 ../outside.txt', directory, {'rename'})

    def test_bare_new_name_stays_in_source_directory(self):
        request = '将 archive/old.txt 重命名为 restored.txt'
        raw = json.dumps({'status': 'ready', 'steps': [{'operation': 'rename', 'parameters': {
            'source': 'archive/old.txt', 'destination': 'restored.txt'}}]})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'archive').mkdir()
            (root / 'archive/old.txt').write_text('original', encoding='utf8')
            with patch('core.engine.analyze_request', return_value={'status': 'ready', 'operations': ['rename']}), \
                 patch('core.engine.chat', return_value=raw):
                plan = Engine(ssh_hosts=[]).generate_task_plan(request, directory)
            prepared = prepare_plan(plan, directory)
            item = prepared['actions'][0]['items'][0]
            self.assertEqual(Path(item['destination']).as_posix(), 'archive/restored.txt')
            self.assertFalse((root / 'archive/restored.txt').exists())
            self.assertEqual(execute_action(prepared, prepared['actions'][0])['status'], 'verified')
            self.assertEqual((root / 'archive/restored.txt').read_text(encoding='utf8'), 'original')
            self.assertFalse((root / 'restored.txt').exists())


if __name__ == '__main__':
    unittest.main()
