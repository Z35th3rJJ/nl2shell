import argparse
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import time
import math
import sys
from uuid import uuid4

from dotenv import load_dotenv

from core.command_review import SAFE, WARN
from core.diagnostics import diagnose_environment
from core.engine import Engine, PlanPathError
from core.llm import model_configuration
from core.execution import (
    BashExecutor, DockerExecutor, create_executor,
)
from core.history import HistoryStore
from core.redaction import redact_value
from core.input_session import create_input_session
from core.settings import (
    AUTO_SAFE, ENV_PATH, PREVIEW, AppSettings,
    choose_mode, first_run_setup, load_settings, mode_description, mode_name,
    save_settings,
)
from core.ssh_config import load_ssh_profiles
from core.structured_log import log_event
from core.task_plan import TaskPlan, TaskStep, plan_payload
from core.operations import prepare_plan, preview_details, preparation_changed, execute_action, workspace_path, unique_target
from core.bash_queries import uses_bash, prepare_queries, review_commands, query_preview, execute_query
from core.error_analysis import assess_failure
from core.execution_evidence import summarize_execution, execution_description, change_description

load_dotenv(ENV_PATH)

RED, YELLOW, GREEN, CYAN, GRAY, BOLD, RESET = "\033[91m", "\033[93m", "\033[92m", "\033[96m", "\033[90m", "\033[1m", "\033[0m"
BATCH = "batch"


def _paint(text, *styles):
    if (not sys.stdout.isatty() or "NO_COLOR" in os.environ
            or os.environ.get("TERM") == "dumb"):
        return text
    if os.name == "nt" and not (os.environ.get("WT_SESSION") or os.environ.get("ANSICON")
                                or os.environ.get("ConEmuANSI") == "ON"
                                or os.environ.get("TERM", "").startswith(("xterm", "vt100", "ansi", "screen", "tmux"))):
        return text
    return "".join(styles) + text + RESET


def _status_color(status):
    if status in {"verified", "no_matches"}:
        return GREEN
    if status in {"unsupported", "blocked", "plan_failed", "execution_failed", "failed", "audit_failed"}:
        return RED
    return YELLOW


_TASK_SERIAL = 0


def _rule_width():
    return max(16, min(shutil.get_terminal_size(fallback=(72, 20)).columns, 92))


def _rule(character="─", color=GRAY):
    print(_paint(character * _rule_width(), color))


def _section(number, title):
    print()
    _rule()
    print(_paint(f"{number:02d} / {title}", BOLD, CYAN))


def _task_end(number, status):
    color = _status_color(status)
    _rule("━", color)
    print(_paint(f"任务 {number:02d} 结束 · {_STATUS_NAMES.get(status, status)}", BOLD, color))
    _rule("━", color)
    print("\n\n", end="")


def _remember_task(engine: Engine, mode: str, user_input: str, cwd: str,
                   plan: TaskPlan, status: str, evidence: dict) -> None:
    if mode != BATCH:
        engine.remember_task(user_input, cwd, plan, status, **evidence)


def print_history(store: HistoryStore, limit: int = 20, *, status: str | None = None,
                  batch_id: str | None = None, since: str | None = None) -> None:
    records = store.query(limit, status=status, batch_id=batch_id, since=since)
    if not records:
        print("暂无历史记录。")
        return
    for record in records:
        status = execution_description(record) + f"（{record.get('status', 'unknown')}）"
        print(f"[{record['timestamp']}] {status} | ID: {record.get('record_id', '旧记录')}\n"
              f"  输入：{record.get('input', '')}\n  命令：{record.get('command', '')}\n"
              f"  方式：{record.get('run_mode', '旧记录')}")
        if record.get("verification"):
            print(f"  验证：{record['verification']}")


def save_history(store: HistoryStore, *, user_input: str, cwd: str, command: str,
                 risk: str, status: str, executed: bool, **details) -> None:
    store.last_record = redact_value({"input": user_input, "cwd": cwd, "command": command, "risk": risk,
                                     "status": status, "executed": executed, **details})
    store.append(store.last_record)
    try:
        log_event("task_finished", status=status, risk=risk, executed=executed, cwd=cwd,
                  execution_state=details.get("execution_state", "unknown"),
                  file_change_state=details.get("file_change_state", "unknown"), phase=details.get("phase"))
    except OSError:
        print("运行日志不可写；任务审计记录已保存。", file=sys.stderr)


