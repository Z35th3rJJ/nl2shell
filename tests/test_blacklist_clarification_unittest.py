"""黑名单命令的必要澄清与执行证据；不调用真实模型。"""
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from cli import execute_request
from core.engine import Engine
from core.execution import ExecutionResult
from core.execution_evidence import summarize_execution
from core.history import HistoryStore
from core.command_policy import review_command


class BlacklistClarificationTests(unittest.TestCase):
    def run_copy(self, target, expected_status):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'chapter.txt').write_text('source', encoding='utf8')
            (root / 'saved').mkdir()
            engine = Engine(ssh_hosts=[])
            engine.command_policy = 'blacklist'
            executor = Mock()
            executor.execute_policy_command.return_value = (
                ExecutionResult(0, '', '', .01, process_state='exited'), ['sandbox'])
            history = HistoryStore(root / 'history.jsonl')
            prompts = []
            def answer(prompt):
                prompts.append(prompt)
                self.assertFalse((root / 'saved/chapter.txt').exists())
                if len(prompts) == 1:
                    self.assertIn('你的回答', prompt)
                    return 'saved'
                return 'yes'
            raw = json.dumps({'status': 'ready', 'argv': ['cp', 'chapter.txt', target],
                              'explanation': '复制文件'})
            with patch('core.command_policy.chat', return_value=raw) as chat, \
                 patch('core.command_policy.sandbox_argv', return_value=['sandbox']), \
                 redirect_stdout(io.StringIO()) as terminal:
                status = execute_request(engine, executor, history, '将 chapter.txt 复制到合适的目录',
                                         directory, 'confirm', input_fn=answer)
            self.assertEqual(status, expected_status)
            self.assertEqual(chat.call_count, 1)
            self.assertIn('saved', chat.call_args.args[0][1]['content'])
            if status == 'blocked':
                executor.execute_policy_command.assert_not_called()
                self.assertNotIn('01 / 执行预览', terminal.getvalue())
            else:
                self.assertEqual(history.last_record['execution_state'], 'completed')
                self.assertEqual(history.last_record['status'], 'unverified')
                self.assertEqual(history.last_record['file_change_state'], 'unknown')
            self.assertIn('用户回答：saved', history.last_record['clarifications'][0])

    def test_missing_destination_is_confirmed_before_command_generation(self):
        self.run_copy('saved', 'unverified')

    def test_model_cannot_replace_confirmed_destination(self):
        self.run_copy('archive', 'blocked')

    def test_execution_completion_is_independent_of_verification(self):
        for status, code in [('unverified', 0), ('execution_failed', 1)]:
            with self.subTest(status=status):
                outcome = {'executed': True, 'execution_state': 'exited', 'status': status,
                           'exit_code': code, 'file_change_state': 'unchanged'}
                self.assertEqual(summarize_execution([outcome])['execution_state'], 'completed')
                outcome['timed_out'] = True
                self.assertEqual(summarize_execution([outcome])['execution_state'], 'stopped')

    def test_transfer_binding_checks_operation_source_and_final_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'chapter.txt').write_text('source', encoding='utf8')
            (root / 'saved').mkdir()
            for verb, program in [('复制', 'cp'), ('移动', 'mv')]:
                transfer = dict(source='chapter.txt', verb=verb, target='saved', directory=True)
                for target in ['saved', './saved/chapter.txt']:
                    with self.subTest(verb=verb, target=target):
                        review_command([program, 'chapter.txt', target], directory, transfer=transfer)
                for argv in [[program, 'other.txt', 'saved'], [program, 'chapter.txt', 'archive'],
                             [program, 'chapter.txt', 'other.txt', 'saved'],
                             [program, '--no-target-directory', 'chapter.txt', 'saved'],
                             ['mkdir', 'saved'], ['pwd'],
                             ['mv' if program == 'cp' else 'cp', 'chapter.txt', 'saved']]:
                    with self.subTest(argv=argv), self.assertRaises(ValueError):
                        review_command(argv, directory, transfer=transfer)
                transfer['target'] = 'new-directory'
                with self.assertRaisesRegex(ValueError, '目标目录不存在'):
                    review_command([program, 'chapter.txt', 'new-directory'], directory, transfer=transfer)

    def test_cancel_during_required_clarification_never_calls_model(self):
        for answer in ['/exit', '/cancel', '']:
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as directory:
                engine = Engine(ssh_hosts=[])
                engine.command_policy = 'blacklist'
                history = HistoryStore(Path(directory) / 'history.jsonl')
                with patch('core.command_policy.chat', side_effect=AssertionError('取消不得调用模型')), \
                     redirect_stdout(io.StringIO()):
                    try:
                        status = execute_request(engine, Mock(), history,
                            '将 chapter.txt 移动到合适的目录', directory, 'confirm', input_fn=lambda _: answer)
                    except SystemExit:
                        self.assertEqual(answer, '/exit')
                    else:
                        self.assertEqual(status, 'cancelled')
                self.assertFalse(history.last_record['executed'])
                self.assertEqual(history.last_record['clarifications'], [])

    def test_explicit_destination_needs_only_execution_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'saved').mkdir()
            engine = Engine(ssh_hosts=[])
            engine.command_policy = 'blacklist'
            history = HistoryStore(root / 'history.jsonl')
            raw = json.dumps({'status': 'ready', 'argv': ['cp', 'chapter.txt', 'saved'], 'explanation': '复制'})
            prompts = []
            def cancel(prompt):
                prompts.append(prompt)
                return 'no'
            with patch('core.command_policy.chat', return_value=raw), \
                 patch('core.command_policy.sandbox_argv', return_value=['sandbox']), \
                 redirect_stdout(io.StringIO()):
                status = execute_request(engine, Mock(), history, '将 chapter.txt 复制到 saved 目录',
                                         directory, 'confirm', input_fn=cancel)
            self.assertEqual(status, 'cancelled')
            self.assertEqual(len(prompts), 1)
            self.assertIn('请输入 yes 确认', prompts[0])
            self.assertEqual(history.last_record['clarifications'], [])

    def test_partial_steps_and_uncertain_repairs_do_not_become_completed(self):
        completed = dict(executed=True, execution_state='exited', status='unverified', file_change_state='unknown')
        pending = dict(executed=False, execution_state='not_started', status='not_executed', file_change_state='unchanged')
        self.assertEqual(summarize_execution([completed, pending])['execution_state'], 'stopped')
        unknown = dict(completed, execution_state='unknown')
        self.assertEqual(summarize_execution([completed], [{'initial_outcome': unknown}])['execution_state'], 'unknown')
        changed = dict(completed, file_change_state='changed')
        self.assertEqual(summarize_execution([changed, unknown])['file_change_state'], 'changed')


if __name__ == '__main__':
    unittest.main()
