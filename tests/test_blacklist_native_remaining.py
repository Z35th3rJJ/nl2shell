"""黑名单文件操作使用固定 Linux 参数，并保留真实文件保护。"""
import json
import io
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from cli import execute_request
from core.command_policy import native_argv, native_parent_argv, prepare_native, review_query, sandbox_argv
from core.engine import Engine
from core.execution import BashExecutor, ExecutionResult, ExecutionStartError
from core.history import HistoryStore
from core.operations import execute_action, prepare_plan
from core.task_plan import parse_operation_plan


class NativeRemainingContracts(unittest.TestCase):
    def test_empty_file_uses_exclusive_linux_creation(self):
        self.assertEqual(native_argv('create_file',{'destination':'empty.txt'}),
                         ['dd','if=/dev/null','of=./empty.txt','conv=excl','status=none'])
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
            review_query(['dd','if=/dev/null','of=./empty.txt'],directory)

    def test_organize_trash_and_restore_use_no_replace_move(self):
        identifier='a'*32
        for operation,source,destination in (
            ('organize_files','a.txt','txt/a.txt'),
            ('trash','a.txt',f'.nl2shell-trash/{identifier}/data'),
            ('restore',f'.nl2shell-trash/{identifier}/data','a.txt')):
            with self.subTest(operation=operation):
                item={'source':source,'destination':destination,'trash_id':identifier}
                self.assertEqual(native_argv(operation,item),['mv','--update=none','--no-copy',
                                 '--no-target-directory','--','./'+source,'./'+destination])

    def test_start_failure_without_prior_write_is_not_recorded_as_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            plan=parse_operation_plan('{"status":"ready","steps":[{"operation":"create_file",'
                                      '"parameters":{"path":"empty.txt"}}]}')
            prepared=prepare_plan(plan,directory)
            action=prepared['actions'][0]
            action['items'][0]['native_argv']=native_argv('create_file',action['items'][0])
            executor=Mock()
            executor.execute_native_item.side_effect=ExecutionStartError(2,'synthetic start failure')
            result=execute_action(prepared,action,executor)
            self.assertEqual(result['status'],'execution_failed')
            self.assertEqual(result['execution_state'],'not_started')
            self.assertFalse(result['executed'])
            self.assertEqual(result['file_change_state'],'unchanged')
            self.assertFalse((Path(directory)/'empty.txt').exists())

    def test_start_failure_after_parent_creation_records_existing_change(self):
        with tempfile.TemporaryDirectory() as directory:
            plan=parse_operation_plan('{"status":"ready","steps":[{"operation":"create_file",'
                                      '"parameters":{"path":"new/empty.txt"}}]}')
            prepared=prepare_plan(plan,directory)
            action=prepared['actions'][0]
            item=action['items'][0]
            item['native_argv']=native_argv('create_file',item)
            item['parent_argv']=native_parent_argv(item)
            executor=Mock()
            def parent(*args,**kwargs):
                (Path(directory)/'new').mkdir()
                return ExecutionResult(0,'','',.01),['synthetic launcher']
            executor.execute_native_parent.side_effect=parent
            executor.execute_native_item.side_effect=ExecutionStartError(2,'synthetic start failure')
            result=execute_action(prepared,action,executor)
            self.assertEqual(result['status'],'execution_failed')
            self.assertEqual(result['execution_state'],'stopped')
            self.assertTrue(result['executed'])
            self.assertEqual(result['file_change_state'],'changed')
            self.assertTrue((Path(directory)/'new').is_dir())
            self.assertFalse((Path(directory)/'new/empty.txt').exists())