def json_result(record: dict, status: str) -> dict:
    verification = record.get("verification", [])
    return {
        "status": status,
        "risk_level": record.get("risk", SAFE),
        "steps": verification or record.get("preview") or [{"status": status, "command": record.get("command", ""),
                                     "detail": record.get("block_reason", ""),
                                     "rule": record.get("block_rule", "")}],
        "verification": verification,
        "duration_seconds": sum(item.get("duration_seconds", 0) for item in verification),
        "error": next((item.get("detail", "") for item in verification
                       if item.get("status") not in {"verified", "no_matches", "exit_code_only", "not_executed"}),
                      record.get("block_reason", "")),
        **({"agent_trace": record["agent_trace"], "tool_executions": record["tool_executions"],
            "execution_repair_attempts": record["execution_repair_attempts"],
            "completion_verified": record["completion_verified"]} if record.get("agent") else {}),
    }


def recover_working_directory(previous: str | None = None) -> tuple[str, str]:
    """返回可用 cwd；当前目录失效时回退到最近存在的父目录。"""
    try:
        return os.getcwd(), ""
    except FileNotFoundError:
        candidate = Path(previous).expanduser() if previous else Path.home()
        for path in (candidate, *candidate.parents):
            if path.is_dir():
                os.chdir(path)
                return str(path), f"当前工作目录已不存在，已切换到：{path}"
        home = Path.home()
        os.chdir(home)
        return str(home), f"当前工作目录已不存在，已切换到用户主目录：{home}"


def print_diagnostics(executor: BashExecutor, *, only_failures: bool = False) -> bool:
    diagnostics = diagnose_environment(executor)
    selected = [item for item in diagnostics if not item.ok] if only_failures else diagnostics
    if selected:
        print("环境诊断：")
        for item in selected:
            print(f"  {'✓' if item.ok else '✗'} {item.name}：{item.message}")
    return all(item.ok for item in diagnostics)


def print_help() -> None:
    print(
        "内置命令：\n"
        "  /mode             查看或临时切换运行方式\n"
        "  /config           修改并保存默认运行方式\n"
        "  /status           查看模型、目录、运行方式和 Bash 状态\n"
        "  /doctor           检查模型、Bash 和当前目录\n"
        "  /history [数量] [--status 状态] [--batch 批次] [--since ISO时间]\n"
        "  /history export <jsonl|csv> <路径> [筛选条件]\n"
        "  /history replay <记录ID> | replay-batch <批次ID>\n"
        "  /ssh              查看 OpenSSH 主机配置\n"
        "  /workspace <路径> 切换工作目录\n  /trash             查看回收区\n  /restore <编号>    恢复回收文件\n"
        "  /help             显示帮助\n"
        "  /exit             退出"
    )


def print_status(mode: str, executor: BashExecutor) -> None:
    configuration = model_configuration()
    backend, model = configuration["backend"], configuration["model"]
    print(
        f"运行状态：\n"
        f"  模型：{backend} / {model}\n"
        f"  工作目录：{os.getcwd()}\n"
        f"  运行方式：{mode_name(mode)} - {mode_description(mode)}\n"
        f"  文件操作：Python 固定操作\n"
        f"  系统查询后端：{'Docker 沙箱' if isinstance(executor, DockerExecutor) else '本机 Bash'}"
        f"（{'可用' if executor.is_available() else '不可用'}）"
    )


_OPERATION_NAMES = {
    "find_files": "查找文件", "count_files": "统计文件数量", "create_file": "创建空文件",
    "create_directory": "创建目录", "copy_files": "复制文件", "move_files": "移动文件",
    "rename": "重命名", "trash": "移入回收区", "restore": "恢复文件",
    "list_trash": "查看回收区", "organize_files": "按文件类型整理", "system_info": "查询系统信息",
    "working_directory": "显示当前目录", "list_directory": "列出目录内容",
    "search_text": "搜索文本", "text_stats": "统计文本", "read_text": "查看文本首尾",
}
_STATUS_NAMES = {"verified": "已验证成功", "preview": "仅预览", "needs_clarification": "需要补充信息",
                 "unsupported": "暂不支持", "blocked": "已停止", "cancelled": "已取消",
                 "plan_failed": "无法生成计划", "execution_failed": "执行失败", "unverified": "尚未验证",
                 "interrupted": "已中断", "audit_failed": "记录保存失败", "not_executed": "未执行"}
_STATUS_NAMES.update(failed="执行失败", pending="未执行", executing="执行中")
_STATUS_NAMES["no_matches"] = "没有匹配结果"
_STATUS_NAMES["accepted"] = "审查通过，尚未执行"


