"""自动模式的执行决策。"""
from .safety import SAFE


def should_auto_execute(auto: bool, risk: str) -> bool:
    return auto and risk == SAFE
