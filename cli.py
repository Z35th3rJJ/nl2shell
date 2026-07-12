import argparse
import json
import os
from pathlib import Path
import shlex
from uuid import uuid4

from dotenv import load_dotenv

from core.decision import AUTO_ALLOW, BLOCK, STRONG_CONFIRM, decide
from core.engine import Engine
from core.execution import BashExecutor, bash_unavailable_message, try_change_directory
from core.history import HistoryStore
from core.impact import analyze
from core.input_session import create_input_session
from core.preflight import CommandEdit, apply_edits, default_target_for_step, inspect_plan
from core.safety import HIGH, SAFE, WARN, check
from core.settings import (
    AUTO_SAFE, ENV_PATH, PREVIEW, AppSettings,
    choose_mode, first_run_setup, load_settings, mode_description, mode_name,
    save_settings,
)
from core.ssh_config import load_ssh_profiles
from core.verification import verify

load_dotenv(ENV_PATH)

RED, YELLOW, GREEN, BOLD, RESET = "\033[91m", "\033[93m", "\033[92m", "\033[1m", "\033[0m"
_RISK_ORDER = {SAFE: 0, WARN: 1, HIGH: 2}
BATCH = "batch"


def run(command: str, executor: BashExecutor, *, cwd: str | None = None,
        timeout_seconds: float = 60, persist_cwd: bool = True):
    cd_result = try_change_directory(command) if persist_cwd else None
    return cd_result if cd_result is not None else executor.execute(
        command, timeout_seconds=timeout_seconds, cwd=cwd,
    )


def print_history(store: HistoryStore, limit: int = 20, *, status: str | None = None,
                  batch_id: str | None = None, since: str | None = None) -> None:
    records = store.query(limit, status=status, batch_id=batch_id, since=since)
    if not records:
        print("暂无历史记录。")
        return
    for record in records:
        status = "已执行" if record.get("executed") else f"未执行（{record.get('status', 'unknown')}）"
        print(f"[{record['timestamp']}] {status} | ID: {record.get('record_id', '旧记录')}\n"
              f"  输入：{record.get('input', '')}\n  命令：{record.get('command', '')}\n"
              f"  方式：{record.get('run_mode', '旧记录')}")
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
        "  /history [数量] [--status 状态] [--batch 批次] [--since ISO时间]\n"
        "  /history export <jsonl|csv> <路径> [筛选条件]\n"
        "  /history replay <记录ID> | replay-batch <批次ID>\n"
        "  /ssh              查看 OpenSSH 主机配置\n"
        "  /ssh test <别名>  检查 SSH 连通性与认证\n"
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
    if mode == BATCH:
        return True
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
                    user_input: str, cwd: str, mode: str, input_fn=input,
                    timeout_seconds: float = 60, batch_id: str = "",
                    batch_index: int | None = None) -> str:
    batch_details = {"batch_id": batch_id, "batch_index": batch_index} if batch_id else {}
    try:
        plan = engine.generate_task_plan(user_input, cwd)
    except Exception as error:
        print(f"{RED}任务计划生成失败：{error}{RESET}")
        save_history(history, user_input=user_input, cwd=cwd, command="", risk=SAFE,
                     status="plan_failed", executed=False, run_mode=mode, **batch_details)
        return "plan_failed"

    plan, preflight, preflight_error = _preflight_plan(engine, plan, user_input, cwd, input_fn)
    if preflight_error:
        command = " && ".join(step.command for step in plan.steps)
        print(f"{RED}执行前检查未通过：{preflight_error}{RESET}")
        save_history(history, user_input=user_input, cwd=cwd, command=command, risk=SAFE,
                     status="preflight_failed", executed=False, run_mode=mode,
                     preflight=preflight, **batch_details)
        return "preflight_failed"

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
        **batch_details,
    }
    blocked = next((item for item in assessments if item[4].level == BLOCK), None)
    if blocked:
        print(f"{RED}计划已阻止：{blocked[4].reason}{RESET}")
        save_history(history, user_input=user_input, cwd=cwd, command=joined, risk=max_risk,
                     status="blocked", executed=False, **details)
        return "blocked"
    if mode == PREVIEW:
        print(f"{YELLOW}预览模式：未调用 Bash。{RESET}")
        save_history(history, user_input=user_input, cwd=cwd, command=joined, risk=max_risk,
                     status="preview", executed=False, **details)
        return "preview"
    if not executor.is_available():
        print(f"{RED}错误：{bash_unavailable_message()}{RESET}")
        save_history(history, user_input=user_input, cwd=cwd, command=joined, risk=max_risk,
                     status="bash_unavailable", executed=False, **details)
        return "bash_unavailable"
    if not _confirm_plan(mode, assessments):
        print("已取消。")
        save_history(history, user_input=user_input, cwd=cwd, command=joined, risk=max_risk,
                     status="cancelled", executed=False, **details)
        return "cancelled"

    outcomes, fix_suggestion = [], ""
    for step, _, _, _, _ in assessments:
        try:
            result = run(step.command, executor, cwd=cwd, timeout_seconds=timeout_seconds,
                         persist_cwd=mode != BATCH)
            if result.stdout:
                print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
            if result.stderr:
                print(f"{RED}{result.stderr}{RESET}", end="" if result.stderr.endswith("\n") else "\n")
            verification = verify(executor, result, step.verification, cwd=cwd)
            print(f"  验证结果：{verification.status} - {verification.detail}")
            outcomes.append({"command": step.command, "status": verification.status,
                             "detail": verification.detail, "timed_out": result.timed_out,
                             "stdout": result.stdout[-2000:], "stderr": result.stderr[-2000:]})
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
                 fix_suggestion=fix_suggestion[:500], timed_out=any(item.get("timed_out") for item in outcomes),
                 **details)
    engine.remember(user_input, joined)
    return final_status


