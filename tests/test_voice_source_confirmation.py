import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from core.voice_input import confirm_transcript


class VoiceSourceConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        (self.root / 'sample.txt').write_bytes(b'voice test\n')
        (self.root / 'backup').mkdir()

    def confirm(self, text, answers, **options):
        self.prompts = []
        replies = iter(answers)

        def prompt(message):
            self.prompts.append(message)
            return next(replies)

        self.output = io.StringIO()
        with redirect_stdout(self.output):
            return confirm_transcript(text, prompt, cwd=str(self.root), **options)

    def test_recorded_error_requires_selection_and_reconfirmation(self):
        evidence = {}
        text = '将simple点TXT复制到backup目录。'
        result = self.confirm(text, ['yes', '1', '1', 'yes'], evidence=evidence)
        self.assertEqual(result, '将 "sample.txt" 复制到 "backup" 目录。')
        self.assertEqual(evidence['transcript'], text)
        self.assertEqual(evidence['confirmed_text'], result)
        self.assertEqual(evidence['source_corrections'][0]['selected_source'], 'sample.txt')
        self.assertIn('修订后的请求', self.output.getvalue())
        self.assertEqual(evidence['target_corrections'][0]['confirmed_target'], 'backup')
        self.assertEqual(len(self.prompts), 4)
        self.assertEqual((self.root / 'sample.txt').read_bytes(), b'voice test\n')
        self.assertEqual(list((self.root / 'backup').iterdir()), [])

    def test_unique_candidate_is_never_selected_implicitly(self):
        self.assertIsNone(self.confirm('将wrong.txt复制到 backup 目录', ['yes', '']))
        self.assertEqual(len(self.prompts), 2)

    def test_empty_target_choice_does_not_accept_the_recognized_directory_name(self):
        evidence = {}
        self.assertIsNone(self.confirm('将sple点TxT复制到backup目录。',
                                      ['yes', '1', '', 'yes'], evidence=evidence))
        self.assertNotIn('confirmed_text', evidence)
        self.assertFalse((self.root / 'backup目录').exists())

    def test_existing_source_still_requires_target_selection(self):
        self.assertIsNone(self.confirm('将sample.txt复制到backup目录。', ['yes', '']))
        self.assertFalse((self.root / 'backup目录').exists())

    def test_both_directory_names_require_an_explicit_choice(self):
        (self.root / 'backup目录').mkdir()
        for choice, target in (('1', 'backup'), ('2', 'backup目录')):
            with self.subTest(choice=choice):
                evidence = {}
                result = self.confirm('将sple点TxT复制到backup目录。',
                                      ['yes', '1', choice, 'yes'], evidence=evidence)
                self.assertEqual(result, '将 "sample.txt" 复制到 "' + target + '" 目录。')
                self.assertEqual(evidence['target_confirmation']['path'], target)

    def test_explicit_new_directory_and_full_target_keep_their_names(self):
        for choice, target, expected in (
                ('d', 'backup目录', '将 "sample.txt" 复制到 "backup目录" 目录。'),
                ('f', 'backup/newname', '将 "sample.txt" 复制到 "backup" 目录 并命名为 "newname"。'),
                ('f', 'copy.txt', '将 "sample.txt" 复制到 "." 目录 并命名为 "copy.txt"。')):
            with self.subTest(target=target):
                self.assertEqual(self.confirm('将 wrong.txt 复制到 backup 目录',
                                             ['yes', '1', choice, target, 'yes']), expected)
                self.assertFalse((self.root / target).exists())

    def test_file_cannot_be_selected_as_a_target_directory(self):
        evidence = {}
        self.assertIsNone(self.confirm('将 sample.txt 复制到 backup 目录',
                                      ['yes', 'd', 'sample.txt'], evidence=evidence))
        self.assertEqual(evidence['path_error'], '目标目录路径实际是文件')

    def test_similar_names_keep_the_users_choice(self):
        (self.root / 'simple.txt').write_bytes(b'other file\n')
        result = self.confirm('将sampel.txt移动到 backup 目录', ['yes', '2', '1', 'yes'])
        self.assertEqual(result, '将 "simple.txt" 移动到 "backup" 目录。')
        self.assertTrue((self.root / 'sample.txt').is_file())
        self.assertTrue((self.root / 'simple.txt').is_file())

    def test_existing_source_and_explicit_new_target_keep_their_meaning(self):
        cases = (
            ('将 sample.txt 复制到 backup/new.txt', ['yes', 'f', 'backup/new.txt', 'yes'],
             '将 "sample.txt" 复制到 "backup" 目录 并命名为 "new.txt"。'),
            ('将 sample.txt 移动到 backup 目录', ['yes', '1', 'yes'],
             '将 "sample.txt" 移动到 "backup" 目录。'))
        for text, answers, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(self.confirm(text, answers), expected)
                self.assertNotIn('未找到源对象', self.output.getvalue())


    def test_manual_source_preserves_spaces_quotes_and_target(self):
        folder = self.root / '子目录'
        folder.mkdir()
        name = "项目 'final' 文件.txt"
        (folder / name).write_bytes(b'content')
        result = self.confirm('将 wrong.txt 复制到 backup/new.txt',
                              ['yes', '0', '子目录/' + name, 'f', 'backup/new.txt', 'yes'])
        self.assertEqual(result, '将 "子目录/' + name + '" 复制到 "backup" 目录 并命名为 "new.txt"。')
        self.assertIn('并命名为 "new.txt"', result)

    def test_sensitive_and_audit_paths_are_not_offered(self):
        for name in ('.env', 'private.key', 'audit.jsonl'):
            (self.root / name).write_bytes(b'not a candidate')
        result = self.confirm('将 wrong.txt 复制到 backup 目录', ['yes', '1', '1', 'yes'],
                              excluded=[self.root / 'audit.jsonl'])
        self.assertIn('sample.txt', result)
        for name in ('.env', 'private.key', 'audit.jsonl'):
            self.assertNotIn(name, self.output.getvalue())

    def test_outside_source_does_not_open_a_candidate_menu(self):
        self.assertIsNone(self.confirm('将 ../outside.txt 复制到 backup 目录', ['yes']))
        self.assertIn('路径位于工作目录之外', self.output.getvalue())
        self.assertEqual(len(self.prompts), 1)

    def test_symlink_is_not_offered_or_used_as_source(self):
        try:
            (self.root / 'linked.txt').symlink_to(self.root / 'sample.txt')
        except OSError as error:
            self.skipTest(str(error))
        self.assertIsNone(self.confirm('将 linked.txt 复制到 backup 目录', ['yes']))
        self.assertIn('符号链接', self.output.getvalue())
        result = self.confirm('将 wrong.txt 复制到 backup 目录', ['yes', '1', '1', 'yes'])
        self.assertIn('sample.txt', result)
        self.assertNotIn('linked.txt', self.output.getvalue())

    def test_invalid_choice_and_cancellation_never_accept_a_correction(self):
        evidence = {}
        self.assertIsNone(self.confirm('将 wrong.txt 复制到 backup 目录',
                                      ['yes', '99', '1', '1', '/cancel'], evidence=evidence))
        self.assertNotIn('confirmed_text', evidence)
        self.assertIn('请选择', self.output.getvalue())

    def test_user_can_edit_the_whole_request_or_exit(self):
        self.assertEqual(self.confirm('错误文字', ['将 sample.txt 复制到 backup 目录', '1', 'yes']),
                         '将 "sample.txt" 复制到 "backup" 目录。')
        self.assertEqual(self.confirm('将 wrong.txt 复制到 backup 目录', ['yes', '/exit']), '/exit')

    def test_missing_workspace_and_large_directory_stop_without_guessing(self):
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(confirm_transcript('将 a.txt 复制到 backup 目录', lambda _: 'yes',
                                                cwd=str(self.root / 'missing')))
        for index in range(201):
            (self.root / f'entry-{index}').touch()
        self.assertIsNone(self.confirm('将 wrong.txt 复制到 backup 目录', ['yes', '']))
        self.assertIn('较多', self.output.getvalue())

    @unittest.skipIf(os.name == 'nt', 'Windows 文件名不允许双引号')
    def test_literal_quotes_in_linux_filename_remain_part_of_the_source(self):
        (self.root / '项目 "final" 文件.txt').touch()
        result = self.confirm('将 wrong.txt 复制到 backup 目录', ['yes', '2', '1', 'yes'])
        self.assertEqual(result, '将 \'项目 "final" 文件.txt\' 复制到 "backup" 目录。')

    def test_target_correction_cannot_escape_the_workspace(self):
        evidence = {}
        self.assertIsNone(self.confirm('将 wrong.txt 复制到 backup 目录',
                                      ['yes', '1', 'd', '../outside'], evidence=evidence))
        self.assertIn('工作目录之外', evidence['path_error'])
        self.assertNotIn('confirmed_text', evidence)

    def test_confirmed_new_directory_is_used_without_model_reinterpretation(self):
        from core.engine import Engine

        evidence = {}
        text = self.confirm('将sple点TxT复制到backup目录。',
                            ['yes', '1', 'd', 'new目录', 'yes'], evidence=evidence)
        engine = Engine()
        with patch('core.engine.chat', side_effect=AssertionError('must not reinterpret confirmed paths')):
            plan = engine.generate_task_plan(text, str(self.root),
                                             confirmed_transfer=evidence['confirmed_transfer'])
        self.assertEqual(plan.steps[0].parameters, {'sources': ['sample.txt'], 'destination': 'new目录'})
        self.assertEqual(engine.plan_attempts[0]['source'], 'confirmed_voice_paths')

    def test_confirmed_fields_still_require_parameter_path_and_request_checks(self):
        from core.engine import Engine

        text = '将 "sample.txt" 复制到 "backup" 目录。'
        variants = (
            {'operation': 'move_files', 'parameters': {'sources': ['sample.txt'], 'destination': 'backup'}},
            {'operation': 'copy_files', 'parameters': {'sources': ['other.txt'], 'destination': 'backup'}},
            {'operation': 'copy_files', 'parameters': {'sources': ['sample.txt'], 'destination': 'other'}},
            {'operation': 'copy_files', 'parameters': {'sources': ['sample.txt'], 'destination': '../outside'}},
            {'operation': 'copy_files', 'parameters': {'sources': ['sample.txt'], 'destination': 'backup', 'destination_name': '../bad'}},
            {'operation': 'copy_files', 'parameters': {'sources': ['sample.txt'], 'destination': 'backup', 'preserve_structure': True}})
        with patch('core.engine.chat', side_effect=AssertionError('validation must not call the model')):
            for fields in variants:
                with self.subTest(fields=fields), self.assertRaises(ValueError):
                    Engine().generate_task_plan(text, str(self.root), confirmed_transfer=fields)

    def test_manual_text_edit_discards_previous_transfer_fields(self):
        evidence = {}
        result = self.confirm('将 wrong.txt 复制到 backup 目录',
                              ['yes', '1', '1', '列出当前目录的内容'], evidence=evidence)
        self.assertEqual(result, '列出当前目录的内容')
        self.assertNotIn('confirmed_transfer', evidence)
        self.assertNotIn('target_confirmation', evidence)

    def test_normal_text_planning_still_uses_the_model(self):
        from core.engine import Engine

        fields = {'operation': 'copy_files', 'parameters': {'sources': ['sample.txt'], 'destination': 'backup'}}
        with patch('core.engine.analyze_request', return_value={'status': 'ready', 'operations': ['copy_files']}) as intent, \
             patch('core.engine.chat', return_value=json.dumps({'status': 'ready', 'steps': [fields]})) as model:
            plan = Engine().generate_task_plan('将 sample.txt 复制到 backup 目录', str(self.root))
        intent.assert_called_once()
        model.assert_called_once()
        self.assertEqual(plan.steps[0].parameters, fields['parameters'])

    def test_cli_cancellation_and_path_rejection_do_not_reach_the_model(self):
        import cli
        from core.history import HistoryStore
        from core.settings import AppSettings

        for transcript, replies, status in (
                ('将 wrong.txt 复制到 backup 目录', ['yes', '/cancel'], 'cancelled'),
                ('将sple点TxT复制到backup目录。', ['yes', '1', ''], 'cancelled'),
                ('将sample.txt复制到backup目录。', ['yes', ''], 'cancelled'),
                ('将 ../outside.txt 复制到 backup 目录', ['yes'], 'blocked')):
            with self.subTest(status=status):
                history = HistoryStore(self.root / 'audit.jsonl')
                session = Mock()
                session.prompt.side_effect = ['/voice', *replies, '/exit']
                with patch.object(cli, 'load_settings', return_value=AppSettings('confirm', True)), \
                     patch.object(cli, 'first_run_setup', side_effect=lambda settings: settings), \
                     patch.object(cli, 'create_executor'), patch.object(cli, 'Engine'), \
                     patch.object(cli, 'HistoryStore', return_value=history), patch.object(cli, 'print_diagnostics'), \
                     patch.object(cli, 'recover_working_directory', return_value=(str(self.root), '')), \
                     patch.object(cli, 'record_and_transcribe', return_value=transcript), \
                     patch.object(cli, 'execute_request') as execute, redirect_stdout(io.StringIO()):
                    cli.main(input_session=session)
                execute.assert_not_called()
                row = history.recent(1)[0]
                self.assertEqual(row['voice_input']['transcript'], transcript)
                self.assertEqual(row['status'], status)
                self.assertEqual(row['phase'], 'input')
                self.assertFalse(row['executed'])
                self.assertEqual(row['file_change_state'], 'unchanged')

    def test_cli_records_original_and_confirmed_text_and_waits_before_writing(self):
        import cli
        from core.history import HistoryStore
        from core.settings import AppSettings
        from core.engine import Engine

        history = HistoryStore(self.root / 'audit.jsonl')
        engine = Engine()
        engine.command_policy = 'whitelist'
        session = Mock()
        session.prompt.side_effect = ['/voice', 'yes', '1', '1', 'yes', '/exit']

        def authorize(message):
            self.assertIn('yes', message)
            self.assertFalse((self.root / 'backup/sample.txt').exists())
            return 'yes'

        run_request = cli.execute_request
        confirmation = Mock(side_effect=authorize)
        with patch.object(cli, 'load_settings', return_value=AppSettings('confirm', True)), \
             patch.object(cli, 'first_run_setup', side_effect=lambda settings: settings), \
             patch.object(cli, 'create_executor'), patch.object(cli, 'Engine', return_value=engine), \
             patch.object(cli, 'HistoryStore', return_value=history), patch.object(cli, 'print_diagnostics'), \
             patch.object(cli, 'recover_working_directory', return_value=(str(self.root), '')), \
             patch.object(cli, 'record_and_transcribe', return_value='将simple点TXT复制到backup目录。'), \
             patch('core.engine.chat', side_effect=AssertionError('confirmed paths must not call the model')), \
             patch.object(cli, 'execute_request', side_effect=lambda *args, **kwargs:
                 run_request(*args, input_fn=confirmation, **kwargs)), redirect_stdout(io.StringIO()):
            cli.main(input_session=session)
        confirmation.assert_called_once()
        row = history.recent(1)[0]
        self.assertEqual(row['voice_input']['transcript'], '将simple点TXT复制到backup目录。')
        self.assertEqual(row['voice_input']['confirmed_text'], row['input'])
        self.assertEqual(row['status'], 'verified')
        self.assertEqual((self.root / 'sample.txt').read_bytes(), b'voice test\n')
        self.assertEqual((self.root / 'backup/sample.txt').read_bytes(), b'voice test\n')
        self.assertIn('sample.txt', row['input'])


if __name__ == '__main__':
    unittest.main()
