"""黑名单系统元数据查询的真实 Linux 接口。"""
import json
import sys
import tempfile
import unittest

from core.execution import BashExecutor


class SystemQueryTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'linux', '需要 Linux 系统元数据')
    def test_blacklist_can_read_visible_block_device_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _ = BashExecutor().execute_policy_query(
                ['lsblk','--json','--output','NAME,TYPE,SIZE'], directory)
            self.assertEqual(result.exit_code,0,result.stderr)
            self.assertIsInstance(json.loads(result.stdout)['blockdevices'],list)


if __name__ == '__main__':
    unittest.main()
