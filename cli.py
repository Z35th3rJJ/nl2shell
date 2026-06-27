import os
import subprocess

from dotenv import load_dotenv

from core.engine import Engine

load_dotenv()


def main():
    engine = Engine()
    print("智能 Shell 助手  |  输入 'exit' 退出")
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
            print(f"⚠  {cmd}")
            continue

        print(f"命令：{cmd}          ")
        confirm = input("执行？(y/n) > ").strip().lower()

        if confirm != "y":
            print("已跳过。")
            continue

        result = subprocess.run(cmd, shell=True, text=True, capture_output=True)
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="")


if __name__ == "__main__":
    main()
