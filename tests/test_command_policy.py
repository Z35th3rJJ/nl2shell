"""两种命令策略的公开接口和 Linux 隔离验证。"""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

from core.command_policy import review_query, sandbox_argv, prepare_native
from core.execution import BashExecutor
from core.operations import prepare_plan, execute_action
from core.settings import load_settings, save_settings, AppSettings
from core.task_plan import parse_operation_plan
from core.engine import Engine
from core.history import HistoryStore
from cli import execute_request


class CommandPolicyTests(unittest.TestCase):
    def test_blacklist_accepts_system_utility_outside_fixed_templates(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(review_query(['date', '-u', '+%Y'], directory), ['date', '-u', '+%Y'])

    def test_blacklist_rejects_dangerous_commands_and_execution_options(self):
        with tempfile.TemporaryDirectory() as directory:
            for argv in (['rm', '-rf', '.'], ['sudo', 'id'], ['sh', '-c', 'id'],
                         ['find', '.', '-exec', 'rm', '{}', ';']):
                with self.subTest(argv=argv), self.assertRaises(ValueError):
                    review_query(argv, directory)

    def test_default_and_explicit_configuration_are_separate_from_run_mode(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_settings().command_policy, 'whitelist')
        with patch.dict(os.environ, {'COMMAND_POLICY':'blacklist','RUN_MODE':'confirm'}, clear=True):
            self.assertEqual(load_settings().command_policy, 'blacklist')
            self.assertEqual(load_settings().run_mode, 'confirm')
        with patch.dict(os.environ, {'COMMAND_POLICY':'invalid'}, clear=True), self.assertRaises(ValueError):
            load_settings()
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory)/'.env'
            env.write_text('LOCAL_MODEL=test-model\n')
            self.assertTrue(save_settings(AppSettings('confirm',True,'blacklist'), env))
            self.assertIn('COMMAND_POLICY=blacklist', env.read_text())
            self.assertIn('LOCAL_MODEL=test-model', env.read_text())

    def test_cli_denial_never_starts_a_process(self):
        with tempfile.TemporaryDirectory() as directory:
            engine=Engine(ssh_hosts=[])
            engine.command_policy='blacklist'
            executor=Mock()
            history=HistoryStore(Path(directory)/'history.jsonl')
            raw=json.dumps({'status':'ready','argv':['rm','-rf','.'],'explanation':'危险'})
            with patch('core.command_policy.chat',return_value=raw):
                status=execute_request(engine,executor,history,'删除全部文件',directory,'confirm',input_fn=lambda _: 'yes')
            self.assertEqual(status,'blocked')
            self.assertFalse(history.last_record['executed'])
            executor.execute_policy_query.assert_not_called()

    def test_exit_at_clarification_is_not_saved_as_answer_or_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            engine=Engine(ssh_hosts=[])
            engine.command_policy='blacklist'
            history=HistoryStore(Path(directory)/'history.jsonl')
            raw=json.dumps({'status':'need_clarification','clarification':'查询哪个文件？'})
            with patch('core.command_policy.chat',return_value=raw) as model, self.assertRaises(SystemExit):
                execute_request(engine,Mock(),history,'查看一下',directory,'confirm',input_fn=lambda _: '/exit')
            self.assertEqual(model.call_count,1)
            self.assertEqual(history.last_record['status'],'cancelled')
            self.assertEqual(history.last_record['clarifications'],[])
            self.assertFalse(history.last_record['executed'])

    @unittest.skipUnless(sys.platform == 'linux', '需要 Linux/bwrap')
    def test_failed_authorization_audit_prevents_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'not_a_directory').write_bytes(b'fixture')
            history=HistoryStore(root/'not_a_directory/history.jsonl')
            engine=Engine(ssh_hosts=[])
            engine.command_policy='blacklist'
            executor=Mock()
            raw=json.dumps({'status':'ready','argv':['date','-u','+%Y'],'explanation':'日期'})
            with patch('core.command_policy.chat',return_value=raw):
                status=execute_request(engine,executor,history,'显示年份',directory,'confirm',input_fn=lambda _: 'yes')
            self.assertEqual(status,'audit_failed')
            self.assertFalse(history.last_record['executed'])
            executor.execute_policy_query.assert_not_called()

    @unittest.skipUnless(sys.platform == 'linux', '需要 Linux/bwrap')
    def test_yes_flag_does_not_authorize_native_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'source.txt').write_bytes(b'fixture')
            plan=parse_operation_plan(json.dumps({'status':'ready','steps':[{'operation':'copy_files',
                'parameters':{'sources':['source.txt'],'destination':'backup'}}]}))
            history=HistoryStore(root/'history.jsonl')
            engine=Engine(ssh_hosts=[])
            engine.command_policy='blacklist'
            executor=Mock()
            with patch('core.command_policy.chat',return_value='{"status":"protected"}'), patch.object(engine,'generate_task_plan',return_value=plan):
                status=execute_request(engine,executor,history,'复制文件',directory,'confirm',input_fn=lambda _: '',assume_yes=True)
            self.assertEqual(status,'cancelled')
            self.assertFalse((root/'backup/source.txt').exists())
            executor.execute_native_item.assert_not_called()

    @unittest.skipUnless(sys.platform == 'linux', '需要 Linux/bwrap')
    def test_real_sandbox_runs_query_without_exposing_sensitive_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'sample.txt').write_bytes(b'fixture')
            (root/'.env').write_bytes(b'test-placeholder')
            result, actual=BashExecutor().execute_policy_query(['cat','sample.txt'], directory)
            self.assertEqual(result.exit_code,0,result.stderr)
            self.assertEqual(result.stdout,'fixture')
            self.assertIn('/usr/bin/bwrap', actual)
            status=subprocess.run(sandbox_argv(['cat','/proc/self/status'],directory),capture_output=True,text=True)
            self.assertEqual(status.returncode,0,status.stderr)
            fields=dict(line.split(':',1) for line in status.stdout.splitlines() if ':' in line)
            self.assertEqual(fields['NoNewPrivs'].strip(),'1')
            self.assertEqual(int(fields['CapEff'].strip(),16),0)
            network=subprocess.run(sandbox_argv(['readlink','/proc/self/ns/net'],directory),capture_output=True,text=True)
            self.assertEqual(network.returncode,0,network.stderr)
            self.assertNotEqual(network.stdout.strip(),os.readlink('/proc/self/ns/net'))
            result, _=BashExecutor().execute_policy_query(['cat','.env'], directory)
            self.assertIn(result.exit_code,(0,1))
            self.assertEqual(result.stdout,'')
            self.assertEqual((root/'.env').read_bytes(),b'test-placeholder')

    @unittest.skipUnless(sys.platform == 'linux', '需要 Linux/bwrap')
    def test_readonly_mount_blocks_write_even_without_command_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            result=subprocess.run(sandbox_argv(['touch','unauthorized.txt'],directory),capture_output=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn(b'Read-only file system',result.stderr)
            self.assertFalse((Path(directory)/'unauthorized.txt').exists())

    @unittest.skipUnless(sys.platform == 'linux', '需要 Linux/bwrap')
    def test_native_copy_move_and_mkdir_are_independently_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'source.txt').write_bytes(b'original content')
            executor=BashExecutor()
            for operation, parameters in (
                ('copy_files',{'sources':['source.txt'],'destination':'backup'}),
                ('move_files',{'sources':['backup/source.txt'],'destination':'moved'}),
                ('create_directory',{'path':'created'})):
                plan=parse_operation_plan(json.dumps({'status':'ready','steps':[{'operation':operation,'parameters':parameters}]}))
                prepared=prepare_native(prepare_plan(plan,directory))
                result=execute_action(prepared,prepared['actions'][0],executor)
                self.assertEqual(result['status'],'verified',result)
                self.assertTrue(result['actual_commands'])
                self.assertIn('/usr/bin/bwrap',result['items'][0]['launcher_argv'])
            self.assertEqual((root/'source.txt').read_bytes(),b'original content')
            self.assertEqual((root/'moved/source.txt').read_bytes(),b'original content')
            self.assertFalse((root/'backup/source.txt').exists())
            self.assertTrue((root/'created').is_dir())

    @unittest.skipUnless(sys.platform == 'linux', '需要 Linux/bwrap')
    def test_native_copy_preserves_existing_target_and_rechecks_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'backup').mkdir()
            (root/'source.txt').write_bytes(b'new')
            (root/'backup/source.txt').write_bytes(b'original target')
            plan=parse_operation_plan(json.dumps({'status':'ready','steps':[{'operation':'copy_files',
                'parameters':{'sources':['source.txt'],'destination':'backup'}}]}))
            prepared=prepare_native(prepare_plan(plan,directory))
            self.assertEqual(prepared['actions'][0]['items'][0]['destination'],'backup/source_1.txt')
            result=execute_action(prepared,prepared['actions'][0],BashExecutor())
            self.assertEqual(result['status'],'verified',result)
            self.assertEqual((root/'backup/source.txt').read_bytes(),b'original target')
            self.assertEqual((root/'backup/source_1.txt').read_bytes(),b'new')
            prepared=prepare_native(prepare_plan(plan,directory))
            (root/'source.txt').write_bytes(b'changed source')
            executor=Mock()
            result=execute_action(prepared,prepared['actions'][0],executor)
            self.assertNotEqual(result['status'],'verified')
            executor.execute_native_item.assert_not_called()

    @unittest.skipUnless(sys.platform == 'linux', '需要 Linux 文件系统链接')
    def test_outside_link_special_file_and_missing_sandbox_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root=Path(directory)
            (root/'linked').symlink_to(outside,target_is_directory=True)
            with self.assertRaises(ValueError):
                sandbox_argv(['date'],directory)
            (root/'linked').unlink()
            os.mkfifo(root/'pipe')
            with self.assertRaises(ValueError):
                sandbox_argv(['date'],directory)
            (root/'pipe').unlink()
            with patch('core.command_policy.os.name','nt'), self.assertRaises(RuntimeError):
                sandbox_argv(['date'],directory)
            with self.assertRaises(ValueError):
                review_query(['cat','../outside.txt'],directory)


if __name__ == '__main__':
    unittest.main()
