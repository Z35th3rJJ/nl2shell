"""三种运行方式、首次向导和 .env 迁移。"""
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


PREVIEW = "preview"
CONFIRM_MODE = "confirm"
AUTO_SAFE = "auto-safe"
RUN_MODES = (PREVIEW, CONFIRM_MODE, AUTO_SAFE)
ENV_PATH = Path(__file__).parent.parent / ".env"
_LEGACY_KEYS = {"AGENT_MODE", "EXECUTION_POLICY", "DRY_RUN", "AUTO_EXECUTE"}


@dataclass(frozen=True)
class AppSettings:
    run_mode: str
    setup_complete: bool


def _truthy(value: str | None) -> bool:
    return (value or "").lower() in {"1", "true", "yes", "on"}


def load_settings() -> AppSettings:
    mode = os.environ.get("RUN_MODE")
    if mode is None:
        if _truthy(os.environ.get("DRY_RUN")):
            mode = PREVIEW
        elif _truthy(os.environ.get("AUTO_EXECUTE")):
            mode = AUTO_SAFE
        else:
            mode = CONFIRM_MODE
    mode = mode.lower()
    if mode not in RUN_MODES:
        raise ValueError(f"RUN_MODE 必须是：{', '.join(RUN_MODES)}")
    return AppSettings(mode, _truthy(os.environ.get("SETUP_COMPLETE")))


def mode_name(mode: str) -> str:
    return {PREVIEW: "预览", CONFIRM_MODE: "确认执行", AUTO_SAFE: "安全自动"}[mode]


def mode_description(mode: str) -> str:
    return {
        PREVIEW: "只展示任务计划、风险和验证方案，永远不执行 Bash。",
        CONFIRM_MODE: "展示完整计划，确认后执行；敏感操作需要更严格确认。",
        AUTO_SAFE: "安全操作自动执行；删除、网络等敏感操作会暂停确认，高危操作阻止。",
    }[mode]


def choose_mode(current: str, input_fn: Callable[[str], str] = input,
                output_fn: Callable[[str], None] = print) -> str | None:
    output_fn(
        "运行方式：\n"
        "  1. 预览：只看计划和风险，不执行命令。\n"
        "  2. 确认执行（推荐）：确认整份计划后执行，并验证结果。\n"
        "  3. 安全自动：安全操作自动执行，敏感操作仍会询问。\n"
        "  0. 取消"
    )
    options = {"1": PREVIEW, "2": CONFIRM_MODE, "3": AUTO_SAFE}
    while True:
        try:
            answer = input_fn(f"请选择（直接回车保持“{mode_name(current)}”）> ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not answer:
            return current
        if answer == "0":
            return None
        if answer in options:
            return options[answer]
        output_fn("请输入 1、2、3 或 0。")


def save_settings(settings: AppSettings, env_path: Path = ENV_PATH) -> bool:
    if not env_path.exists():
        return False
    values = {"RUN_MODE": settings.run_mode, "SETUP_COMPLETE": "true"}
    lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
    updated, seen = [], set()
    for line in lines:
        key_match = re.match(r"^\s*([A-Z_]+)\s*=", line)
        if key_match and key_match.group(1) in _LEGACY_KEYS:
            continue
        replacement = line
        for key, value in values.items():
            match = re.match(rf"^(\s*{key}\s*=\s*)[^#\r\n]*(.*?)(\r?\n)?$", line)
            if match:
                replacement = f"{match.group(1)}{value}{match.group(2)}{match.group(3) or ''}"
                seen.add(key)
                break
        updated.append(replacement)
    if updated and not updated[-1].endswith(("\n", "\r")):
        updated[-1] += "\n"
    for key, value in values.items():
        if key not in seen:
            updated.append(f"{key}={value}\n")
    env_path.write_text("".join(updated), encoding="utf-8")
    return True


def first_run_setup(settings: AppSettings, input_fn: Callable[[str], str] = input,
                    output_fn: Callable[[str], None] = print,
                    env_path: Path = ENV_PATH) -> AppSettings | None:
    if settings.setup_complete:
        return settings
    output_fn("首次运行设置：请选择默认运行方式。以后可用 /config 修改，或用 /mode 临时切换。")
    mode = choose_mode(settings.run_mode, input_fn, output_fn)
    if mode is None:
        return None
    selected = AppSettings(mode, True)
    if save_settings(selected, env_path):
        output_fn(f"默认方式已保存：{mode_name(mode)}。")
    else:
        output_fn("未找到 .env，本次设置不会持久化。")
    return selected
