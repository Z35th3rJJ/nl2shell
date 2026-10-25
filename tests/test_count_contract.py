"""计数契约的响应回放；真实模型验收单独记录。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.bash_queries import prepare_queries
from core.engine import Engine, _plan_defaults
from core.requirements import resolve_request
from core.task_plan import TaskPlan, TaskStep, parse_operation_plan


REQUEST = "统计 logs 目录及子目录中的文件数量"
MODEL_REJECTION = "意图模型未能将请求识别为受支持的操作；任务未执行。"


def payload(steps):
    return json.dumps({"status": "ready", "steps": steps}, ensure_ascii=False)


def step(operation, **parameters):
    return {"operation": operation, "parameters": parameters}


class CountContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = self.temp.name
        (Path(self.cwd) / 'logs/nested').mkdir(parents=True)
        (Path(self.cwd) / 'logs/app.log').write_text('log\n')
        (Path(self.cwd) / 'logs/nested/readme.txt').write_text('also counted\n')

    def test_named_directory_request_resolves(self):
        plan = resolve_request(REQUEST)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.steps[0].operation, 'count_files')
        self.assertEqual(plan.steps[0].parameters, {'path': 'logs', 'recursive': True})

    def test_non_recursive_and_quoted_directory_requests(self):
        for request, path in [('统计 counts 目录这一层的文件数量，不包含子目录', 'counts'),
                              ('统计“日志 目录”目录中的文件数量', '日志 目录')]:
            with self.subTest(request=request):
                plan = resolve_request(request)
                self.assertIsNotNone(plan)
                self.assertEqual(plan.steps[0].parameters, {'path': path, 'recursive': False})

    def test_unparsed_conditions_do_not_create_a_request_contract(self):
        for request in ('统计 logs 目录中最近三天的文件数量',
                        '统计 logs 目录中大于 1 MiB 的文件数量',
                        '统计 logs 目录及子目录当前层的文件数量'):
            with self.subTest(request=request):
                self.assertIsNone(resolve_request(request))

    def test_named_count_missing_recursive_uses_request_scope(self):
        raw = payload([step('count_files', path='./logs')])
        plan = parse_operation_plan(_plan_defaults(raw, REQUEST, [], self.cwd))
        self.assertEqual(plan.steps[0].parameters, {'path': './logs', 'recursive': True})

    def test_recorded_find_then_count_is_normalized(self):
        intent = json.dumps({'status': 'ready', 'operations': ['count_files']})
        for parameters in ({'source_step': 1}, {'path': 'logs', 'recursive': True}):
            raw = payload([step('find_files', path='logs', recursive=True), step('count_files', **parameters)])
            with self.subTest(parameters=parameters), patch('core.intent.chat', return_value=intent), patch('core.engine.chat', return_value=raw) as model:
                engine = Engine(ssh_hosts=[])
                plan = engine.generate_task_plan(REQUEST, self.cwd)
                self.assertEqual(len(plan.steps), 1)
                self.assertEqual(plan.steps[0].operation, 'count_files')
                self.assertEqual(plan.steps[0].parameters, {'path': 'logs', 'recursive': True})
                self.assertEqual(model.call_count, 1)
                self.assertEqual(engine.plan_attempts[0]['raw_output'], raw)
                self.assertIn('normalization', engine.plan_attempts[0])

    def test_reference_find_is_checked_against_original_count_request(self):
        plan = TaskPlan((TaskStep(operation='count_files', parameters={'path': 'logs', 'recursive': True}),))
        raw = payload([step('find_files', path='logs', recursive=True)])
        with patch('core.engine.chat', side_effect=[raw, '{"commands":[]}']) as model:
            engine = Engine(ssh_hosts=[])
            result = engine.generate_bash_commands(REQUEST, self.cwd, plan, prepare_queries(plan, self.cwd), [])
        self.assertEqual(result, {'commands': []})
        self.assertEqual(model.call_count, 2)
        self.assertEqual(engine.command_attempts[0]['review'], 'accepted')
        self.assertIn('normalization', engine.command_attempts[0])

    def test_mismatched_reference_is_still_rejected(self):
        plan = TaskPlan((TaskStep(operation='count_files', parameters={'path': 'logs', 'recursive': True}),))
        for parameters in ({'path': 'logs', 'recursive': True, 'pattern': '*.log'},
                           {'path': 'logs', 'recursive': False}, {'path': '.', 'recursive': True}):
            with self.subTest(parameters=parameters), patch('core.engine.chat', return_value=payload([step('find_files', **parameters)])) as model:
                engine = Engine(ssh_hosts=[])
                with self.assertRaises(ValueError):
                    engine.generate_bash_commands(REQUEST, self.cwd, plan, prepare_queries(plan, self.cwd), [])
                self.assertEqual(model.call_count, 1)
                self.assertTrue(all(r['phase'] == 'independent_requirements' for r in engine.command_attempts))

    def test_self_contained_count_does_not_receive_previous_filter(self):
        engine = Engine(ssh_hosts=[])
        previous = TaskPlan((TaskStep(operation='find_files', parameters={'path': 'logs', 'recursive': True, 'pattern': '*.log'}),))
        engine.remember_task('查找日志文件', self.cwd, previous, 'verified', True,
                             execution_state='completed', file_change_state='unchanged')
        seen = []
        def reply(messages, **kwargs):
            seen.append(messages)
            return payload([step('count_files', path='logs', recursive=True)])
        with patch('core.intent.chat', return_value='{"status":"ready","operations":["count_files"]}'), patch('core.engine.chat', side_effect=reply):
            engine.generate_task_plan(REQUEST, self.cwd)
        self.assertNotIn('最近任务上下文', seen[0][1]['content'])
        self.assertNotIn('*.log', seen[0][1]['content'])
        self.assertEqual(len(engine._task_history), 1)

    def test_conflicting_count_parameters_are_not_overwritten(self):
        for parameters in ({'path': 'logs', 'recursive': False},
                           {'path': 'logs', 'recursive': True, 'pattern': '*.log'},
                           {'path': '.', 'recursive': True},
                           {'path': '../outside', 'recursive': True}):
            raw = payload([step('find_files', **parameters), step('count_files', source_step=1)])
            with self.subTest(parameters=parameters), patch('core.intent.chat', return_value='{"status":"ready","operations":["count_files"]}'), patch('core.engine.chat', return_value=raw):
                with self.assertRaises(ValueError):
                    Engine(ssh_hosts=[]).generate_task_plan(REQUEST, self.cwd)

    def test_illegal_count_parameters_are_not_discarded(self):
        for parameters in ({'source_step': 1, 'path': 'other'},
                           {'source_step': True}, {'source_step': 1.0}, {'source_step': 2}):
            raw = payload([step('find_files', path='logs', recursive=True), step('count_files', **parameters)])
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                parse_operation_plan(_plan_defaults(raw, REQUEST, [], self.cwd))

    def test_redundant_count_cannot_drop_conflicting_second_step(self):
        for parameters in ({'path': 'logs', 'recursive': False},
                           {'path': 'logs', 'recursive': True, 'pattern': '*.log'},
                           {'path': '.', 'recursive': True}):
            raw = payload([step('find_files', path='logs', recursive=True), step('count_files', **parameters)])
            with self.subTest(parameters=parameters), patch('core.intent.chat', return_value='{"status":"ready","operations":["count_files"]}'), patch('core.engine.chat', return_value=raw):
                with self.assertRaises(ValueError):
                    Engine(ssh_hosts=[]).generate_task_plan(REQUEST, self.cwd)

    def test_model_rejection_category_is_not_presented_as_program_fact(self):
        for request, code in [('我们挂载了几个硬盘', 'network'),
                              ('找一下当前目录下最大的文件', 'directory_selection'),
                              ('永久删除所有文件', 'permanent_delete')]:
            response = json.dumps({'status': 'unsupported', 'reason_code': code})
            with self.subTest(code=code), patch('core.intent.chat', return_value=response), patch('core.engine.chat', side_effect=AssertionError('不得规划拒绝的请求')):
                engine = Engine(ssh_hosts=[])
                plan = engine.generate_task_plan(request, self.cwd)
                self.assertTrue(plan.refused)
                self.assertFalse(plan.steps)
                self.assertEqual(plan.reason, MODEL_REJECTION)
                self.assertEqual(engine.request_intent['reason_code'], code)
                self.assertTrue(engine.intent_attempts)


if __name__ == '__main__':
    unittest.main()
