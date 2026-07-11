from core.task_plan import parse_task_plan
import pytest


def test_parse_json_task_plan():
    plan = parse_task_plan('{"steps":[{"command":"mkdir out","explanation":"创建目录","expected":"目录存在","verification":"test -d out"}]}')
    assert len(plan.steps) == 1
    assert plan.steps[0].verification == "test -d out"


def test_legacy_two_line_output_falls_back_to_single_step():
    plan = parse_task_plan("ls -la\n列出文件")
    assert plan.steps[0].command == "ls -la"
    assert plan.steps[0].explanation == "列出文件"


def test_invalid_json_is_not_treated_as_a_shell_command():
    with pytest.raises(ValueError):
        parse_task_plan('{"steps": []}')


def test_json_with_single_trailing_backtick_is_accepted():
    plan = parse_task_plan(
        '{"steps":[{"command":"ls","explanation":"列出文件",'
        '"expected":"显示列表","verification":""}]}`'
    )
    assert plan.steps[0].command == "ls"
