"""
engine 模块单元测试（不调 API）。
运行：python3 -m pytest tests/test_engine.py -v
"""
import pytest
from core.engine import classify_output, CLARIFY_PREFIX, CANNOT_GENERATE_PREFIX


# ── classify_output：三类前缀正确分类 ────────────────────────
@pytest.mark.parametrize("text, expected", [
    # 正常命令
    ("ls -la",                          "command"),
    ("find . -name '*.txt'",            "command"),
    ("tar -czf out.tar.gz .",           "command"),
    # CLARIFY 前缀
    ("CLARIFY: 你是要删除 .log 还是 .tmp 文件？", "clarify"),
    ("CLARIFY: 打包哪个目录？",          "clarify"),
    # CANNOT_GENERATE 前缀
    ("CANNOT_GENERATE: 意图不明",       "cannot"),
    ("CANNOT_GENERATE: 无法转为命令",   "cannot"),
    # 前缀带空白也能正确分类
    ("  CLARIFY: 要删哪里的文件？",     "clarify"),
    ("  CANNOT_GENERATE: 原因",         "cannot"),
    ("  rm -rf /tmp/test",              "command"),
])
def test_classify_output(text: str, expected: str):
    assert classify_output(text) == expected, f"classify_output({text!r}) 应返回 {expected!r}"


# ── 常量前缀值保持稳定（cli 依赖这些值） ─────────────────────
def test_prefix_constants():
    assert CLARIFY_PREFIX          == "CLARIFY:"
    assert CANNOT_GENERATE_PREFIX  == "CANNOT_GENERATE:"


def test_remember_adds_short_term_context():
    from core.engine import Engine

    engine = Engine(ssh_hosts=[])
    engine.remember("列文件", "ls")

    assert engine._history == [("列文件", "ls")]


def test_task_context_keeps_five_turns_and_includes_execution_state(monkeypatch):
    from core.engine import Engine
    from core.task_plan import TaskPlan, TaskStep

    captured = {}

    def fake_chat(messages, backend=None):
        captured["messages"] = messages
        return '{"steps":[{"command":"ls","explanation":"","expected":"","verification":""}]}'

    monkeypatch.setattr("core.engine.chat", fake_chat)
    engine = Engine(ssh_hosts=[])
    for index in range(6):
        plan = TaskPlan((TaskStep(f"touch file{index}", "", "", ""),))
        engine.remember_task(f"任务{index}", f"/work/{index}", plan,
                             "cancelled" if index == 5 else "verified", index != 5)

    engine.generate_task_plan("删除file5", "/work/5")
    assert len(engine._task_history) == 5
    assert engine._task_history[0].user_input == "任务1"
    prompt = captured["messages"][-1]["content"]
    assert '"status": "cancelled"' in prompt
    assert '"executed": false' in prompt
    assert '"cwd": "/work/5"' in prompt


def test_create_file_prompt_forbids_guessing_content(monkeypatch):
    from core.engine import Engine

    captured = {}
    monkeypatch.setattr(
        "core.engine.chat",
        lambda messages, backend=None: captured.setdefault("messages", messages)
        and '{"steps":[{"command":"touch admin.txt","explanation":"","expected":"","verification":""}]}',
    )
    Engine(ssh_hosts=[]).generate_task_plan("生成admin.txt文件", "/work")
    assert "使用 touch" in captured["messages"][0]["content"]
    assert "不得从文件名猜测内容" in captured["messages"][0]["content"]


@pytest.mark.parametrize("user_input", ["删除", "清理一下", "请帮我移除"])
def test_ambiguous_deletion_requires_clarification_without_calling_model(monkeypatch, user_input):
    from core.engine import Engine

    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: pytest.fail("不应调用模型"))
    plan = Engine(ssh_hosts=[]).generate_task_plan(user_input, "/work")
    assert not plan.steps
    assert "明确" in plan.clarification
