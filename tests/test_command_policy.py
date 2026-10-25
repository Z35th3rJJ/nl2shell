"""两种命令策略的公开接口和 Linux 隔离验证。"""
from pathlib import Path
import tempfile
import unittest

from core.command_policy import review_query


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


if __name__ == '__main__':
    unittest.main()
