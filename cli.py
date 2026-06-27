import os
import subprocess

from dotenv import load_dotenv

from core.engine import Engine
from core.safety import check, HIGH, WARN

load_dotenv()

# ANSI 颜色
RED    = "\033[91m"
YELLOW = "\033[93m"
GREEN  = "\033[92m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


def run(cmd: str) -> None:
    result = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="")


def main():
    engine = Engine()
    print(f"{BOLD}智能 Shell 助手{RESET}  |  输入 'exit' 退出")
    print("─" * 40)

    while True:
        try:
            user_input = input("\n你想做什么？> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "退出"):
            print("再见！")
            break

        cwd = os.getcwd()
        print("生成中...", end="\r")

        cmd = engine.generate(user_input, cwd)

        if cmd.startswith("CANNOT_GENERATE:"):
            print(f"{YELLOW}⚠  {cmd}{RESET}")
            continue

        # 安全检查
        risk, reason = check(cmd)

        if risk == HIGH:
            print(f"\n{RED}{BOLD}【高危命令】{RESET}")
            print(f"  命令：{RED}{cmd}{RESET}")
            print(f"  风险：{RED}{reason}{RESET}")
            confirm = input(f"\n{RED}确认执行请输入 yes（其他任意键取消）> {RESET}").strip()
            if confirm != "yes":
                print("已取消。")
                continue

        elif risk == WARN:
            print(f"\n{YELLOW}【注意】{reason}{RESET}")
            print(f"  命令：{YELLOW}{cmd}{RESET}")
            confirm = input("执行？(y/n) > ").strip().lower()
            if confirm != "y":
                print("已取消。")
                continue

        else:
            print(f"  命令：{GREEN}{cmd}{RESET}")
            confirm = input("执行？(y/n) > ").strip().lower()
            if confirm != "y":
                print("已跳过。")
                continue

        run(cmd)


if __name__ == "__main__":
    main()
