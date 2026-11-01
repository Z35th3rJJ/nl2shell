import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cli import execute_request
from core.engine import Engine
from core.history import HistoryStore
from core.requirements import resolve_request
from core.task_plan import TaskPlan, TaskStep


class CreateDirectoryIntentTests(unittest.TestCase):
    def test_explicit_names_and_paired_quotes_are_preserved(self):
        for path_text, name in [('语音预览取消', '语音预览取消'), ('new-dir', 'new-dir'),
                            ('"项目 文件"', '项目 文件'), ('“语音的目录”', '语音的目录'),
                            ('"项目\'资料"', "项目'资料"), ("'项目\"资料'", '项目"资料')]:
            with self.subTest(path_text=path_text):
                plan = resolve_request('在当前目录创建一个名为' + path_text + '的目录。')
                self.assertEqual([(step.operation, step.parameters) for step in plan.steps],
                                 [('create_directory', {'path': name})])

    def test_partial_requests_and_additional_conditions_are_not_consumed(self):
        for request in ['不要在当前目录创建一个名为测试的目录。',
                        '在当前目录创建一个名为“项目 文件的目录。',
                        '在当前目录创建一个名为测试的目录，然后删除 sample.txt',
                        '在当前目录创建一个名为测试的目录，权限设为 777',
                        '在当前目录创建一个目录。']:
            with self.subTest(request=request):
                self.assertIsNone(resolve_request(request))
        self.assertIsNone(resolve_request('在当前目录创建一个名为测试的目录。', ['用户回答：其他']))

    def test_explicit_name_survives_model_refusal_after_previous_creation(self):
        request = '在当前目录创建一个名为语音运染取消的目录。'
        refused = json.dumps({'status': 'unsupported',
                              'reason': "当前版本不支持创建目录，只能创建名为 '语音取消测试' 的目录。"})
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(ssh_hosts=[])
            engine.remember_task('在当前目录创建语音取消测试目录。', directory,
                                 TaskPlan((TaskStep(operation='create_directory',
                                                    parameters={'path': '语音取消测试'}),)),
                                 'verified', True, execution_state='completed', file_change_state='changed')
            with patch('core.engine.analyze_request', return_value={
                    'status': 'ready', 'operations': ['create_directory']}), \
                 patch('core.engine.chat', return_value=refused) as model:
                plan = engine.generate_task_plan(request, directory)
            self.assertEqual(plan.status, 'ready')
            self.assertEqual([(step.operation, step.parameters) for step in plan.steps],
                             [('create_directory', {'path': '语音运染取消'})])
            self.assertNotIn('最近任务上下文', model.call_args.args[0][1]['content'])

    def test_model_cannot_replace_the_requested_name(self):
        raw = json.dumps({'status': 'ready', 'steps': [{'operation': 'create_directory',
                                                      'parameters': {'path': '语音取消测试'}}]})
        with tempfile.TemporaryDirectory() as directory, \
             patch('core.engine.analyze_request', return_value={
                 'status': 'ready', 'operations': ['create_directory']}), \
             patch('core.engine.chat', return_value=raw):
            with self.assertRaises(ValueError):
                Engine(ssh_hosts=[]).generate_task_plan('在当前目录创建一个名为语音预览取消的目录。', directory)

    def test_recovered_plan_still_rejects_outside_path(self):
        raw = json.dumps({'status': 'unsupported', 'reason': '不支持创建目录'})
        with tempfile.TemporaryDirectory() as directory, \
             patch('core.engine.analyze_request', return_value={
                 'status': 'ready', 'operations': ['create_directory']}), \
             patch('core.engine.chat', return_value=raw):
            with self.assertRaisesRegex(ValueError, '工作目录之外'):
                Engine(ssh_hosts=[]).generate_task_plan('在当前目录创建一个名为../outside的目录。', directory)

    def test_cancelled_preview_does_not_create_directory(self):
        raw = json.dumps({'status': 'unsupported', 'reason': '不支持创建目录'})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executor = Mock()
            history = HistoryStore(root / 'history.jsonl')
            with patch('core.engine.analyze_request', return_value={
                    'status': 'ready', 'operations': ['create_directory']}), \
                 patch('core.engine.chat', return_value=raw):
                status = execute_request(Engine(ssh_hosts=[]), executor, history,
                                         '在当前目录创建一个名为语音预览取消的目录。', directory,
                                         'confirm', input_fn=lambda _: 'no')
            self.assertEqual(status, 'cancelled')
            self.assertEqual(history.last_record['execution_state'], 'not_started')
            self.assertEqual(history.last_record['file_change_state'], 'unchanged')
            self.assertFalse(history.last_record['executed'])
            self.assertFalse((root / '语音预览取消').exists())
            self.assertFalse((root / '语音取消测试').exists())
            executor.execute_policy_command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