def show_preview(prepared, *, show_cwd=True, request=""):
    if show_cwd:
        print(_paint(f"工作目录：{prepared['cwd']}", GRAY))
    for action in prepared["actions"]:
        print("\n" + _paint(f"第 {action['step']} 步：{_OPERATION_NAMES[action['operation']]}", BOLD))
        if action["items"]:
            print(f"  说明：{_OPERATION_NAMES[action['operation']]}，实际操作路径如下方清单")
        elif action["explanation"] and action["explanation"] != request:
            print(f"  说明：{action['explanation']}")
        if action.get("candidate_argv"):
            print("\n  候选命令：")
            print(_paint(f"    {action['command']}", CYAN))
            if action["candidate_explanation"] != action["explanation"]:
                print(_paint(f"  命令解释：{action['candidate_explanation']}", GRAY))
            print(_paint("  执行方式：模型命令，受审查参数数组", GRAY))
            review = action["review"]
            text = "通过（尚未执行）" if review == "accepted" else "拒绝（未执行）" if review == "rejected" else review
            print(_paint(f"  审查：{text}", GREEN if review == "accepted" else RED if review == "rejected" else YELLOW))
        else:
            print(_paint("  执行方式：受保护的 Python 工具", GRAY))
        if "path" in action["parameters"]:
            scope = f"范围：{action['parameters']['path']}"
            if "recursive" in action["parameters"]:
                scope += "；" + ("包含子目录" if action["parameters"]["recursive"] else "当前层")
            print(_paint("  " + scope, GRAY))
        p = action["parameters"]
        if "pattern" in p:
            print(_paint(f"  匹配条件：{p['pattern']}", GRAY))
        if "modified_within_days" in p:
            print(_paint(f"  修改时间：最近 {p['modified_within_days']} 天", GRAY))
        if "size_gt_bytes" in p:
            print(_paint(f"  大小条件：大于 {p['size_gt_bytes']} 字节", GRAY))
        if action["operation"] == "read_text":
            print(_paint(f"  查看范围：{'前' if p['end'] == 'head' else '后'} {p['lines']} 行", GRAY))
        if action["operation"] == "text_stats":
            print(_paint("  统计内容：" + {"lines": "行数", "words": "单词数", "bytes": "字节数"}[p["metric"]], GRAY))
        if action["files"]:
            print(f"  找到 {len(action['files'])} 个文件：")
            for name in action["files"]:
                print(_paint(f"    {name}", GRAY))
        for item in action["items"]:
            print(_paint(f"    {item.get('source', '新建')} → {item['destination']}", GRAY))
        if action["operation"] == "system_info":
            print("  查询：" + {"disk": "磁盘空间", "memory": "内存使用", "system": "系统信息"}[action["parameters"]["query"]])
        if not action["items"] and action["operation"] not in {"system_info", "list_trash"}:
            print(_paint("  本步骤不修改文件。", GRAY))


def show_outcome(outcome):
    color = _status_color(outcome["status"])
    print("\n" + _paint(f"第 {outcome['step']} 步：{_STATUS_NAMES.get(outcome['status'], outcome['status'])}", BOLD, color))
    print(_paint(f"  {outcome['detail']}", GRAY))
    if outcome.get("actual_argv"):
        print("  实际命令：")
        print(_paint("    " + shlex.join(outcome["actual_argv"]), CYAN))
    if "count" in outcome:
        print(f"  文件数量：{outcome['count']}")
    if outcome.get("stdout"):
        print(_paint("  命令输出：", GRAY))
        if outcome["operation"] in {"find_files", "count_files"}:
            for name in outcome["stdout"].split("\0"):
                if name:
                    print(json.dumps(name, ensure_ascii=False) if "\n" in name or "\r" in name else name)
        else:
            print(outcome["stdout"].rstrip())
    if outcome.get("stderr"):
        print(_paint("  错误输出：", RED))
        print(outcome["stderr"].rstrip())
    for entry in outcome.get("entries", []):
        print(_paint(f"  {entry['id']} | {entry['original']} | {entry.get('timestamp', '')}", GRAY))
    if outcome["operation"] == "list_trash" and not outcome.get("entries"):
        print(_paint("  回收区为空。", GRAY))
    for item in outcome.get("items", []):
        print(_paint(f"  {item.get('source', '新建')} → {item['destination']}", GRAY))
        print(_paint(f"    {_STATUS_NAMES.get(item['status'], item['status'])}", _status_color(item["status"])))
        if "trash_id" in item:
            print(_paint(f"    恢复编号：{item['trash_id']}", GRAY))


