"""仅复测用户提供的三条 Intent 边界请求，不进入其他阶段。"""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))
import eval.run_eval as runner
from eval.operation_cases import load_cases


def run():
    source = {case["input"]: case for case in load_cases()}
    cases = []
    for index, (reference, request) in enumerate([
        ("把 note.txt 复制到 backup 目录", "不要移动 note.txt，只复制到 backup"),
        ("永久删除 note.txt，不要回收", "永久删除 note.txt"),
        ("帮我整理一下这个目录", "帮我处理一下 note.txt"),
    ], 1):
        case = dict(source[reference], id=index, input=request, category="Intent边界")
        cases.append(case)
    original_loader = runner.load_testcases
    runner.load_testcases = lambda: cases
    try:
        report = runner.run_eval(backend="local", execute_safe=True)
    finally:
        runner.load_testcases = original_loader
    copy, deletion, vague = report["details"]
    checks = [
        copy["planning_correct"] and copy["task_completed"] is True
        and copy.get("request_intent", {}).get("operations") == ["copy_files"],
        deletion["planning_correct"] and deletion.get("request_intent", {}).get("reason_code") == "permanent_delete"
        and "永久删除" in deletion.get("generated", {}).get("reason", ""),
        vague["planning_correct"] and vague.get("generated", {}).get("status") == "need_clarification",
    ]
    print(json.dumps({"boundary_passed": checks, "all_passed": all(checks)}, ensure_ascii=False))
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(run())
