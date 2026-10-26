"""已有只读需求不重复交给通用模型；真实 Linux 执行仍经过隔离与验证。"""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from cli import execute_request
from core.engine import Engine
from core.execution import BashExecutor
from core.history import HistoryStore
from core.requirements import resolve_request


class BuiltinRequirementTests(unittest.TestCase):
    def test_directory_literal_and_inode_have_complete_request_contracts(self):
        for request,operation,parameters in (
            ('列出当前目录这一层的文件和文件夹，不列隐藏项','list_directory',{'path':'.','include_hidden':False}),
            ('在 sample.txt 中搜索字面文本 ERROR.*','search_text',{'path':'sample.txt','pattern':'ERROR.*'}),
            ('在 sample.txt 中搜索字面文本 [1]','search_text',{'path':'sample.txt','pattern':'[1]'}),
            ('显示 chapter.txt 的 inode','file_inode',{'path':'chapter.txt'})):
            with self.subTest(request=request):
                plan=resolve_request(request)
                self.assertIsNotNone(plan)
                self.assertEqual(plan.steps[0].operation,operation)
                self.assertEqual(plan.steps[0].parameters,parameters)

    def test_quoted_paths_and_literal_punctuation_are_preserved(self):
        plan=resolve_request('在 “项目 文件.txt” 中搜索字面文本 [1]')
        self.assertEqual(plan.steps[0].parameters,{'path':'项目 文件.txt','pattern':'[1]'})
        plan=resolve_request('列出 organize 目录的内容，包括隐藏文件')
        self.assertEqual(plan.steps[0].parameters,{'path':'organize','include_hidden':True})

    def test_extra_actions_and_ambiguous_requests_do_not_become_local_queries(self):
        for request in ('在 sample.txt 中使用正则表达式搜索 ERROR.*',
                        '递归搜索所有文件中的 ERROR',
                        '显示 chapter.txt 的 inode，然后删除它',
                        '找一下当前目录下最大的文件，并复制到 saved',
                        '列出所有文件夹，不列文件','看看那个文件'):
            with self.subTest(request=request):
                self.assertIsNone(resolve_request(request))


@unittest.skipUnless(sys.platform=='linux','需要真实 Linux/bwrap')
class BuiltinQueryCLITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'workspace'
        self.root.mkdir()
        (self.root/'src').mkdir()
        (self.root/'src/a.txt').write_bytes(b'a')
        (self.root/'src/b.txt').write_bytes(b'b')
        (self.root/'sample.txt').write_bytes(b'INFO\nERROR.* literal match\nERRORzz decoy\n[1]\n')
        (self.root/'big.bin').write_bytes(b'x'*200)
        self.history=HistoryStore(Path(self.temp.name)/'history.jsonl')
        self.engine=Engine(ssh_hosts=[])
        self.engine.command_policy='blacklist'

    def run_request(self,request,answer='yes'):
        refusal='{"status":"unsupported","reason":"known model misclassification"}'
        with patch('core.command_policy.chat',return_value=refusal) as generic, \
                patch('core.engine.chat',side_effect=AssertionError('不应重新调用规划模型')):
            status=execute_request(self.engine,BashExecutor(),self.history,request,str(self.root),'confirm',
                                   input_fn=lambda _:answer)
        generic.assert_not_called()
        return status,self.history.last_record

    def test_all_five_queries_execute_real_commands_and_verify_results(self):
        requests=[('列出当前目录这一层的文件和文件夹，不列隐藏项','list_directory'),
                  ('统计 src 目录及子目录中的文件数量','count_files'),
                  ('在 sample.txt 中搜索字面文本 ERROR.*','search_text'),
                  ('在 sample.txt 中搜索字面文本 [1]','search_text'),
                  ('显示 sample.txt 的 inode','file_inode'),
                  ('找一下当前目录下最大的文件','largest_file')]
        before={p.relative_to(self.root).as_posix():p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        for request,operation in requests:
            with self.subTest(request=request):
                status,record=self.run_request(request)
                self.assertEqual(status,'verified',record.get('block_reason'))
                result=record['verification'][0]
                self.assertEqual(result['operation'],operation)
                self.assertIn('/usr/bin/bwrap',result['launcher_argv'])
                self.assertTrue(record['authorized'])
                if operation=='count_files':
                    self.assertEqual(result['count'],2)
                elif operation=='search_text':
                    self.assertIn('-F',result['actual_argv'])
                    self.assertNotIn('ERRORzz',result['stdout'])
                elif operation=='file_inode':
                    self.assertEqual(result['inode'],(self.root/'sample.txt').stat().st_ino)
                elif operation=='largest_file':
                    self.assertEqual(result['largest_files'],[{'path':'big.bin','size_bytes':200}])
        self.assertEqual(before,{p.relative_to(self.root).as_posix():p.read_bytes()
                                 for p in self.root.rglob('*') if p.is_file()})

    def test_confirmation_cannot_be_skipped(self):
        status,record=self.run_request('统计 src 目录及子目录中的文件数量',answer='no')
        self.assertEqual(status,'cancelled')
        self.assertFalse(record['executed'])

    def test_yes_flag_cannot_replace_confirmation(self):
        with patch('core.command_policy.chat',side_effect=AssertionError('不应调用模型')):
            status=execute_request(self.engine,BashExecutor(),self.history,
                '统计 src 目录及子目录中的文件数量',str(self.root),'confirm',assume_yes=True,
                input_fn=lambda _:self.fail('不应进入隐式确认'))
        self.assertEqual(status,'cancelled')
        self.assertFalse(self.history.last_record['executed'])

    def test_path_and_sensitive_file_rejections_do_not_start_commands(self):
        for request in ('显示 ../outside.txt 的 inode','显示 .env 的 inode'):
            with self.subTest(request=request), patch.object(BashExecutor,'_execute_argv') as execute:
                status,record=self.run_request(request)
                self.assertEqual(status,'blocked')
                self.assertFalse(record['executed'])
                execute.assert_not_called()


if __name__=='__main__':
    unittest.main()
