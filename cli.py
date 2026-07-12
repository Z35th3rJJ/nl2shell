import argparse
import os

from dotenv import load_dotenv

from core.decision import AUTO_ALLOW, BLOCK, STRONG_CONFIRM, decide
from core.engine import Engine
from core.execution import BashExecutor, bash_unavailable_message, try_change_directory
from core.history import HistoryStore
from core.impact import analyze
from core.preflight import CommandEdit, apply_edits, default_target_for_step, inspect_plan
from core.safety import HIGH, SAFE, WARN, check
from core.settings import (
    AUTO_SAFE, ENV_PATH, PREVIEW, AppSettings,
    choose_mode, first_run_setup, load_settings, mode_description, mode_name,
    save_settings,
)
from core.verification import verify

load_dotenv(ENV_PATH)

RED, YELLOW, GREEN, BOLD, RESET = "\033[91m", "\033[93m", "\033[92m", "\033[1m", "\033[0m"
_RISK_ORDER = {SAFE: 0, WARN: 1, HIGH: 2}


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
        print(f"[{record['timestamp']}] {status}\n  输入：{record['input']}\n  命令：{record['command']}\n  方式：{record.get('run_mode', '旧记录')}")
        if record.get("verification"):
            print(f"  验证：{record['verification']}")


def save_history(store: HistoryStore, *, user_input: str, cwd: str, command: str,
                 risk: str, status: str, executed: bool, **details) -> None:
    store.append({"input": user_input, "cwd": cwd, "command": command, "risk": risk,
                  "status": status, "executed": executed, **details})


def print_help() -> None:
    print(
        "内置命令：\n"
        "  /mode             查看或临时切换运行方式\n"
        "  /config           修改并保存默认运行方式\n"
        "  /status           查看模型、目录、运行方式和 Bash 状态\n"
        "  /history [数量]   查看历史记录\n"
        "  /help             显示帮助\n"
        "  /exit             退出"
    )


def print_status(mode: str, executor: BashExecutor) -> None:
    backend = os.environ.get("LLM_BACKEND", "deepseek").lower()
    model = os.environ.get("LOCAL_MODEL", "qwen2.5-coder:1.5b") if backend == "local" else os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
    print(
        f"运行状态：\n"
        f"  模型：{backend} / {model}\n"
        f"  工作目录：{os.getcwd()}\n"
        f"  运行方式：{mode_name(mode)} - {mode_description(mode)}\n"
        f"  Bash：{'可用' if executor.is_available() else '不可用'}"
    )


def _confirm_plan(mode: str, decisions) -> bool:
    levels = {item[4].level for item in decisions}
    if mode == AUTO_SAFE and levels == {AUTO_ALLOW}:
        print(f"{GREEN}安全自动：计划全部满足自动执行条件。{RESET}")
        return True
    if STRONG_CONFIRM in levels:
        return input(f"{RED}计划包含 sudo、系统级或未知操作，确认执行请输入 yes > {RESET}").strip() == "yes"
    return input("执行整个计划？(y/n) > ").strip().lower() == "y"


def _preflight_details(original, corrected, status, confirmed=None, target="", used_default=False):
    return {
        "status": status,
        "original_commands": [step.command for step in original.steps],
        "corrected_commands": [step.command for step in corrected.steps],
        "confirmed_candidates": confirmed or [],
        "selected_target": target,
        "used_default_target": used_default,
    }


def _choose_candidate(issue, input_fn):
    if not issue.candidates:
        return "", issue.message
    if len(issue.candidates) == 1:
        candidate = issue.candidates[0]
        answer = input_fn(f"未找到“{issue.path}”，是否指“{candidate}”？(Y/n) > ").strip().lower()
        return (candidate, "") if answer in {"", "y", "yes"} else ("", "用户未确认候选文件")
    print(f"未找到“{issue.path}”，可能是：")
    for index, item in enumerate(issue.candidates, 1):
        print(f"  {index}. {item}")
    answer = input_fn("请选择候选编号（其他输入取消）> ").strip()
    if not answer.isdigit() or not 1 <= int(answer) <= len(issue.candidates):
        return "", "用户未选择候选文件"
    return issue.candidates[int(answer) - 1], ""