def _batch_tasks(path: Path, default_cwd: str) -> list[dict]:
    tasks = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            task = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"第 {line_number} 行不是合法 JSON：{error.msg}") from error
        if not isinstance(task, dict) or not isinstance(task.get("input"), str) or not task["input"].strip():
            raise ValueError(f"第 {line_number} 行必须包含非空字符串 input")
        cwd = task.get("cwd", default_cwd)
        if not isinstance(cwd, str) or not Path(cwd).is_dir():
            raise ValueError(f"第 {line_number} 行的 cwd 不是有效目录")
        tasks.append({"input": task["input"].strip(), "cwd": str(Path(cwd).resolve())})
    return tasks


def run_batch(engine: Engine, executor: BashExecutor, history: HistoryStore, task_path: Path,
              timeout_seconds: float = 60) -> dict:
    tasks = _batch_tasks(task_path, os.getcwd())
    batch_id = uuid4().hex[:12]
    summary = {"batch_id": batch_id, "source": str(task_path), "total": len(tasks),
               "success": 0, "failed": 0, "blocked": 0, "timed_out": 0, "results": []}
    print(f"批量任务 {batch_id}：共 {len(tasks)} 条，失败后继续执行。")
    try:
        for index, task in enumerate(tasks, 1):
            print(f"\n[{index}/{len(tasks)}] {task['input']}")
            status = execute_request(engine, executor, history, task["input"], task["cwd"], BATCH,
                                     input_fn=lambda _: "", timeout_seconds=timeout_seconds,
                                     batch_id=batch_id, batch_index=index)
            summary["results"].append({"index": index, "input": task["input"], "status": status})
            if status in {"verified", "exit_code_only"}:
                summary["success"] += 1
            elif status == "blocked":
                summary["blocked"] += 1
            elif status == "command_failed" and history.query(1, batch_id=batch_id)[0].get("timed_out"):
                summary["timed_out"] += 1
                summary["failed"] += 1
            else:
                summary["failed"] += 1
    except KeyboardInterrupt:
        summary["interrupted"] = True
        print("\n批量任务已中断，已完成结果已保留。")
    summary_path = history.path.parent / f"batch_{batch_id}.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    history.append({"input": "批量任务摘要", "cwd": os.getcwd(), "command": "", "risk": SAFE,
                    "status": "batch_summary", "executed": False, "run_mode": BATCH,
                    "batch_id": batch_id, "source": str(task_path), "summary": summary})
    print(f"\n汇总：成功 {summary['success']}，失败 {summary['failed']}，阻止 {summary['blocked']}，超时 {summary['timed_out']}。")
    print(f"结果文件：{summary_path}")
    return summary


def _history_options(arguments: list[str]) -> tuple[int, dict]:
    limit, filters, index = 20, {}, 0
    while index < len(arguments):
        item = arguments[index]
        if item.isdigit():
            limit = int(item)
            index += 1
        elif item in {"--status", "--batch", "--since"} and index + 1 < len(arguments):
            filters[{"--status": "status", "--batch": "batch_id", "--since": "since"}[item]] = arguments[index + 1]
            index += 2
        else:
            raise ValueError("历史参数无效")
    if limit <= 0:
        raise ValueError("数量必须为正整数")
    return limit, filters


