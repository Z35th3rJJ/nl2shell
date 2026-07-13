from eval.run_eval import load_testcases


def test_system_eval_contains_at_least_200_cases():
    cases = load_testcases()
    assert len(cases) >= 200
    assert len({case["id"] for case in cases}) == len(cases)
    categories = {case["category"] for case in cases}
    assert {"文件查询", "文件修改", "系统查询", "安全-HIGH", "澄清"} <= categories


def test_system_eval_contains_current_directory_python_count_case():
    cases = load_testcases()
    case = next(
        item for item in cases
        if item["input"] == "统计当前目录下的 Python 文件"
    )

    assert len(cases) == 200
    assert case["expected"] == "find . -maxdepth 1 -type f -name '*.py' | wc -l"
    assert case["expected_intent"] == "FILE_QUERY"
