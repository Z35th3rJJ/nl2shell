import os
import shlex
import subprocess

from dotenv import load_dotenv

from core.engine import Engine, classify_output, CLARIFY_PREFIX
from core.safety import check, HIGH, WARN

load_dotenv()

# ANSI 颜色
RED    = "\033[91m"
YELLOW = "\033[93m"
GREEN  = "\033[92m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


def _try_cd(cmd: str) -> bool:
    """若命令是 cd，直接在当前进程切换目录（子进程 cd 不影响父进程）。
    返回 True 表示已处理，不需要再走 subprocess。"""
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return False
    if not parts or parts[0] != "cd":
        return False
    target = parts[1] if len(parts) > 1 else os.path.expanduser("~")
    target = os.path.expanduser(target)
    try:
        os.chdir(target)
        print(f"已切换到：{os.getcwd()}")
    except FileNotFoundError:
        print(f"{RED}cd: 目录不存在：{target}{RESET}")
    except PermissionError:
        print(f"{RED}cd: 权限不足：{target}{RESET}")
    return True


def run(cmd: str) -> None:
    if _try_cd(cmd):
        return
    subprocess.run(cmd, shell=True)


def main():
    engine = Engine()
    print(f"{BOLD}智能 Shell 助手{RESET}  |  输入 'exit' 退出")
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

        print("生成中...", end="\r")

        # ── 澄清循环（最多 2 轮） ──────────────────────────────────
        followups: list[tuple[str, str]] = []
        MAX_CLARIFY = 2
        cmd = explanation = ""
        for _ in range(MAX_CLARIFY + 1):
            try:
                cmd, explanation = engine.generate(user_input, cwd, followups=followups)
            except Exception as e:
                print(f"{RED}⚠  API 调用失败：{e}{RESET}")
                cmd = ""
                break

            kind = classify_output(cmd)

            if kind == "clarify":
                question = cmd[len(CLARIFY_PREFIX):].strip()
                print(f"\n{YELLOW}❓ {question}{RESET}")
                try:
                    ans = input("你的回答（直接回车取消）> ").strip()
                except (EOFError, KeyboardInterrupt):
                    ans = ""
                if not ans or ans.lower() in ("exit", "退出"):
                    print("已取消。")
                    cmd = ""
                    break
                followups.append((cmd, ans))
                print("生成中...", end="\r")
                continue  # 带上回答再生成

            break  # command 或 cannot，退出澄清循环
        else:
            # 超出澄清轮数仍未明确
            print(f"{YELLOW}⚠  无法明确你的意图，请换种说法重新描述。{RESET}")
            continue

        if not cmd:
            continue

        if classify_output(cmd) == "cannot":
            print(f"{YELLOW}⚠  {cmd}{RESET}")
            continue

        # 安全检查
        risk, reason = check(cmd)

        if risk == HIGH:
            print(f"\n{RED}{BOLD}【高危命令】{RESET}")
            print(f"  命令：{RED}{cmd}{RESET}")
            if explanation:
                print(f"  说明：{explanation}")
            print(f"  风险：{RED}{reason}{RESET}")
            confirm = input(f"\n{RED}确认执行请输入 yes（其他任意键取消）> {RESET}").strip()
            if confirm != "yes":
                print("已取消。")
                continue

        elif risk == WARN:
            print(f"\n{YELLOW}【注意】{reason}{RESET}")
            print(f"  命令：{YELLOW}{cmd}{RESET}")
            if explanation:
                print(f"  说明：{explanation}")
            confirm = input("执行？(y/n) > ").strip().lower()
            if confirm != "y":
                print("已取消。")
                continue

        else:
            print(f"  命令：{GREEN}{cmd}{RESET}")
            if explanation:
                print(f"  说明：{explanation}")
            confirm = input("执行？(y/n) > ").strip().lower()
            if confirm != "y":
                print("已跳过。")
                continue

        run(cmd)


if __name__ == "__main__":
    main()
