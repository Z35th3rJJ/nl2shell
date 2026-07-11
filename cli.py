import argparse
import os

from dotenv import load_dotenv

from core.automation import should_auto_execute
from core.engine import Engine, classify_output, CLARIFY_PREFIX
from core.execution import BashExecutor, bash_unavailable_message, try_change_directory
from core.history import HistoryStore
from core.impact import analyze
from core.policy import evaluate
from core.safety import check, HIGH, WARN, SAFE
from core.settings import ENV_PATH, choose_runtime_settings, load_settings
from core.verification import verify

load_dotenv(ENV_PATH)

RED, YELLOW, GREEN, BOLD, RESET = "\033[91m", "\033[93m", "\033[92m", "\033[1m", "\033[0m"


def run(command: str, executor: BashExecutor):
    cd_result = try_change_directory(command)
    return cd_result if cd_result is not None else executor.execute(command)


def print_history(store: HistoryStore, limit: int) -> None:
    records = store.recent(limit)
    if not records:
        print("暂无历史记录。")
        return
    for record in records:
        status = "已执行" if record.get("executed") else f"未执行（{record.get('status', 'unknown')}）"
        verification = record.get("verification")
        print(f"[{record['timestamp']}] {status}\n  输入：{record['input']}\n  命令：{record['command']}\n  目录：{record['cwd']}")
        if verification:
            print(f"  验证：{verification}")


def save_history(store: HistoryStore, *, user_input: str, cwd: str, command: str,
                 risk: str, status: str, executed: bool, result=None, **details) -> None:
    store.append({
        "input": user_input, "cwd": cwd, "command": command, "risk": risk,
        "status": status, "executed": executed,
        "exit_code": result.exit_code if result else None,
        "duration_seconds": result.duration_seconds if result else None,
        "stderr": result.stderr.strip()[:500] if result else "", **details,
    })


def show_assessment(command: str, policy: str, cwd: str):
    impact = analyze(command)
    decision = evaluate(impact, policy, cwd)
    print(f"  影响：{impact.summary}（标签：{', '.join(impact.tags)}）")
    print(f"  策略：{policy} - {decision.reason}")
    return impact, decision


def confirm_command(command: str, risk: str, reason: str, auto: bool, requires_confirmation: bool) -> bool:
    if auto:
        return should_auto_execute(True, risk) and not requires_confirmation
    if risk == HIGH:
        print(f"{RED}{BOLD}【高危命令】{RESET}\n  风险：{reason}")
        return input("确认执行请输入 yes（其他任意键取消）> ").strip() == "yes"
    if risk == WARN or requires_confirmation:
        print(f"{YELLOW}【注意】{reason or '策略要求人工确认'}{RESET}")
    return input("执行？(y/n) > ").strip().lower() == "y"


def execute_agent(engine: Engine, executor: BashExecutor | None, history: HistoryStore, *, user_input: str,
                  cwd: str, policy: str, auto: bool, dry_run: bool) -> None:
    try:
        plan = engine.generate_task_plan(user_input, cwd)
    except Exception as error:
        print(f"{RED}任务计划生成失败：{error}{RESET}")
        return

    assessments = []
    print(f"\n{BOLD}任务计划（{len(plan.steps)} 步）{RESET}")
    for index, step in enumerate(plan.steps, 1):
        risk, reason = check(step.command)
        print(f"{index}. {GREEN}{step.command}{RESET}\n   说明：{step.explanation}\n   预期：{step.expected or '按退出码判断'}\n   验证：{step.verification or '仅检查退出码'}")
        impact, decision = show_assessment(step.command, policy, cwd)
        assessments.append((step, risk, reason, impact, decision))

    blocked = [item for item in assessments if not item[4].allowed]
    history_details = {"policy": policy, "dry_run": dry_run, "impact_tags": [item[3].tags for item in assessments], "steps": len(assessments)}
    joined = " && ".join(item[0].command for item in assessments)
    if blocked:
        print(f"{RED}计划已阻止：{blocked[0][4].reason}{RESET}")
        save_history(history, user_input=user_input, cwd=cwd, command=joined, risk=blocked[0][1], status="policy_blocked", executed=False, **history_details)
        return
    if dry_run:
        print(f"{YELLOW}Dry-run：已完成影响与验证方案分析，未调用 Bash。{RESET}")
        save_history(history, user_input=user_input, cwd=cwd, command=joined, risk=max((item[1] for item in assessments), key={SAFE: 0, WARN: 1, HIGH: 2}.get), status="dry_run", executed=False, **history_details)
        return
    if executor is None:
        print(f"{RED}错误：{bash_unavailable_message()}{RESET}")
        return
    if auto:
        allowed_auto = all(risk == SAFE and not decision.requires_confirmation for _, risk, _, _, decision in assessments)
        confirmed = allowed_auto
        if not confirmed:
            print(f"{YELLOW}自动模式跳过：计划包含非 SAFE 或需人工确认的步骤。{RESET}")
    else:
        has_high = any(risk == HIGH for _, risk, _, _, _ in assessments)
        prompt = "计划含高危步骤，确认执行请输入 yes > " if has_high else "执行整个计划？(y/n) > "
        confirmed = input(prompt).strip().lower() == ("yes" if has_high else "y")
    if not confirmed:
        save_history(history, user_input=user_input, cwd=cwd, command=joined, risk=SAFE, status="cancelled", executed=False, **history_details)
        return

    outcomes, fix_suggestion = [], ""
    for step, risk, _, _, _ in assessments:
        try:
            result = run(step.command, executor)
            verification = verify(executor, result, step.verification)
        except Exception as error:
            result, verification = None, None
            outcomes.append({"command": step.command, "status": "execution_error", "detail": str(error)})
            break
        outcomes.append({"command": step.command, "status": verification.status, "detail": verification.detail})
        if verification.status in {"command_failed", "verification_failed"}:
            try:
                fix_suggestion = engine.suggest_fix(step.command, verification.detail)
            except Exception as error:
                fix_suggestion = f"修复建议生成失败：{error}"
            print(f"{YELLOW}步骤验证失败：{verification.detail}\n修复建议（不会自动执行）：{fix_suggestion}{RESET}")
            break
    final_status = outcomes[-1]["status"] if outcomes else "execution_error"
    save_history(history, user_input=user_input, cwd=cwd, command=joined, risk=SAFE, status=final_status, executed=True,
                 verification=outcomes, fix_suggestion=fix_suggestion[:500], **history_details)
    engine.remember(user_input, joined)


