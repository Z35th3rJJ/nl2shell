"""最大文件查询：通过公共接口核对范围、命令及实际结果。"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from core.bash_queries import prepare_queries, review_commands, execute_query
from core.execution import BashExecutor
from core.task_plan import parse_operation_plan


class LargestFileTests(unittest.TestCase):
    def test_plan_supports_explicit_directory_and_scope(self):
        plan = parse_operation_plan(json.dumps({'status': 'ready', 'steps': [
            {'operation': 'largest_file', 'parameters': {'path': '.', 'recursive': False}}]}))
        self.assertEqual(plan.steps[0].operation, 'largest_file')
        self.assertEqual(plan.steps[0].parameters, {'path': '.', 'recursive': False})

    @unittest.skipUnless(sys.platform == 'linux', '需要 Linux/GNU find')
    def test_command_result_matches_largest_regular_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'small.txt').write_bytes(b'small')
            (root/'large.bin').write_bytes(b'\0\xff' * 500)
            plan = parse_operation_plan(json.dumps({'status': 'ready', 'steps': [
                {'operation': 'largest_file', 'parameters': {'path': '.', 'recursive': False}}]}))
            prepared = prepare_queries(plan, directory)
            review_commands(prepared, {'commands': [{'step': 1,
                'argv': prepared['actions'][0]['_required_argv'], 'explanation': '读取大小，核对最大文件'}]})
            result = execute_query(prepared, prepared['actions'][0], BashExecutor())
            self.assertEqual(result['status'], 'verified', result)
            self.assertEqual(result['largest_files'], [{'path': 'large.bin', 'size_bytes': 1000}])
            self.assertEqual((root/'large.bin').read_bytes(), b'\0\xff' * 500)


if __name__ == '__main__':
    unittest.main()
