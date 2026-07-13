from eval.run_eval import load_testcases


def test_system_eval_contains_at_least_200_cases():
    cases = load_testcases()
    assert len(cases) >= 200
    assert len({case["id"] for case in cases}) == len(cases)
    categories = {case["category"] for case in cases}
    assert {"文件查询", "文件修改", "系统查询", "安全-HIGH", "澄清"} <= categories
