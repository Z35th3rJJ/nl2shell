import io
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
        result = self.confirm(text, ['yes', '1', 'backup', 'yes'], evidence=evidence)
        self.assertEqual(result, '将"sample.txt"复制到"backup"。')
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

    def test_similar_names_keep_the_users_choice(self):
        (self.root / 'simple.txt').write_bytes(b'other file\n')
        result = self.confirm('将sampel.txt移动到 backup 目录', ['yes', '2', '', 'yes'])
        self.assertEqual(result, '将"simple.txt"移动到 "backup" 目录')
        self.assertTrue((self.root / 'sample.txt').is_file())
        self.assertTrue((self.root / 'simple.txt').is_file())

    def test_existing_source_and_new_target_are_not_rewritten(self):
        for text in ('将 sample.txt 复制到 backup/new.txt', '将 sample.txt 移动到 backup 目录'):
            with self.subTest(text=text):
                self.assertEqual(self.confirm(text, ['yes']), text)
                self.assertEqual(len(self.prompts), 1)

    def test_manual_source_preserves_spaces_quotes_and_target(self):
        folder = self.root / '子目录'
        folder.mkdir()
        name = "项目 'final' 文件.txt"
        (folder / name).write_bytes(b'content')
        result = self.confirm('将 wrong.txt 复制到 backup/new.txt',
                              ['yes', '0', '子目录/' + name, '', 'yes'])
        self.assertEqual(result, '将 "子目录/' + name + '" 复制到 "backup/new.txt"')
        self.assertIn('backup/new.txt', result)

    def test_sensitive_and_audit_paths_are_not_offered(self):
        for name in ('.env', 'private.key', 'audit.jsonl'):
            (self.root / name).write_bytes(b'not a candidate')
        result = self.confirm('将 wrong.txt 复制到 backup 目录', ['yes', '1', '', 'yes'],
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
        result = self.confirm('将 wrong.txt 复制到 backup 目录', ['yes', '1', '', 'yes'])
        self.assertIn('sample.txt', result)
        self.assertNotIn('linked.txt', self.output.getvalue())

    def test_invalid_choice_and_cancellation_never_accept_a_correction(self):
        evidence = {}
        self.assertIsNone(self.confirm('将 wrong.txt 复制到 backup 目录',
                                      ['yes', '99', '1', '', '/cancel'], evidence=evidence))
        self.assertNotIn('confirmed_text', evidence)
        self.assertIn('请选择', self.output.getvalue())

    def test_user_can_edit_the_whole_request_or_exit(self):
        self.assertEqual(self.confirm('错误文字', ['将 sample.txt 复制到 backup 目录']),
                         '将 sample.txt 复制到 backup 目录')
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
        result = self.confirm('将 wrong.txt 复制到 backup 目录', ['yes', '1', '', 'yes'])
        self.assertEqual(result, '将 \'项目 "final" 文件.txt\' 复制到 "backup" 目录')

    def test_target_correction_cannot_escape_the_workspace(self):
        evidence = {}
        self.assertIsNone(self.confirm('将 wrong.txt 复制到 backup 目录',
                                      ['yes', '1', '../outside'], evidence=evidence))
        self.assertIn('工作目录之外', evidence['path_error'])
        self.assertNotIn('confirmed_text', evidence)

    def test_cli_cancellation_and_path_rejection_do_not_reach_the_model(self):
        import cli
        from core.history import HistoryStore
        from core.settings import AppSettings

        for transcript, replies, status in (
                ('将 wrong.txt 复制到 backup 目录', ['yes', '/cancel'], 'cancelled'),
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
        from core.task_plan import TaskPlan, TaskStep

        history = HistoryStore(self.root / 'audit.jsonl')
        engine = Mock(command_policy='whitelist', policy_attempts=[], intent_attempts=[],
                      plan_attempts=[], command_attempts=[])
        engine.generate_task_plan.return_value = TaskPlan((TaskStep(operation='copy_files',
            parameters={'sources': ['sample.txt'], 'destination': 'backup'}),))
        session = Mock()
        session.prompt.side_effect = ['/voice', 'yes', '1', 'backup', 'yes', '/exit']

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