def _correct_file_plan(plan, cwd: str, input_fn=input):
    original = plan
    confirmed, selected_target, used_default = [], "", False
    report = inspect_plan(plan, cwd)
    source_edits = []
    for issue in report.issues:
        if issue.kind != "missing_source":
            continue
        if issue.program not in {"cp", "mv"}:
            return plan, _preflight_details(original, plan, "failed"), issue.message
        candidate, failure = _choose_candidate(issue, input_fn)
        if failure:
            return plan, _preflight_details(original, plan, "failed", confirmed), failure
        confirmed.append({"requested": issue.path, "selected": candidate})
        source_edits.append(CommandEdit(issue.step, issue.argument_index, candidate))
    if source_edits:
        plan = apply_edits(plan, source_edits)

    report = inspect_plan(plan, cwd)
    remaining_source = next((issue for issue in report.issues if issue.kind == "missing_source"), None)
    if remaining_source:
        return plan, _preflight_details(original, plan, "failed", confirmed), remaining_source.message

    target_edits = []
    handled_steps = set()
    for issue in report.issues:
        if issue.kind not in {"missing_target", "same_source_target"} or issue.step in handled_steps:
            continue
        handled_steps.add(issue.step)
        default_target = default_target_for_step(plan, issue.step, cwd)
        if default_target:
            target = input_fn(f"请输入目标路径/新文件名（回车使用 {default_target}）> ").strip()
            if not target:
                target, used_default = default_target, True
        else:
            target = input_fn("请输入目标路径/新文件名> ").strip()
        if not target:
            return plan, _preflight_details(original, plan, "failed", confirmed), "未提供有效目标路径"
        selected_target = target
        target_edits.append(CommandEdit(issue.step, issue.argument_index, target))
    if target_edits:
        plan = apply_edits(plan, target_edits)

    final_report = inspect_plan(plan, cwd)
    if not final_report.ok:
        return plan, _preflight_details(original, plan, "failed", confirmed, selected_target, used_default), final_report.issues[0].message
    return plan, _preflight_details(original, plan, "passed", confirmed, selected_target, used_default), ""


def _preflight_plan(engine: Engine, plan, user_input: str, cwd: str, input_fn=input):
    if plan.clarification:
        answer = input_fn(f"需要确认：{plan.clarification}\n你的回答> ").strip()
        if not answer:
            return plan, _preflight_details(plan, plan, "failed"), "未提供计划所需的补充信息"
        try:
            plan = engine.generate_task_plan(
                user_input, cwd,
                clarifications=[f"{plan.clarification} 用户回答：{answer}"],
            )
        except Exception as error:
            return plan, _preflight_details(plan, plan, "failed"), f"澄清后重新生成计划失败：{error}"
        if plan.clarification:
            return plan, _preflight_details(plan, plan, "failed"), f"澄清后计划仍不完整：{plan.clarification}"
    return _correct_file_plan(plan, cwd, input_fn)