def handle_history_command(command: str, store: HistoryStore, engine: Engine, executor: BashExecutor,
                           mode: str) -> None:
    arguments = shlex.split(command)[1:]
    if not arguments:
        print_history(store)
        return
    if arguments[0] == "export" and len(arguments) >= 3:
        fmt, destination = arguments[1], Path(arguments[2])
        _, filters = _history_options(arguments[3:])
        records = store.query(limit=None, **filters)
        store.export(records, fmt, destination)
        print(f"已导出 {len(records)} 条记录到：{destination}")
        return
    if arguments[0] == "replay" and len(arguments) == 2:
        record = store.find(arguments[1])
        if not record or not record.get("input"):
            print("未找到可重放的历史记录。")
            return
        execute_request(engine, executor, store, record["input"], os.getcwd(), mode)
        return
    if arguments[0] == "replay-batch" and len(arguments) == 2:
        records = store.query(limit=None, batch_id=arguments[1])
        records = sorted((record for record in records if record.get("input") != "批量任务摘要"),
                         key=lambda record: record.get("batch_index", 0))
        for record in records:
            execute_request(engine, executor, store, record["input"], os.getcwd(), mode)
        return
    limit, filters = _history_options(arguments)
    print_history(store, limit, **filters)


def print_ssh_profiles() -> None:
    profiles = load_ssh_profiles()
    if not profiles:
        print("未找到 OpenSSH Host 配置。")
        return
    for profile in profiles:
        target = profile.hostname or "（使用别名默认解析）"
        user = f"{profile.user}@" if profile.user else ""
        port = f":{profile.port}" if profile.port else ""
        key = "已配置" if profile.identity_file else "未配置"
        print(f"{profile.alias}: {user}{target}{port} | 私钥：{key}")


def test_ssh_profile(alias: str, executor: BashExecutor) -> None:
    if alias not in {profile.alias for profile in load_ssh_profiles()}:
        print(f"未知 SSH 别名：{alias}")
        return
    result = executor.execute(f"ssh -o BatchMode=yes -o ConnectTimeout=10 {shlex.quote(alias)} exit",
                              timeout_seconds=15)
    if result.exit_code == 0:
        print(f"SSH {alias} 连通且认证成功。")
    elif result.timed_out:
        print(f"SSH {alias} 连接超时。")
    else:
        print(f"SSH {alias} 检查失败：{result.stderr.strip() or '未知错误'}")


def main(input_session=None, args=None) -> None:
    args = args or argparse.Namespace(batch=None, timeout=60.0)
    try:
        settings = load_settings()
    except ValueError as error:
        print(f"{RED}配置错误：{error}{RESET}")
        return
    executor, engine, history = BashExecutor(), Engine(), HistoryStore()
    if args.batch:
        try:
            run_batch(engine, executor, history, Path(args.batch), args.timeout)
        except (OSError, ValueError) as error:
            print(f"{RED}批量任务失败：{error}{RESET}")
        return

    settings = first_run_setup(settings)
    if settings is None:
        print("已退出。")
        return

    mode = settings.run_mode
    input_session = input_session or create_input_session()
    print(f"{BOLD}智能 Shell 助手{RESET} | {mode_name(mode)} | 输入 /help 查看命令")
    while True:
        try:
            cwd = os.getcwd()
            user_input = input_session.prompt(f"\n[{cwd}]\n你想做什么？> ").strip()
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
                handle_history_command(user_input, history, engine, executor, mode)
            except ValueError:
                print("/history 参数无效。")
            continue
        if user_input == "/ssh":
            print_ssh_profiles()
            continue
        if user_input.startswith("/ssh test "):
            alias = user_input.split(maxsplit=2)[2].strip()
            if alias:
                test_ssh_profile(alias, executor)
            else:
                print("请输入 SSH 别名。")
            continue
        execute_request(engine, executor, history, user_input, cwd, mode)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="安全可控的自然语言 Shell 助手")
    parser.add_argument("--batch", help="JSONL 批量任务文件；失败后继续，HIGH 命令永久阻止")
    parser.add_argument("--timeout", type=float, default=60, help="批量主命令超时秒数（默认 60）")
    parsed_args = parser.parse_args()
    if parsed_args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    main(args=parsed_args)