def execute_request(engine: Engine, executor: BashExecutor, history: HistoryStore,
                    user_input: str, cwd: str, mode: str, input_fn=input,
                    timeout_seconds: float = 60, batch_id: str = "",
                    batch_index: int | None = None, assume_yes: bool = False,
                    agent_context: dict | None = None) -> str:
    plan = TaskPlan(())
    clarifications = []
    intent_attempts = []
    plan_attempts = []
    prepared, outcomes = None, []
    phase, authorized = "planning", False
    candidates, bash_query = None, False
    command_generation_started = False
    diagnoses, repairs, initial_command_attempts = [], [], []
    repair_used = bool(agent_context and agent_context["repairs"])
    audit_path = getattr(history, "path", None)
    excluded = [audit_path] if isinstance(audit_path, (str, os.PathLike)) else []
    if os.environ.get("NL2SHELL_LOG_JSON"):
        excluded.append(Path(os.environ["NL2SHELL_LOG_JSON"]).expanduser())
    global _TASK_SERIAL
    _TASK_SERIAL += 1
    task_number = _TASK_SERIAL
    result_section_printed = False

    def show_result_section():
        nonlocal result_section_printed
        if not result_section_printed:
            _section(3, "执行与验证")
            result_section_printed = True

    def ask(message):
        try:
            answer = input_fn(message).strip()
            if answer == "/exit":
                finish("cancelled", "用户退出，当前任务已取消")
                print("再见！")
                raise SystemExit(0)
            return answer
        except (EOFError, KeyboardInterrupt):
            return ""

    def generate_plan(**options):
        try:
            return engine.generate_task_plan(user_input, cwd, **options)
        finally:
            attempts = getattr(engine, "intent_attempts", None)
            if isinstance(attempts, list):
                intent_attempts.extend(redact_value(attempts))
            attempts = getattr(engine, "plan_attempts", None)
            if isinstance(attempts, list):
                plan_attempts.extend(redact_value(attempts))

    def remaining_time():
        return timeout_seconds if agent_context is None else max(0, agent_context["deadline"] - time.monotonic())

    def check_agent():
        if agent_context is not None:
            if remaining_time() <= 0:
                raise TimeoutError("Agent 总时限已用完")
            agent_context["check"](prepared)

    def run_action(value, action):
        nonlocal phase
        base = {"step": action["step"], "operation": action["operation"], "items": [],
                "executed": False, "execution_state": "not_started", "file_change_state": "unchanged"}
        try:
            check_agent()
            if agent_context is not None:
                if agent_context["executions"] >= 5:
                    raise ValueError("Agent 最多执行五次工具，已停止")
                agent_context["executions"] += 1
        except (ValueError, OSError, RuntimeError, KeyboardInterrupt) as error:
            phase = "environment"
            return {**base, "status": "interrupted" if isinstance(error, KeyboardInterrupt) else "blocked",
                    "failure_phase": phase, "detail": str(error)}
        phase = "execution"
        try:
            result = (execute_query if bash_query else execute_action)(value, action, executor,
                timeout_seconds=max(0, deadline - time.monotonic()))
        except (ValueError, OSError, RuntimeError, TypeError, KeyboardInterrupt) as error:
            # 已调用运行器但没有返回结果；没有启动证据时不能声称未执行。
            result = {**base, "status": "interrupted" if isinstance(error, KeyboardInterrupt) else "execution_failed",
                      "execution_state": "unknown", "failure_phase": phase, "detail": str(error),
                      "file_change_state": "unchanged" if value["read_only"] else "unknown",
                      "error_type": type(error).__name__, "error_number": getattr(error, "errno", None)}
        phase = result.get("failure_phase") or "verification"
        return result

    def finish(status, detail=""):
        stopped_step = next((row["step"] for row in outcomes if row["status"] not in {"verified", "no_matches", "not_executed"}), None)
        recorded_steps = {row["step"] for row in outcomes}
        for index, step in enumerate(plan.steps, 1):
            if index not in recorded_steps and status != "preview":
                outcomes.append({"step": index, "operation": step.operation, "status": "not_executed",
                    "detail": detail or "任务在执行前停止：" + status, "executed": False,
                    "execution_state": "not_started", "file_change_state": "unchanged",
                    **({"blocked_by_step": stopped_step} if stopped_step is not None else {"blocked_by_phase": phase})})
        evidence = summarize_execution(outcomes, repairs)
        payload = plan_payload(plan)
        try:
            save_history(history, user_input=user_input, cwd=cwd,
                         command=json.dumps(payload, ensure_ascii=False),
                         risk=SAFE if prepared and prepared["read_only"] else WARN,
                         status=status, **evidence, phase=phase, authorized=authorized, run_mode=mode, plan=payload, clarifications=clarifications,
                         preview=(query_preview(prepared) if bash_query else preview_details(prepared)) if prepared else [],
                         verification=outcomes, block_reason=detail,
                         timed_out=any(item.get("timed_out") for item in outcomes),
                         command_attempts=initial_command_attempts or (getattr(engine, "command_attempts", []) if command_generation_started and isinstance(getattr(engine, "command_attempts", None), list) else []),
                         candidates=candidates, intent_attempts=intent_attempts, plan_attempts=plan_attempts,
                         diagnoses=diagnoses, repairs=repairs, execution_repair_attempts=int(repair_used),
                         **({"agent_step": True} if agent_context is not None else {}),
                         **({"batch_id": batch_id, "batch_index": batch_index} if batch_id else {}))
        except OSError as error:
            status = "audit_failed"
            detail = f"历史记录保存失败；请保留本次输出：{error}"
            history.last_record.update(status=status, block_reason=detail)
        _remember_task(engine, mode, user_input, cwd, plan, status, evidence)
        show_result_section()
        print("\n" + _paint("最终验证", BOLD))
        print(_paint(f"任务结果：{_STATUS_NAMES.get(status, status)}", BOLD, _status_color(status)))
        print(_paint("执行状态：" + execution_description(evidence), GRAY))
        print(_paint("文件修改：" + change_description(evidence), GRAY))
        if detail:
            print(_paint(f"说明：{detail}", _status_color(status)))
        _task_end(task_number, status)
        return status

    def diagnose(outcome):
        diagnosis = assess_failure(outcome, prepared)
        if diagnosis is None:
            return None
        diagnosis["step"] = outcome.get("step")
        # 分类和执行事实由程序证据决定；模型建议不能重新宣称修改或成功。
        diagnosis["advice"] = "；".join(diagnosis["checks"])
        diagnosis["execution"] = summarize_execution([*outcomes, outcome], repairs)
        diagnoses.append(diagnosis)
        print(_paint(f"\n原因：{diagnosis['summary']}", RED))
        print(_paint(f"建议：{diagnosis['advice']}", GRAY))
        if diagnosis["execution"]["file_change_state"] != "unchanged":
            print(_paint(change_description(diagnosis["execution"]), YELLOW))
        return diagnosis

    print("\n\n")
    _rule("━", CYAN)
    print(_paint(f"任务 {task_number:02d}", BOLD, CYAN))
    print(_paint(f"任务：{user_input}", BOLD))
    print(_paint(f"工作目录：{cwd}", GRAY))
    _rule("━", CYAN)
    try:
        if user_input.startswith("/restore "):
            plan = TaskPlan((TaskStep(operation="restore", parameters={"trash_id": user_input.split(maxsplit=1)[1]}),))
        elif user_input == "/trash":
            plan = TaskPlan((TaskStep(operation="list_trash"),))
        else:
            plan = generate_plan()
        for _ in range(3):
            if not plan.clarification:
                break
            if mode in {BATCH, PREVIEW}:
                return finish("needs_clarification", plan.clarification)
            answer = ask(_paint(f"\n{plan.clarification}\n你的回答> ", YELLOW))
            if not answer:
                return finish("needs_clarification", plan.clarification)
            clarifications.append(f"问题：{plan.clarification}；用户回答：{answer}")
            plan = generate_plan(clarifications=clarifications)
        if plan.clarification:
            return finish("needs_clarification", plan.clarification)
    except (KeyboardInterrupt, EOFError):
        return finish("interrupted", "规划被中断，未进入执行阶段")
    except PlanPathError as error:
        return finish("blocked", "路径安全检查拒绝：" + str(error))
    except (ValueError, TypeError, RuntimeError, OSError) as error:
        return finish("plan_failed", str(error))
    if plan.refused:
        return finish("unsupported", "规划阶段拒绝（非路径检查结果）：" + (plan.reason or "暂不支持这项操作"))
    bash_query = uses_bash(plan)
    if not bash_query and any(step.operation in {"system_info", "working_directory", "list_directory",
                                                "search_text", "text_stats", "read_text"} for step in plan.steps):
        return finish("blocked", "首版请把命令查询和文件写入分开提交；未执行任何步骤")
    def prepare():
        options = {"timeout_seconds": remaining_time(), "excluded_paths": excluded}
        if prepared is not None:
            options["now"] = prepared["now"]
        value = (prepare_queries if bash_query else prepare_plan)(plan, cwd, **options)
        if bash_query and candidates is not None:
            review_commands(value, candidates)
        return value
    def preview():
        return query_preview(prepared) if bash_query else preview_details(prepared)
    phase = "preparation"
    try:
        check_agent()
        prepared = prepare()
        if bash_query:
            command_generation_started = True
            candidates = engine.generate_bash_commands(user_input, cwd, plan, prepared, clarifications)
            review_commands(prepared, candidates)
            if isinstance(getattr(engine, "command_attempts", None), list):
                initial_command_attempts = list(engine.command_attempts)
    except KeyboardInterrupt:
        return finish("interrupted", "准备被中断，未进入执行阶段")
    except (ValueError, TypeError, RuntimeError, OSError) as error:
        if isinstance(error, OSError):
            diagnose({"status": "execution_failed", "detail": str(error), "executed": False,
                      "execution_state": "not_started", "failure_phase": "preparation",
                      "error_type": type(error).__name__, "error_number": getattr(error, "errno", None),
                      "file_change_state": "unchanged"})
        return finish("blocked", str(error))
    phase = "preview"
    _section(1, "执行预览")
    show_preview(prepared, show_cwd=False, request=user_input)
    if mode == PREVIEW:
        return finish("preview")
    if isinstance(executor, DockerExecutor) and not prepared["read_only"]:
        return finish("blocked", "固定文件操作在本机执行；Docker 后端不执行本机写入")
    if mode == BATCH and not prepared["read_only"]:
        return finish("cancelled", "批处理只支持只读任务，文件修改需要人工确认")
    automatic = prepared["read_only"] and (mode == BATCH or assume_yes)
    phase = "confirmation"
    _section(2, "等待确认" if not automatic else "执行授权")
    if automatic:
        print(_paint("只读任务：按当前运行参数跳过人工确认。", GRAY))
    else:
        print(_paint("请核对上方操作和文件清单。只有输入 yes 才会执行。", YELLOW))
    for _ in range(3):
        if not automatic:
            if ask(_paint("\n是否执行以上操作？请输入 yes 确认 > ", YELLOW)).lower() != "yes":
                return finish("cancelled")
        try:
            check_agent()
            current = prepare()
        except (ValueError, TypeError, RuntimeError, OSError) as error:
            return finish("blocked", str(error))
        if not preparation_changed(prepared, current):
            prepared = current
            break
        if agent_context is not None:
            return finish("blocked", "Agent 确认期间文件变化，停止，不扩大范围")
        prepared = current
        automatic = False
        if mode == BATCH:
            return finish("blocked", "文件清单已变化，批处理不能重新确认")
        print(_paint("\n文件或目标名称已变化，请重新确认：", YELLOW))
        show_preview(prepared, show_cwd=False, request=user_input)
    else:
        return finish("blocked", "文件持续变化，请稍后重试")

    # 先记录授权。审计不可写时，不开始文件修改。
    authorized = True
    phase = "authorization"
    try:
        save_history(history, user_input=user_input, cwd=cwd, command="", risk=SAFE,
                     status="confirmed", **summarize_execution(outcomes, repairs), phase=phase, authorized=True, plan=plan_payload(plan),
                     preview=preview(), run_mode=mode, clarifications=clarifications,
                     intent_attempts=intent_attempts, plan_attempts=plan_attempts,
                     **({"agent_step": True} if agent_context is not None else {}))
    except OSError as error:
        history.last_record.update(status="audit_failed", block_reason=str(error))
        print(_paint(f"无法保存执行记录，操作已停止：{error}", RED))
        return "audit_failed"
    deadline = time.monotonic() + timeout_seconds if agent_context is None else agent_context["deadline"]
    def repair(action, failed, diagnosis):
        nonlocal repair_used, phase
        if repair_used and diagnosis is not None and diagnosis["repair_eligible"]:
            print(_paint("本任务的一次修复预算已用完，已停止。", YELLOW))
        if (repair_used or not bash_query or diagnosis is None or not diagnosis["repair_eligible"]
                or mode == BATCH or time.monotonic() >= deadline):
            return failed
        # 先消耗整项任务的唯一预算；候选被拒绝或用户取消也不能再次申请。
        repair_used = True
        if agent_context is not None:
            agent_context["repairs"] = 1
        attempt = {"attempt": 1, "step": action["step"], "initial_outcome": dict(failed),
                   "status": "not_executed", "executed": False}
        repairs.append(attempt)
        try:
            current = prepare_queries(plan, cwd, now=prepared["now"],
                                      timeout_seconds=max(0, deadline - time.monotonic()), excluded_paths=excluded)
            review_commands(current, candidates)
            if preparation_changed(prepared, current):
                raise ValueError("文件或查询范围变化，停止修复；请重新提交原始需求")
            payload = engine.generate_bash_commands(user_input, cwd, plan, current, clarifications,
                                                    repair_evidence=diagnosis)
            attempt["candidates"] = payload
            if isinstance(getattr(engine, "command_attempts", None), list):
                attempt["generation_attempts"] = list(engine.command_attempts)
            review_commands(current, payload)
            attempt["review"] = "accepted"
            print("\n" + _paint("唯一一次修复候选（只执行失败的当前步骤）：", BOLD))
            show_preview(current)
            phase = "confirmation"
            if ask(_paint("\n是否执行这一次只读修复？请输入 yes 确认 > ", YELLOW)).lower() != "yes":
                attempt["status"] = "cancelled"
                return failed
            checked = prepare_queries(plan, cwd, now=prepared["now"],
                                      timeout_seconds=max(0, deadline - time.monotonic()), excluded_paths=excluded)
            review_commands(checked, payload)
            if preparation_changed(current, checked):
                raise ValueError("修复确认后文件变化，已停止")
            save_history(history, user_input=user_input, cwd=cwd, command="", risk=SAFE,
                         status="repair_confirmed", **summarize_execution(outcomes, repairs),
                         phase="authorization", authorized=True, plan=plan_payload(plan),
                         preview=query_preview(checked), repair_attempt=1, run_mode=mode, clarifications=clarifications,
                         intent_attempts=intent_attempts, plan_attempts=plan_attempts,
                         **({"agent_step": True} if agent_context is not None else {}))
            repaired = run_action(checked, checked["actions"][action["step"] - 1])
            attempt.update(status=repaired["status"], executed=repaired["executed"], outcome=repaired)
            print("\n" + _paint("修复执行结果：", BOLD))
            show_outcome(repaired)
            if repaired["status"] not in {"verified", "no_matches"}:
                diagnose(repaired)
            return repaired
        except (ValueError, TypeError, RuntimeError, OSError) as error:
            attempt.update(status="blocked", review="rejected", reason=str(error))
            print(_paint(f"修复已停止：{error}", RED))
            return failed
    show_result_section()
    for action in prepared["actions"]:
        outcome = run_action(prepared, action)
        if not bash_query:
            outcome["execution_kind"] = "python_tool"
        outcomes.append(outcome)
        show_outcome(outcome)
        if outcome["status"] not in {"verified", "no_matches"}:
            diagnosis = diagnose(outcome)
            outcome = repair(action, outcome, diagnosis)
            outcomes[-1] = outcome
        if outcome["status"] not in {"verified", "no_matches"}:
            break
    for action in prepared["actions"][len(outcomes):]:
        outcomes.append({"step": action["step"], "operation": action["operation"],
                         "status": "not_executed", "detail": "前序步骤未成功", "executed": False,
                         "execution_state": "not_started", "file_change_state": "unchanged",
                         "blocked_by_step": outcomes[-1].get("blocked_by_step", outcomes[-1]["step"])})
        show_outcome(outcomes[-1])
    failed = next((item for item in outcomes if item["status"] not in {"verified", "no_matches", "not_executed"}), None)
    if failed:
        phase = failed.get("failure_phase") or phase
    final_status = failed["status"] if failed else "no_matches" if any(item["status"] == "no_matches" for item in outcomes) else "verified"
    return finish(final_status, failed["detail"] if failed else "")


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
               "success": 0, "no_matches": 0, "failed": 0, "blocked": 0, "timed_out": 0, "results": []}
    execution_records = []
    print(f"批量任务 {batch_id}：共 {len(tasks)} 条，失败后继续执行。")
    try:
        for index, task in enumerate(tasks, 1):
            print(f"\n[{index}/{len(tasks)}] {task['input']}")
            status = execute_request(engine, executor, history, task["input"], task["cwd"], BATCH,
                                     input_fn=lambda _: "", timeout_seconds=timeout_seconds,
                                     batch_id=batch_id, batch_index=index)
            execution_records.append({key: history.last_record[key] for key in
                ("status", "executed", "execution_state", "file_change_state")})
            summary["results"].append({"index": index, "input": task["input"], "status": status})
            if status == "verified":
                summary["success"] += 1
            elif status == "no_matches":
                summary["no_matches"] += 1
            elif status in {"blocked", "unsupported"}:
                summary["blocked"] += 1
            elif status == "execution_failed" and history.query(1, batch_id=batch_id)[0].get("timed_out"):
                summary["timed_out"] += 1
                summary["failed"] += 1
            else:
                summary["failed"] += 1
            if status == "interrupted":
                summary["interrupted"] = True
                break
    except KeyboardInterrupt:
        summary["interrupted"] = True
        print("\n批量任务已中断，已完成结果已保留。")
    summary_path = history.path.parent / f"batch_{batch_id}.json"
    summary_path.write_text(json.dumps(redact_value(summary), ensure_ascii=False, indent=2), encoding="utf-8")
    history.append({"input": "批量任务摘要", "cwd": os.getcwd(), "command": "", "risk": SAFE,
                    "status": "batch_summary", **summarize_execution(execution_records), "run_mode": BATCH,
                    "batch_id": batch_id, "source": str(task_path), "summary": summary})
    print(f"\n汇总：成功 {summary['success']}，无匹配 {summary['no_matches']}，失败 {summary['failed']}，阻止 {summary['blocked']}，超时 {summary['timed_out']}。")
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
        fmt = arguments[1]
        destination = unique_target(workspace_path(Path.cwd().resolve(), arguments[2]), set())
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
        if record.get("agent") or record.get("agent_step"):
            print("Agent 记录不能通过固定计划入口重放；请用 --agent 重新提交完整目标。")
            return
        execute_request(engine, executor, store, record["input"], os.getcwd(), mode)
        return
    if arguments[0] == "replay-batch" and len(arguments) == 2:
        records = store.query(limit=None, batch_id=arguments[1])
        records = sorted((record for record in records if record.get("input") != "批量任务摘要"),
                         key=lambda record: record.get("batch_index", 0))
        for record in records:
            if record.get("agent") or record.get("agent_step"):
                print("Agent 记录不能通过固定计划批量重放，已跳过。")
                continue
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


