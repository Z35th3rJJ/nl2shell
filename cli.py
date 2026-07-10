import argparse
import os

from dotenv import load_dotenv

from core.automation import should_auto_execute
from core.engine import Engine, classify_output, CLARIFY_PREFIX
from core.execution import BashExecutor, bash_unavailable_message, try_change_directory
from core.history import HistoryStore
from core.safety import check, HIGH, WARN

load_dotenv()

RED = "\033[91m"
YELLOW = "\033[93m"
GREEN = "\033[92m"
BOLD = "\033[1m"
RESET = "\033[0m"


def run(command: str, executor: BashExecutor):
    cd_result = try_change_directory(command)
    return cd_result if cd_result is not None else executor.execute(command)


def print_history(store: HistoryStore, limit: int) -> None:
    records = store.recent(limit)
    if not records:
        print("暂无历史记录。")
        return
    for record in records:
        status = "已执行" if record["executed"] else f"未执行（{record['status']}）"
        print(
            f"[{record['timestamp']}] {status}\n"
            f"  输入：{record['input']}\n"
            f"  命令：{record['command']}\n"
            f"  目录：{record['cwd']}"
        )


def save_history(store: HistoryStore, *, user_input: str, cwd: str, command: str,
                 risk: str, status: str, executed: bool, result=None) -> None:
    store.append({
        "input": user_input,
        "cwd": cwd,
        "command": command,
        "risk": risk,
        "status": status,
        "executed": executed,
        "exit_code": result.exit_code if result else None,
        "duration_seconds": result.duration_seconds if result else None,
        "stderr": result.stderr.strip()[:500] if result else "",
    })


def main(auto: bool = False):
    executor = BashExecutor()
    if not executor.is_available():
        print(f"{RED}错误：{bash_unavailable_message()}{RESET}")
        return

    engine = Engine()
    history = HistoryStore()
    mode = "自动执行（仅 SAFE）" if auto else "交互确认"
    print(f"{BOLD}智能 Shell 助手{RESET}  |  {mode}  |  输入 'exit' 退出")
    print("─" * 40)

    while True:
        try:
            cwd = os.getcwd()
            user_input = input(f"\n[{cwd}]\n你想做什么？> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "退出"):
            print("再见！")
            break
        if user_input == "history" or user_input.startswith("history "):
            try:
                limit = int(user_input.split(maxsplit=1)[1]) if " " in user_input else 20
            except ValueError:
                print(f"{YELLOW}history 后只能输入正整数。{RESET}")
                continue
            if limit <= 0:
                print(f"{YELLOW}history 后只能输入正整数。{RESET}")
                continue
            print_history(history, limit)
            continue

        print("生成中...", end="\r")
        followups: list[tuple[str, str]] = []
        command = explanation = ""
        for _ in range(3):
            try:
                command, explanation = engine.generate(user_input, cwd, followups=followups)
            except Exception as e:
                print(f"{RED}⚠ API 调用失败：{e}{RESET}")
                command = ""
                break

            if classify_output(command) != "clarify":
                break
            question = command[len(CLARIFY_PREFIX):].strip()
            print(f"\n{YELLOW}❓ {question}{RESET}")
            try:
                answer = input("你的回答（直接回车取消）> ").strip()
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if not answer or answer.lower() in ("exit", "退出"):
                print("已取消。")
                save_history(history, user_input=user_input, cwd=cwd, command=command,
                             risk="", status="clarify_cancelled", executed=False)
                command = ""
                break
            followups.append((command, answer))
            print("生成中...", end="\r")
        else:
            print(f"{YELLOW}⚠ 无法明确你的意图，请换种说法重新描述。{RESET}")
            continue

        if not command:
            continue
        if classify_output(command) == "cannot":
            print(f"{YELLOW}⚠ {command}{RESET}")
            save_history(history, user_input=user_input, cwd=cwd, command=command,
                         risk="", status="cannot_generate", executed=False)
            continue

        risk, reason = check(command)
        if explanation:
            print(f"  说明：{explanation}")
        if auto and not should_auto_execute(auto, risk):
            print(f"{YELLOW}已跳过 {risk} 命令：{reason}{RESET}")
            save_history(history, user_input=user_input, cwd=cwd, command=command,
                         risk=risk, status="blocked_by_auto_safety", executed=False)
            engine.remember(user_input, command)
            continue

        if risk == HIGH:
            print(f"\n{RED}{BOLD}【高危命令】{RESET}\n  命令：{RED}{command}{RESET}\n  风险：{RED}{reason}{RESET}")
            confirmed = input(f"\n{RED}确认执行请输入 yes（其他任意键取消）> {RESET}").strip() == "yes"
        elif risk == WARN:
            print(f"\n{YELLOW}【注意】{reason}{RESET}\n  命令：{YELLOW}{command}{RESET}")
            confirmed = input("执行？(y/n) > ").strip().lower() == "y"
        else:
            print(f"  命令：{GREEN}{command}{RESET}")
            confirmed = should_auto_execute(auto, risk) or input("执行？(y/n) > ").strip().lower() == "y"

        if not confirmed:
            print("已取消。")
            save_history(history, user_input=user_input, cwd=cwd, command=command,
                         risk=risk, status="cancelled", executed=False)
            engine.remember(user_input, command)
            continue

        try:
            result = run(command, executor)
        except Exception as e:
            print(f"{RED}⚠ 执行失败：{e}{RESET}")
            save_history(history, user_input=user_input, cwd=cwd, command=command,
                         risk=risk, status="execution_error", executed=True)
            engine.remember(user_input, command)
            continue
        if result.stdout:
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
        if result.stderr:
            print(f"{RED}{result.stderr}{RESET}", end="" if result.stderr.endswith("\n") else "\n")
        save_history(history, user_input=user_input, cwd=cwd, command=command, risk=risk,
                     status="executed", executed=True, result=result)
        engine.remember(user_input, command)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto", action="store_true", help="自动执行 SAFE 命令，跳过 WARN/HIGH 命令")
    main(auto=parser.parse_args().auto)
