"""人工确认和澄清不消耗计算预算；模型与准备耗时仍受限制。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from cli import execute_request
from core.engine import Engine
from core.execution import ExecutionResult
from core.history import HistoryStore


class ConfirmationTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.clock=[0.0]
        self.engine=Engine(ssh_hosts=[])
        self.engine.command_policy='blacklist'
        self.executor=Mock()
        self.executor.execute_policy_query.return_value=(ExecutionResult(0,'2026\n','',.01),['launcher'])
        self.history=HistoryStore(self.root/'history.jsonl')
        self.candidate={'status':'ready','argv':['date','-u','+%Y'],'explanation':'年份'}

    def run_request(self,model,answer,sandbox=None):
        with patch('cli.time.monotonic',side_effect=lambda:self.clock[0]), \
                patch('core.command_policy.chat',side_effect=model), \
                patch('core.command_policy.sandbox_argv',side_effect=sandbox or (lambda *a,**k:['launcher'])):
            return execute_request(self.engine,self.executor,self.history,'显示UTC年份',str(self.root),'confirm',
                                   timeout_seconds=60,input_fn=answer)

    def response(self,*args,**kwargs):
        return json.dumps(self.candidate)

    def delayed_yes(self,prompt):
        self.clock[0]+=120
        return 'yes'

    def test_long_confirmation_wait_does_not_use_execution_budget(self):
        self.assertEqual(self.run_request(self.response,self.delayed_yes),'unverified')
        self.assertEqual(self.executor.execute_policy_query.call_args.kwargs['timeout_seconds'],60)
        self.assertEqual(self.history.last_record['measurements']['user_input_seconds'],120)

    def test_clarification_and_confirmation_waits_are_both_excluded(self):
        replies=iter([json.dumps({'status':'need_clarification','clarification':'使用UTC时间吗？'}),self.response()])
        self.assertEqual(self.run_request(lambda *a,**k:next(replies),self.delayed_yes),'unverified')
        self.assertEqual(self.history.last_record['measurements']['user_input_seconds'],240)
        self.assertEqual(self.executor.execute_policy_query.call_args.kwargs['timeout_seconds'],60)

    def test_model_time_is_still_limited_before_confirmation(self):
        def slow_model(*args,**kwargs):
            self.clock[0]+=61
            return self.response()
        answer=Mock(return_value='yes')
        self.assertEqual(self.run_request(slow_model,answer),'blocked')
        answer.assert_not_called()
        self.executor.execute_policy_query.assert_not_called()

    def test_preparation_time_is_not_erased_by_waiting(self):
        def model(*args,**kwargs):
            self.clock[0]+=5
            return self.response()
        def sandbox(*args,**kwargs):
            self.clock[0]+=3
            return ['launcher']
        self.assertEqual(self.run_request(model,self.delayed_yes,sandbox),'unverified')
        self.assertEqual(self.executor.execute_policy_query.call_args.kwargs['timeout_seconds'],49)

    def test_cancelling_after_long_wait_never_executes(self):
        def no(prompt):
            self.clock[0]+=120
            return 'no'
        self.assertEqual(self.run_request(self.response,no),'cancelled')
        self.executor.execute_policy_query.assert_not_called()
        self.assertFalse(self.history.last_record['executed'])

    def test_slow_post_confirmation_check_cannot_start_an_expired_task(self):
        def sandbox(*args,**kwargs):
            self.clock[0]+=40
            return ['launcher']
        self.assertEqual(self.run_request(self.response,self.delayed_yes,sandbox),'blocked')
        self.executor.execute_policy_query.assert_not_called()
        self.assertFalse(self.history.last_record['executed'])


if __name__=='__main__':
    unittest.main()
