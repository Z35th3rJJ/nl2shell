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


@pytest.mark.parametrize("user_input", ["删除", "清理一下", "请帮我移除"])
def test_ambiguous_deletion_requires_clarification_without_calling_model(monkeypatch, user_input):
    from core.engine import Engine

    monkeypatch.setattr("core.engine.chat", lambda *args, **kwargs: pytest.fail("不应调用模型"))
    plan = Engine(ssh_hosts=[]).generate_task_plan(user_input, "/work")
    assert not plan.steps
    assert "明确" in plan.clarification