def main():
    try:
        settings = load_settings()
    except ValueError as error:
        print(f"{RED}配置错误：{error}{RESET}")
        return
    selected = choose_runtime_settings(settings)
    if selected is None:
        print("已退出。")
        return
    auto = selected.auto_execute
    policy = selected.policy
    dry_run = selected.dry_run
    agent = selected.agent_mode
    executor = BashExecutor()
    if not dry_run and not executor.is_available():
        print(f"{RED}错误：{bash_unavailable_message()}{RESET}")
        return
    active_executor = executor if executor.is_available() else None
    engine, history = Engine(), HistoryStore()
    mode = "Agent" if agent else "单命令"
    print(f"{BOLD}智能 Shell 助手{RESET} | {mode} | 策略：{policy} | 输入 'exit' 退出")

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
        if user_input == "policy":
            print(f"当前策略：{policy}")
            continue
        if user_input == "history" or user_input.startswith("history "):
            try:
                limit = int(user_input.split(maxsplit=1)[1]) if " " in user_input else 20
                print_history(history, limit)
            except ValueError:
                print(f"{YELLOW}history 后只能输入正整数。{RESET}")
            continue
        if agent:
            execute_agent(engine, active_executor, history, user_input=user_input, cwd=cwd, policy=policy, auto=auto, dry_run=dry_run)
            continue

        print("生成中...", end="\r")
        followups: list[tuple[str, str]] = []
        command = explanation = ""
        for _ in range(3):
            try:
                command, explanation = engine.generate(user_input, cwd, followups=followups)
            except Exception as error:
                print(f"{RED}API 调用失败：{error}{RESET}")
                break
            if classify_output(command) != "clarify":
                break
            print(f"\n{YELLOW}{command[len(CLARIFY_PREFIX):].strip()}{RESET}")
            answer = input("你的回答（直接回车取消）> ").strip()
            if not answer:
                command = ""
                break
            followups.append((command, answer))
        if not command:
            continue
        if classify_output(command) == "cannot":
            print(f"{YELLOW}{command}{RESET}")
            save_history(history, user_input=user_input, cwd=cwd, command=command, risk="", status="cannot_generate", executed=False, policy=policy, dry_run=dry_run)
            continue
        risk, reason = check(command)
        impact, decision = show_assessment(command, policy, cwd)
        if explanation:
            print(f"  说明：{explanation}")
        details = {"policy": policy, "dry_run": dry_run, "impact_tags": impact.tags}
        if not decision.allowed:
            print(f"{RED}已阻止：{decision.reason}{RESET}")
            save_history(history, user_input=user_input, cwd=cwd, command=command, risk=risk, status="policy_blocked", executed=False, **details)
            continue
        if dry_run:
            print(f"{YELLOW}Dry-run：已完成影响分析，未调用 Bash。{RESET}")
            save_history(history, user_input=user_input, cwd=cwd, command=command, risk=risk, status="dry_run", executed=False, **details)
            continue
        if not confirm_command(command, risk, reason, auto, decision.requires_confirmation):
            save_history(history, user_input=user_input, cwd=cwd, command=command, risk=risk, status="cancelled", executed=False, **details)
            continue
        try:
            result = run(command, active_executor)
        except Exception as error:
            print(f"{RED}执行失败：{error}{RESET}")
            save_history(history, user_input=user_input, cwd=cwd, command=command, risk=risk, status="execution_error", executed=True, **details)
            continue
        if result.stdout:
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
        if result.stderr:
            print(f"{RED}{result.stderr}{RESET}", end="" if result.stderr.endswith("\n") else "\n")
        save_history(history, user_input=user_input, cwd=cwd, command=command, risk=risk, status="executed", executed=True, result=result, **details)
        engine.remember(user_input, command)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="智能 Shell 助手：运行模式在启动菜单中配置")
    parser.parse_args()
    main()
