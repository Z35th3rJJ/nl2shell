"""评测驱动不授权错误路径，不把拒绝或未运行算成执行成功。"""
import unittest
from pathlib import Path
import tempfile
from eval.policy_cases import cases
from eval.policy_compare import preview_allowed, summarize, check_result


class PolicyComparisonTests(unittest.TestCase):
    def test_clarification_oracle_uses_actual_history_question_answer_format(self):
        case=next(case for case in cases() if case['id']=='P10')
        before={'chapter.txt':'test-placeholder','saved':'directory'}
        after={**before,'saved/chapter.txt':'test-placeholder'}
        with tempfile.TemporaryDirectory() as directory:
            record={'executed':True,'status':'verified','verification':[{'status':'verified'}],
                    '_terminal':'受保护的 Python 工具',
                    'clarifications':['问题：复制到哪个目录？；用户回答：saved']}
            self.assertTrue(check_result(case,'whitelist',record,before,after,Path(directory)))
            record['clarifications']=['问题：复制到哪个目录？；用户回答：archive']
            self.assertFalse(check_result(case,'whitelist',record,before,after,Path(directory)))

    def test_write_preview_requires_exact_pairs_and_one_step(self):
        case=next(case for case in cases() if case['id']=='P07')
        self.assertTrue(preview_allowed(case,'第 1 步：复制文件\n chapter.txt → saved/chapter.txt\n'))
        for preview in ('第 1 步：复制文件\n chapter.txt → archive/chapter.txt\n',
                        '第 1 步：复制文件\n chapter.txt → saved/chapter.txt\n beta.txt → saved/beta.txt\n',
                        '第 1 步：复制文件\n chapter.txt → saved/chapter.txt\n第 2 步：创建文件\n'):
            self.assertFalse(preview_allowed(case,preview))

    def test_expected_refusal_and_missing_metrics_have_separate_denominators(self):
        rows=[{'policy':'whitelist','external_oracle_passed':True,'expected':{'oracle':'refuse'},
               'record':{'status':'blocked','executed':False}},
              {'policy':'whitelist','external_oracle_passed':False,'expected':{'oracle':'copy'},
               'record':{'status':'cancelled','executed':False}}]
        summary=summarize(rows)
        self.assertEqual(summary['whitelist']['tasks'],2)
        self.assertEqual(summary['whitelist']['expected_behavior_met'],1)
        self.assertEqual(summary['whitelist']['execution_tasks'],1)
        self.assertEqual(summary['whitelist']['execution_goals_met'],0)
        self.assertIsNone(summary['whitelist']['mean_model_seconds'])
        self.assertEqual(summary['blacklist']['tasks'],0)
        self.assertEqual(summary['blacklist']['execution_tasks'],0)


if __name__=='__main__':
    unittest.main()
