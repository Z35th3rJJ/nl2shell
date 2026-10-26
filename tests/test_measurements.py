"""计时不改变授权、错误传播或公共 JSON 输出。"""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from core.measurements import current, measured, measure_task, snapshot
from core.history import HistoryStore
from core.engine import Engine
from core.task_plan import parse_operation_plan
from core.llm import chat
from cli import execute_request, json_result


class MeasurementTests(unittest.TestCase):
    def test_wait_time_and_nested_tasks_are_not_counted_twice(self):
        clock=[0.0]
        @measure_task
        def child(input_fn=input):
            with measured('model',model='test-placeholder'):
                clock[0]+=3
        @measure_task
        def task(input_fn=input):
            input_fn('确认')
            child()
            return snapshot()
        def answer(prompt):
            clock[0]+=2
            return 'yes'
        with patch('core.measurements.time.monotonic',side_effect=lambda:clock[0]), \
                patch('core.measurements.runtime_environment',return_value={}):
            result=task(input_fn=answer)
        self.assertEqual(result['elapsed_seconds'],5)
        self.assertEqual(result['user_input_seconds'],2)
        self.assertEqual(result['active_seconds'],3)
        self.assertEqual(result['model_seconds'],3)
        self.assertEqual(result['model_call_count'],1)
        self.assertIsNone(current.get())

    def test_model_errors_are_recorded_without_messages_or_secrets(self):
        create=Mock(side_effect=RuntimeError('test-placeholder'))
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        @measure_task
        def task(input_fn=input):
            with self.assertRaises(RuntimeError):
                chat([{'role':'user','content':'private test input'}])
            return snapshot()
        with patch('core.llm._get_client',return_value=(client,'test-placeholder')):
            result=task()
        self.assertEqual(result['model_call_count'],1)
        self.assertEqual(result['calls'][0]['error_type'],'RuntimeError')
        self.assertNotIn('private test input',json.dumps(result))
        self.assertNotIn('base_url',json.dumps(result))
        create.assert_called_once()

    def test_cli_failure_saves_measurements_and_keeps_json_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            history=HistoryStore(Path(directory)/'history.jsonl')
            engine=Engine(ssh_hosts=[])
            refusal=parse_operation_plan('{"status":"unsupported","reason":"test-placeholder"}')
            with patch.object(engine,'generate_task_plan',return_value=refusal):
                status=execute_request(engine,Mock(),history,'未支持的需求',directory,'confirm')
            record=json.loads(history.path.read_text(encoding='utf-8').splitlines()[-1])
            self.assertEqual(status,'unsupported')
            self.assertFalse(record['executed'])
            self.assertEqual(record['measurements']['model_call_count'],0)
            self.assertIn('python',record['measurements']['runtime'])
            self.assertEqual(set(json_result(record,status)),
                             {'status','risk_level','steps','verification','duration_seconds','error'})

    def test_exit_resets_context_and_does_not_become_an_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            history=HistoryStore(Path(directory)/'history.jsonl')
            engine=Engine(ssh_hosts=[])
            plan=parse_operation_plan('{"status":"need_clarification","clarification":"目标目录？"}')
            with patch.object(engine,'generate_task_plan',return_value=plan) as planner, self.assertRaises(SystemExit):
                execute_request(engine,Mock(),history,'复制到合适的目录',directory,'confirm',input_fn=lambda _: '/exit')
            record=json.loads(history.path.read_text(encoding='utf-8').splitlines()[-1])
            self.assertEqual(record['status'],'cancelled')
            self.assertEqual(record['clarifications'],[])
            self.assertFalse(record['executed'])
            self.assertEqual(planner.call_count,1)
            self.assertIsNone(current.get())


if __name__=='__main__':
    unittest.main()
