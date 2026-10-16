"""Terminal task separators and phase layout regression tests."""
import io
from contextlib import redirect_stdout
from unittest.mock import Mock

import cli
from core.settings import PREVIEW
from tests.test_cli_flow import History, engine_for, step


def test_three_tasks_have_distinct_boundaries(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_TASK_SERIAL", 0)
    output = io.StringIO()
    with redirect_stdout(output):
        for index in range(3):
            status = cli.execute_request(
                engine_for(step("create_file", path=f"item_{index}.txt")),
                Mock(), History(), f"创建 item_{index}.txt", str(tmp_path), PREVIEW,
            )
            assert status == "preview"
    text = output.getvalue()
    for number in range(1, 4):
        assert text.count(f"任务 {number:02d} 结束 · 仅预览") == 1
        assert text.count(f"任务 {number:02d}\n") == 1
    assert text.count("01 / 执行预览") == 3
    assert "02 / 等待确认" not in text
    assert text.count("03 / 执行与验证") == 3
    assert text.index("任务 01 结束") < text.index("任务 02\n")
    assert text.index("任务 02 结束") < text.index("任务 03\n")
    assert "已验证成功" not in text
    assert not any(tmp_path.iterdir())


def test_confirmation_and_execution_have_distinct_phases(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_TASK_SERIAL", 0)
    output = io.StringIO()
    prompts = []
    def cancel(prompt):
        prompts.append(prompt)
        return "no"
    with redirect_stdout(output):
        status = cli.execute_request(
            engine_for(step("create_file", path="sample.txt")), Mock(), History(),
            "创建 sample.txt", str(tmp_path), "confirm", input_fn=cancel,
        )
    text = output.getvalue()
    assert status == "cancelled"
    assert text.index("01 / 执行预览") < text.index("02 / 等待确认") < text.index("03 / 执行与验证")
    assert "任务 01 结束 · 已取消" in text
    assert "执行状态：未执行" in text
    assert len(prompts) == 1 and "yes" in prompts[0]
    assert not (tmp_path / "sample.txt").exists()


def test_rule_width_is_bounded(monkeypatch):
    monkeypatch.setattr(cli.shutil, "get_terminal_size", lambda fallback: __import__("os").terminal_size((24, 20)))
    output = io.StringIO()
    with redirect_stdout(output):
        cli._rule("━")
    assert output.getvalue().strip() == "━" * 24


def test_no_color_task_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(cli, "_TASK_SERIAL", 0)
    output = io.StringIO()
    with redirect_stdout(output):
        cli.execute_request(
            engine_for(step("create_file", path="check.txt")), Mock(), History(),
            "创建 check.txt", str(tmp_path), PREVIEW,
        )
    text = output.getvalue()
    assert "\x1b" not in text
    assert "任务 01 结束 · 仅预览" in text
