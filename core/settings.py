"""读取、交互调整并保存 CLI 运行设置。"""
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .policy import MANUAL, POLICIES


ENV_PATH = Path(__file__).parent.parent / ".env"
_RUNTIME_KEYS = ("AGENT_MODE", "EXECUTION_POLICY", "DRY_RUN", "AUTO_EXECUTE")


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    if value.lower() in {"1", "true", "yes", "on"}:
        return True
    if value.lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 必须是 true 或 false")


@dataclass(frozen=True)
class AppSettings:
    auto_execute: bool
    policy: str
    dry_run: bool
    agent_mode: bool


def load_settings() -> AppSettings:
    policy = os.environ.get("EXECUTION_POLICY", MANUAL).lower()
    if policy not in POLICIES:
        raise ValueError(f"EXECUTION_POLICY 必须是：{', '.join(POLICIES)}")
    return AppSettings(
        auto_execute=_bool("AUTO_EXECUTE", False),
        policy=policy,
        dry_run=_bool("DRY_RUN", False),
        agent_mode=_bool("AGENT_MODE", False),
    )


def format_settings(settings: AppSettings) -> str:
    return (
        f"  Agent 模式：{'开启' if settings.agent_mode else '关闭'}\n"
        f"  执行策略：{settings.policy}\n"
        f"  Dry-run：{'开启' if settings.dry_run else '关闭'}\n"
        f"  自动执行：{'开启' if settings.auto_execute else '关闭'}"
    )


def _choose_bool(label: str, current: bool, input_fn: Callable[[str], str], output_fn: Callable[[str], None]) -> bool:
    default = "y" if current else "n"
    while True:
        answer = input_fn(f"{label}？(y/n，直接回车保持 {default}) > ").strip().lower()
        if not answer:
            return current
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        output_fn("请输入 y 或 n。")


def _choose_policy(current: str, input_fn: Callable[[str], str], output_fn: Callable[[str], None]) -> str:
    options = {"1": "read-only", "2": "workspace", "3": "manual"}
    output_fn("执行策略：1. read-only  2. workspace  3. manual")
    while True:
        answer = input_fn(f"请选择（直接回车保持 {current}）> ").strip()
        if not answer:
            return current
        if answer in options:
            return options[answer]
        output_fn("请输入 1、2 或 3。")


def save_runtime_settings(settings: AppSettings, env_path: Path = ENV_PATH) -> bool:
    """只更新运行模式键，保留 .env 的其他内容与行内注释。"""
    if not env_path.exists():
        return False
    values = {
        "AGENT_MODE": str(settings.agent_mode).lower(),
        "EXECUTION_POLICY": settings.policy,
        "DRY_RUN": str(settings.dry_run).lower(),
        "AUTO_EXECUTE": str(settings.auto_execute).lower(),
    }
    lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
    seen = set()
    updated = []
    for line in lines:
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
    for key in _RUNTIME_KEYS:
        if key not in seen:
            updated.append(f"{key}={values[key]}\n")
    env_path.write_text("".join(updated), encoding="utf-8")
    return True


def choose_runtime_settings(
    current: AppSettings,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    env_path: Path = ENV_PATH,
) -> AppSettings | None:
    """显示启动菜单，返回本次设置；None 表示用户选择退出。"""
    output_fn("\n当前运行配置：\n" + format_settings(current))
    try:
        while True:
            choice = input_fn("启动方式：1. 直接启动  2. 本次调整  0. 退出 > ").strip()
            if choice in {"", "1"}:
                return current
            if choice == "0":
                return None
            if choice == "2":
                break
            output_fn("请输入 1、2 或 0。")

        selected = AppSettings(
            agent_mode=_choose_bool("启用 Agent 模式", current.agent_mode, input_fn, output_fn),
            policy=_choose_policy(current.policy, input_fn, output_fn),
            dry_run=_choose_bool("启用 Dry-run", current.dry_run, input_fn, output_fn),
            auto_execute=_choose_bool("启用自动执行", current.auto_execute, input_fn, output_fn),
        )
        output_fn("\n本次运行配置：\n" + format_settings(selected))
        save = input_fn("保存为 .env 默认配置？(y/N) > ").strip().lower()
        if save in {"y", "yes"}:
            if save_runtime_settings(selected, env_path):
                output_fn("已保存到 .env。")
            else:
                output_fn("未找到 .env，未保存；请先从 .env.example 创建 .env。")
        return selected
    except EOFError:
        output_fn("输入结束，已退出。")
        return None