def execute_request(engine: Engine, executor: BashExecutor, history: HistoryStore,
                    user_input: str, cwd: str, mode: str, input_fn=input) -> None:
    try:
        plan = engine.generate_task_plan(user_input, cwd)
    except Exception as error:
        print(f"{RED}任务计划生成失败：{error}{RESET}")
        return

    plan, preflight, preflight_error = _preflight_plan(engine, plan, user_input, cwd, input_fn)
    if preflight_error:
        command = " && ".join(step.command for step in plan.steps)
        print(f"{RED}执行前检查未通过：{preflight_error}{RESET}")
        save_history(history, user_input=user_input, cwd=cwd, command=command, risk=SAFE,
                     status="preflight_failed", executed=False, run_mode=mode,
                     preflight=preflight)
        return

    assessments = []
    print(f"\n{BOLD}任务计划（{len(plan.steps)} 步）{RESET}")
    for index, step in enumerate(plan.steps, 1):
        risk, risk_reason = check(step.command)
        impact = analyze(step.command)
        decision = decide(impact, risk, cwd)
        assessments.append((step, risk, risk_reason, impact, decision))
        print(
            f"{index}. {GREEN}{step.command}{RESET}\n"
            f"   说明：{step.explanation}\n"
            f"   预期：{step.expected or '按退出码判断'}\n"
            f"   验证：{step.verification or '仅检查退出码'}\n"
            f"   影响：{impact.summary}（{', '.join(impact.tags)}）\n"
            f"   决策：{decision.level} - {decision.reason}"
        )

    joined = " && ".join(item[0].command for item in assessments)
    max_risk = max((item[1] for item in assessments), key=_RISK_ORDER.get)
    details = {
        "run_mode": mode,
        "decisions": [item[4].level for item in assessments],
        "impact_tags": [item[3].tags for item in assessments],
        "steps": len(assessments),
        "preflight": preflight,
    }
    blocked = next((item for item in assessments if item[4].level == BLOCK), None)
    if blocked:
        print(f"{RED}计划已阻止：{blocked[4].reason}{RESET}")
        save_history(history, user_input=user_input, cwd=cwd, command=joined, risk=max_risk,
                     status="blocked", executed=False, **details)
        return
    if mode == PREVIEW:
        print(f"{YELLOW}预览模式：未调用 Bash。{RESET}")
        save_history(history, user_input=user_input, cwd=cwd, command=joined, risk=max_risk,
                     status="preview", executed=False, **details)
        return
    if not executor.is_available():
        print(f"{RED}错误：{bash_unavailable_message()}{RESET}")
        save_history(history, user_input=user_input, cwd=cwd, command=joined, risk=max_risk,
                     status="bash_unavailable", executed=False, **details)
        return
    if not _confirm_plan(mode, assessments):
        print("已取消。")
        save_history(history, user_input=user_input, cwd=cwd, command=joined, risk=max_risk,
                     status="cancelled", executed=False, **details)
        return

    outcomes, fix_suggestion = [], ""
    for step, _, _, _, _ in assessments:
        try:
            result = run(step.command, executor)
            if result.stdout:
                print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
            if result.stderr:
                print(f"{RED}{result.stderr}{RESET}", end="" if result.stderr.endswith("\n") else "\n")
            verification = verify(executor, result, step.verification)
            print(f"  验证结果：{verification.status} - {verification.detail}")
            outcomes.append({"command": step.command, "status": verification.status, "detail": verification.detail})
        except Exception as error:
            outcomes.append({"command": step.command, "status": "execution_error", "detail": str(error)})
            print(f"{RED}执行失败：{error}{RESET}")
            break
        if verification.status in {"command_failed", "verification_failed", "invalid_verifier"}:
            try:
                fix_suggestion = engine.suggest_fix(step.command, verification.detail)
            except Exception as error:
                fix_suggestion = f"修复建议生成失败：{error}"
            print(f"{YELLOW}修复建议（不会自动执行）：{fix_suggestion}{RESET}")
            break
    final_status = outcomes[-1]["status"] if outcomes else "execution_error"
    save_history(history, user_input=user_input, cwd=cwd, command=joined, risk=max_risk,
                 status=final_status, executed=True, verification=outcomes,
                 fix_suggestion=fix_suggestion[:500], **details)
    engine.remember(user_input, joined)


def main() -> None:
    try:
        settings = load_settings()
    except ValueError as error:
        print(f"{RED}配置错误：{error}{RESET}")
        return
    settings = first_run_setup(settings)
    if settings is None:
        print("已退出。")
        return

    mode = settings.run_mode
    executor, engine, history = BashExecutor(), Engine(), HistoryStore()
    print(f"{BOLD}智能 Shell 助手{RESET} | {mode_name(mode)} | 输入 /help 查看命令")
    while True:
        try:
            cwd = os.getcwd()
            user_input = input(f"\n[{cwd}]\n你想做什么？> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break
        if not user_input:
            continue
        if user_input in {"/exit", "exit", "quit", "退出"}:
            print("再见！")
            break
        if user_input == "/help":
            print_help()
            continue
        if user_input == "/status":
            print_status(mode, executor)
            continue
        if user_input == "/mode":
            selected = choose_mode(mode)
            if selected is not None:
                mode = selected
                print(f"本次运行已切换为：{mode_name(mode)}。")
            continue
        if user_input == "/config":
            selected = choose_mode(mode)
            if selected is not None:
                saved = save_settings(AppSettings(selected, True))
                if saved:
                    mode = selected
                    print(f"默认运行方式已保存并切换为：{mode_name(mode)}。")
                else:
                    print("未找到 .env，配置未保存。")
            continue
        if user_input == "/history" or user_input.startswith("/history "):
            try:
                limit = int(user_input.split(maxsplit=1)[1]) if " " in user_input else 20
                if limit <= 0:
                    raise ValueError
                print_history(history, limit)
            except ValueError:
                print("/history 后只能输入正整数。")
            continue
        execute_request(engine, executor, history, user_input, cwd, mode)


if __name__ == "__main__":
    argparse.ArgumentParser(description="安全可控的自然语言 Shell 助手").parse_args()
    main()