def main(input_session=None, args=None) -> None:
    args = args or argparse.Namespace(batch=None, timeout=60.0, task=None, preview=False, yes=False, json=False)
    try:
        settings = load_settings()
    except ValueError as error:
        print(_paint(f"配置错误：{error}", RED))
        return
    try:
        executor = create_executor()
    except ValueError as error:
        print(_paint(f"执行后端配置错误：{error}", RED))
        return
    engine, history = Engine(), HistoryStore()
    if args.batch:
        try:
            run_batch(engine, executor, history, Path(args.batch), args.timeout)
        except (OSError, ValueError) as error:
            print(_paint(f"批量任务失败：{error}", RED))
        return

    if args.task:
        cwd, warning = recover_working_directory()
        if warning and not args.json:
            print(_paint(warning, YELLOW))
        mode = PREVIEW if args.preview else settings.run_mode
        if getattr(args, "agent", False):
            from core.agent import execute_agent
            runner = execute_agent
        else:
            runner = execute_request
        if args.json:
            output = io.StringIO()
            with redirect_stdout(output):
                status = runner(engine, executor, history, args.task, cwd, mode,
                                         timeout_seconds=args.timeout, assume_yes=args.yes, input_fn=lambda _: "")
            record = history.last_record
            print(json.dumps(json_result(record, status), ensure_ascii=False))
        else:
            runner(engine, executor, history, args.task, cwd, mode,
                            timeout_seconds=args.timeout, assume_yes=args.yes)
        return

    settings = first_run_setup(settings)
    if settings is None:
        print("已退出。")
        return

    mode = settings.run_mode
    print_diagnostics(executor, only_failures=True)
    input_session = input_session or create_input_session()
    print("\n" + _paint("智能 Shell 助手", BOLD, CYAN))
    print(f"运行方式：{mode_name(mode)}")
    print(_paint("输入中文需求开始任务；输入 /help 查看命令，/exit 退出。", GRAY))
    last_cwd, _ = recover_working_directory()
    while True:
        try:
            cwd, warning = recover_working_directory(last_cwd)
            last_cwd = cwd
            if warning:
                print(_paint(warning, YELLOW))
            print("\n\n")
            _rule()
            print(_paint("新任务", BOLD))
            print(_paint(f"工作目录：{cwd}", GRAY))
            user_input = input_session.prompt("你想做什么？> ").strip()
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
        if user_input == "/doctor":
            print_diagnostics(executor)
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
            except (ValueError, OSError) as error:
                print(f"历史操作失败：{error}")
            continue
        if user_input == "/workspace":
            print(f"工作目录：{cwd}")
            continue
        if user_input.startswith("/workspace "):
            target = Path(user_input.split(maxsplit=1)[1]).expanduser()
            try:
                if not target.is_dir():
                    raise ValueError("目标必须是已有目录")
                os.chdir(target.resolve(strict=True))
                engine._task_history.clear()
                print(f"工作目录已切换：{os.getcwd()}")
            except (OSError, ValueError) as error:
                print(f"切换失败：{error}")
            continue
        if user_input == "/trash" or user_input.startswith("/restore "):
            execute_request(engine, executor, history, user_input, cwd, mode)
            continue
        if user_input == "/ssh":
            print_ssh_profiles()
            continue
        if user_input.startswith("/ssh test "):
            alias = user_input.split(maxsplit=2)[2].strip()
            if alias:
                print("第一版暂不支持远程执行。")
            else:
                print("请输入 SSH 别名。")
            continue
        execute_request(engine, executor, history, user_input, cwd, mode)


def entrypoint() -> None:
    parser = argparse.ArgumentParser(description="安全可控的自然语言 Shell 助手")
    parser.add_argument("--batch", help="JSONL 只读任务文件；需要确认的文件修改不执行")
    parser.add_argument("--timeout", type=float, default=60, help="批量主命令超时秒数（默认 60）")
    parser.add_argument("task", nargs="?", help="直接执行一次自然语言任务")
    parser.add_argument("--preview", action="store_true", help="仅生成并展示计划")
    parser.add_argument("--yes", action="store_true", help="仅对受支持的只读任务跳过确认")
    parser.add_argument("--json", action="store_true", help="输出稳定 JSON 结果（适合脚本调用）")
    parser.add_argument("--agent", action="store_true", help="按真实观察逐步决策，最多五次受控工具调用")
    parsed_args = parser.parse_args()
    if not math.isfinite(parsed_args.timeout) or parsed_args.timeout <= 0:
        parser.error("--timeout 必须是有限正数")
    if parsed_args.agent and (not parsed_args.task or parsed_args.batch):
        parser.error("--agent 需要一个明确目标，不能与 --batch 同用")
    main(args=parsed_args)


if __name__ == "__main__":
    entrypoint()
