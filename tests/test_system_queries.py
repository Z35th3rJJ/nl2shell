"""黑名单系统元数据查询的真实 Linux 接口。"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from core.execution import BashExecutor
from core.command_policy import review_query, sandbox_argv
from core.system_view import system_view, SYSTEM_VIEW


class SystemQueryTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'linux', '需要 Linux 系统元数据')
    def test_blacklist_can_read_visible_block_device_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _ = BashExecutor().execute_policy_query(
                ['lsblk','--json','--output','NAME,TYPE,SIZE'], directory)
            self.assertEqual(result.exit_code,0,result.stderr)
            self.assertIsInstance(json.loads(result.stdout)['blockdevices'],list)

    def test_metadata_binding_is_idempotent_and_rejects_other_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            for argv in (['lsblk','--json'],['findmnt','--json']):
                reviewed=review_query(argv,directory)
                self.assertIn(SYSTEM_VIEW, ' '.join(reviewed))
                self.assertEqual(review_query(reviewed,directory),reviewed)
            for argv in (['lsblk','--sysroot','/proc'],['findmnt','-F','/proc/mounts'],
                         ['findmnt','-N1'],['findmnt','--task=1']):
                with self.subTest(argv=argv), self.assertRaises(ValueError):
                    review_query(argv,directory)

    def test_only_mount_listing_and_bounded_text_filters_are_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            for argv in (['mount'],['mount','-l'],['awk','{print $1}','sample.txt'],
                         ['sed','--sandbox','1,2p','sample.txt'],['cat','/proc/cpuinfo'],['ls','/sys/class/block']):
                self.assertEqual(review_query(argv,directory),argv)
            for argv in (['mount','/dev/sda','/tmp/disk'],['mount','-o','remount,rw','/'],
                         ['sed','--sandbox','-i','s/a/b/','sample.txt'],['sed','1p','sample.txt'],
                         ['awk','BEGIN {system("id")}'],['awk','{print > "out"}'],
                         ['awk','-fscript'],['gawk','--load=plugin'],['rm','source.txt'],
                         ['cat','/dev/sda'],['cat','/home/test-placeholder'],['cat','/mnt/c/test-placeholder'],
                         ['cat','/sys/../home/test-placeholder']):
                with self.subTest(argv=argv), self.assertRaises(ValueError):
                    review_query(argv,directory)

    @unittest.skipUnless(sys.platform == 'linux', '需要 Linux/bwrap')
    def test_actual_mount_input_is_host_snapshot_and_not_sandbox_mount_table(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'sample.txt').write_bytes(b'unchanged')
            for argv in (['findmnt','--json','--output','SOURCE,TARGET,FSTYPE'],['mount']):
                result,launcher=BashExecutor().execute_policy_query(argv,directory)
                self.assertEqual(result.exit_code,0,result.stderr)
                self.assertIsNotNone(result.system_metadata)
                self.assertIn(SYSTEM_VIEW,launcher)
                self.assertNotIn('/workspace',result.stdout)
                if argv[0]=='findmnt':
                    entries=json.loads(result.stdout)['filesystems']
                    self.assertTrue(any(item['target']=='/' for item in entries))
                self.assertGreater(result.system_metadata['mount_record_count'],0)
            self.assertEqual((root/'sample.txt').read_bytes(),b'unchanged')
            self.assertEqual(sorted(p.name for p in root.iterdir()),['sample.txt'])

    @unittest.skipUnless(sys.platform == 'linux', '需要 Linux/bwrap')
    def test_text_filters_work_without_external_execution_or_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'sample.txt').write_text('one a\ntwo b\nthree c\n')
            for argv,expected in ((['awk','{print $1}','sample.txt'],'one\ntwo\nthree\n'),
                                  (['sed','--sandbox','-n','1,2p','sample.txt'],'one a\ntwo b\n')):
                result,_=BashExecutor().execute_policy_query(argv,directory)
                self.assertEqual(result.exit_code,0,result.stderr)
                self.assertEqual(result.stdout,expected)
            result,_=BashExecutor().execute_policy_query(['sed','--sandbox','1e id','sample.txt'],directory)
            self.assertNotEqual(result.exit_code,0)
            for alias in ('awk','nawk'):
                if (Path('/usr/bin')/alias).is_file():
                    with self.assertRaises(ValueError):
                        sandbox_argv([alias,'BEGIN {system("id")}'],directory)
            self.assertEqual((root/'sample.txt').read_text(),'one a\ntwo b\nthree c\n')
            self.assertEqual(sorted(p.name for p in root.iterdir()),['sample.txt'])

    @unittest.skipUnless(sys.platform == 'linux', '需要 Linux 元数据')
    def test_snapshot_is_bounded_and_removed_after_use(self):
        with system_view() as (root,summary):
            self.assertEqual((root/'proc/self/mountinfo').read_bytes(),Path('/proc/self/mountinfo').read_bytes())
            expected={p.name for p in Path('/sys/class/block').iterdir()}
            self.assertEqual({p['name'] for p in summary['block_devices']},expected)
            self.assertEqual(summary['block_device_count'],len(expected))
        self.assertFalse(root.exists())
        from unittest.mock import patch
        with patch('core.system_view.METADATA_LIMIT',0), self.assertRaises(ValueError):
            with system_view():
                self.fail('超过预算不能发布快照')


if __name__ == '__main__':
    unittest.main()
