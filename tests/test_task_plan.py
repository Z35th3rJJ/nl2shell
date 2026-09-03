from core.task_plan import parse_task_plan
import pytest


def test_parse_json_task_plan():
    plan = parse_task_plan('{"steps":[{"command":"mkdir out","explanation":"创建目录","expected":"目录存在","verification":"test -d out"}]}')
    assert len(plan.steps) == 1
    assert plan.steps[0].verification == "test -d out"


def test_parse_structured_intent_and_entities():
    plan = parse_task_plan(
        '{"intent":"NETWORK_QUERY","operation":"find_port_process",'
        '"entities":{"port":8080},"steps":[{"command":"ss -ltnp",'
        '"explanation":"查看监听端口","expected":"显示端口","verification":""}]}'
    )
    assert plan.intent == "NETWORK_QUERY"
    assert plan.operation == "find_port_process"
    assert plan.entities == {"port": 8080}


def test_parse_model_risk_advisory():
    plan = parse_task_plan('{"risk_advisory":"warn","steps":[{"command":"ls"}]}')
    assert plan.risk_advisory == "WARN"


def test_unknown_intent_and_invalid_advisory_fall_back_safely():
    plan = parse_task_plan('{"intent":"MADE_UP","risk_advisory":"LOW","steps":[{"command":"ls"}]}')
    assert plan.intent == "UNKNOWN"
    assert plan.risk_advisory == "SAFE"


def test_old_plan_json_uses_compatible_defaults():
    plan = parse_task_plan('{"steps":[{"command":"ls"}]}')
    assert plan.intent == "UNKNOWN"
    assert plan.entities == {}


def test_legacy_two_line_output_falls_back_to_single_step():
    plan = parse_task_plan("ls -la\n列出文件")
    assert plan.steps[0].command == "ls -la"
    assert plan.steps[0].explanation == "列出文件"


def test_invalid_json_is_not_treated_as_a_shell_command():
    # 无风险提示的空 steps 仍视为格式错误
    with pytest.raises(ValueError):
        parse_task_plan('{"steps": []}')


def test_refused_high_risk_empty_steps_is_not_an_error():
    # 模型识别到危险操作时输出空 steps + 高风险，应识别为拒绝，而非格式错误
    plan = parse_task_plan('{"risk_advisory":"HIGH","steps":[]}')
    assert plan.steps == ()
    assert plan.refused is True
    assert plan.risk_advisory == "HIGH"


def test_refused_warn_empty_steps():
    plan = parse_task_plan('{"risk_advisory":"WARN","steps":[]}')
    assert plan.refused is True
    assert plan.risk_advisory == "WARN"


def test_refused_high_risk_empty_command_step():
    # 小模型的另一种拒绝表达：step 存在但 command 为空 + 高风险
    plan = parse_task_plan('{"risk_advisory":"HIGH","steps":[{"command":"","explanation":"危险"}]}')
    assert plan.refused is True
    assert plan.steps == ()
    assert plan.risk_advisory == "HIGH"


def test_non_high_risk_empty_command_steps_is_error():
    # 无高风险的完整空 command 仍视为格式错误
    with pytest.raises(ValueError):
        parse_task_plan('{"steps":[{"command":""}]}')


def test_json_with_single_trailing_backtick_is_accepted():
    plan = parse_task_plan(
        '{"steps":[{"command":"ls","explanation":"列出文件",'
        '"expected":"显示列表","verification":""}]}`'
    )
    assert plan.steps[0].command == "ls"


def test_parse_structured_clarification():
    plan = parse_task_plan('{"clarification":"要复制成什么文件名？"}')
    assert plan.steps == ()
    assert plan.clarification == "要复制成什么文件名？"