@unittest.skipUnless(sys.platform=='linux','需要 Linux/bwrap')
class NativeRemainingLinuxTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'workspace'
        self.root.mkdir()
        self.executor=BashExecutor()

    def prepare(self,operation,**parameters):
        plan=parse_operation_plan(json.dumps({'status':'ready','steps':[{'operation':operation,'parameters':parameters}]}))
        return prepare_native(prepare_plan(plan,str(self.root)))

    def test_create_empty_file_and_parent_use_linux_commands(self):
        prepared=self.prepare('create_file',path='new_folder/empty.txt')
        item=prepared['actions'][0]['items'][0]
        self.assertEqual(item['parent_argv'],['mkdir','--parents','--','./new_folder'])
        result=execute_action(prepared,prepared['actions'][0],self.executor)
        self.assertEqual(result['status'],'verified',result)
        self.assertEqual((self.root/'new_folder/empty.txt').read_bytes(),b'')
        self.assertEqual([argv[0] for argv in result['actual_commands']],['mkdir','dd'])

    def test_empty_file_race_does_not_overwrite_existing_content(self):
        prepared=self.prepare('create_file',path='empty.txt')
        original=BashExecutor._execute_argv
        def race(executor,argv,*args,**kwargs):
            (self.root/'empty.txt').write_bytes(b'keep-existing')
            return original(executor,argv,*args,**kwargs)
        with patch.object(BashExecutor,'_execute_argv',race):
            result=execute_action(prepared,prepared['actions'][0],self.executor)
        self.assertEqual(result['status'],'execution_failed',result)
        self.assertEqual((self.root/'empty.txt').read_bytes(),b'keep-existing')

    def test_organize_uses_linux_moves_and_preserves_content(self):
        (self.root/'a.txt').write_bytes(b'text')
        (self.root/'b.log').write_bytes(b'log')
        prepared=self.prepare('organize_files',path='.',recursive=False,group_by='extension')
        result=execute_action(prepared,prepared['actions'][0],self.executor)
        self.assertEqual(result['status'],'verified',result)
        self.assertEqual((self.root/'txt/a.txt').read_bytes(),b'text')
        self.assertEqual((self.root/'log/b.log').read_bytes(),b'log')
        self.assertFalse((self.root/'a.txt').exists())
        self.assertEqual([argv[0] for argv in result['actual_commands']].count('mv'),2)
        self.assertEqual([argv[0] for argv in result['actual_commands']].count('mkdir'),2)

    def test_trash_and_restore_keep_record_and_use_sandboxed_move(self):
        (self.root/'a.txt').write_bytes(b'original')
        trash=self.prepare('trash',sources=['a.txt'])
        item=trash['actions'][0]['items'][0]
        result=execute_action(trash,trash['actions'][0],self.executor)
        self.assertEqual(result['status'],'verified',result)
        self.assertEqual(result['actual_commands'][0][0],'mv')
        self.assertFalse((self.root/'a.txt').exists())
        entry=self.root/'.nl2shell-trash'/item['trash_id']
        self.assertEqual((entry/'data').read_bytes(),b'original')
        self.assertEqual(json.loads((entry/'record.json').read_text())['original'],'a.txt')
        restored=self.prepare('restore',trash_id=item['trash_id'])
        result=execute_action(restored,restored['actions'][0],self.executor)
        self.assertEqual(result['status'],'verified',result)
        self.assertEqual(result['actual_commands'][0][0],'mv')
        self.assertEqual((self.root/'a.txt').read_bytes(),b'original')
        self.assertFalse((entry/'data').exists())

    def test_tampered_trash_binding_is_rejected_before_launch(self):
        (self.root/'a.txt').write_bytes(b'original')
        prepared=self.prepare('trash',sources=['a.txt'])
        item=prepared['actions'][0]['items'][0]
        item['trash_id']='b'*32
        with patch.object(BashExecutor,'_execute_argv') as launch, self.assertRaises(ValueError):
            self.executor.execute_native_item(prepared,prepared['actions'][0],item)
        launch.assert_not_called()
        self.assertEqual((self.root/'a.txt').read_bytes(),b'original')

    def test_trash_sandbox_cannot_see_other_recovery_entries(self):
        own='a'*32
        other='b'*32
        for identifier in (own,other):
            entry=self.root/'.nl2shell-trash'/identifier
            entry.mkdir(parents=True)
            (entry/'data').write_bytes(identifier.encode())
        argv=sandbox_argv(['find','.nl2shell-trash','-type','f'],str(self.root),
                          writable=True,internal_entry=own)
        result=subprocess.run(argv,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn(own,result.stdout)
        self.assertNotIn(other,result.stdout)
        self.assertEqual((self.root/'.nl2shell-trash'/other/'data').read_bytes(),other.encode())

    def test_cli_confirmation_freezes_trash_id_and_shows_actual_linux_move(self):
        (self.root/'a.txt').write_bytes(b'original')
        plan=parse_operation_plan(json.dumps({'status':'ready','steps':[{'operation':'trash',
                                   'parameters':{'sources':['a.txt']}}]}))
        engine=Engine(ssh_hosts=[])
        engine.command_policy='blacklist'
        history=HistoryStore(Path(self.temp.name)/'history.jsonl')
        seen=[]
        def confirm(prompt):
            self.assertTrue((self.root/'a.txt').exists())
            self.assertFalse((self.root/'.nl2shell-trash').exists())
            seen.append(prompt)
            return 'yes'
        output=io.StringIO()
        with patch('core.command_policy.chat',return_value='{"status":"protected"}'), \
                patch.object(engine,'generate_task_plan',return_value=plan), \
                patch.dict(os.environ,{'NO_COLOR':'1'}), redirect_stdout(output):
            status=execute_request(engine,self.executor,history,'将 a.txt 移入回收区',str(self.root),
                                   'confirm',input_fn=confirm)
        self.assertEqual(status,'verified',history.last_record)
        self.assertEqual(len(seen),1)
        self.assertIn('回收索引：由受保护的 Python 工具记录',output.getvalue())
        outcome=history.last_record['verification'][0]
        self.assertIn(shlex.join(outcome['actual_commands'][0]),output.getvalue())
        identifier=outcome['items'][0]['trash_id']
        self.assertEqual((self.root/'.nl2shell-trash'/identifier/'data').read_bytes(),b'original')
        self.assertFalse((self.root/'a.txt').exists())
        with patch('core.command_policy.chat',side_effect=AssertionError('内置恢复不应调用模型')), \
                redirect_stdout(io.StringIO()):
            status=execute_request(engine,self.executor,history,'/restore '+identifier,str(self.root),
                                   'confirm',input_fn=lambda _: 'yes')
        self.assertEqual(status,'verified',history.last_record)
        self.assertEqual((self.root/'a.txt').read_bytes(),b'original')

    def test_cli_cancellation_never_creates_trash_record_or_moves_file(self):
        (self.root/'a.txt').write_bytes(b'original')
        plan=parse_operation_plan(json.dumps({'status':'ready','steps':[{'operation':'trash',
                                   'parameters':{'sources':['a.txt']}}]}))
        engine=Engine(ssh_hosts=[])
        engine.command_policy='blacklist'
        history=HistoryStore(Path(self.temp.name)/'history.jsonl')
        with patch('core.command_policy.chat',return_value='{"status":"protected"}'), \
                patch.object(engine,'generate_task_plan',return_value=plan), redirect_stdout(io.StringIO()):
            status=execute_request(engine,self.executor,history,'将 a.txt 移入回收区',str(self.root),
                                   'confirm',input_fn=lambda _: 'no')
        self.assertEqual(status,'cancelled')
        self.assertFalse(history.last_record['executed'])
        self.assertEqual((self.root/'a.txt').read_bytes(),b'original')
        self.assertFalse((self.root/'.nl2shell-trash').exists())

    def test_cli_multiple_trash_items_keep_distinct_confirmed_bindings(self):
        (self.root/'a.txt').write_bytes(b'first')
        (self.root/'b.txt').write_bytes(b'second')
        plan=parse_operation_plan(json.dumps({'status':'ready','steps':[{'operation':'trash',
                                   'parameters':{'sources':['a.txt','b.txt']}}]}))
        engine=Engine(ssh_hosts=[])
        engine.command_policy='blacklist'
        history=HistoryStore(Path(self.temp.name)/'history.jsonl')
        output=io.StringIO()
        def confirm(prompt):
            self.assertEqual((self.root/'a.txt').read_bytes(),b'first')
            self.assertEqual((self.root/'b.txt').read_bytes(),b'second')
            self.assertFalse((self.root/'.nl2shell-trash').exists())
            return 'yes'
        with patch('core.command_policy.chat',return_value='{"status":"protected"}'), \
                patch.object(engine,'generate_task_plan',return_value=plan), \
                patch.dict(os.environ,{'NO_COLOR':'1'}), redirect_stdout(output):
            status=execute_request(engine,self.executor,history,'将 a.txt 和 b.txt 放入回收区',str(self.root),
                                   'confirm',input_fn=confirm)
        self.assertEqual(status,'verified',history.last_record)
        outcome=history.last_record['verification'][0]
        self.assertEqual(len(outcome['actual_commands']),2)
        identifiers=[item['trash_id'] for item in outcome['items']]
        self.assertEqual(len(set(identifiers)),2)
        for item,content in zip(outcome['items'],(b'first',b'second')):
            self.assertEqual((self.root/'.nl2shell-trash'/item['trash_id']/'data').read_bytes(),content)
        for command in outcome['actual_commands']:
            self.assertIn(shlex.join(command),output.getvalue())
        self.assertFalse((self.root/'a.txt').exists())
        self.assertFalse((self.root/'b.txt').exists())


if __name__=='__main__':
    unittest.main()
