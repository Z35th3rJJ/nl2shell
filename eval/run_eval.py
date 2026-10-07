"""评测操作计划与实际任务结果。只在独立临时目录执行匹配期望的计划。"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import os
import re
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
from core.engine import Engine
from core.execution import BashExecutor
from core.llm import model_configuration
from core.operations import execute_action, prepare_plan, workspace_path
from core.redaction import redact_value
from core.task_plan import plan_payload
from eval.operation_cases import load_cases

load_dotenv()


def load_testcases():
    return load_cases()


def create_fixture(root):
    files = {"main.py": b"print('main')", "src/main.py": b"print('nested')", "src/old.py": b"old",
             "backup/existing.py": b"keep backup", "note.txt": b"notes", "small.pdf": b"small",
             "report.pdf": b"P" * 10000001, "organize/a.pdf": b"pdf", "organize/b.txt": b"text"}
    now = time.time()
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        modified = now - (9 * 86400 if name == "src/old.py" else 60)
        os.utime(path, (modified, modified))


def tree_state(root):
    return {path.relative_to(root).as_posix(): "directory" if path.is_dir()
            else hashlib.sha256(path.read_bytes()).hexdigest() for path in root.rglob("*")}


def normalized_steps(plan, root=None):
    steps = [{"operation": s.operation, "parameters": dict(s.parameters)} for s in plan.steps]
    for step in steps:
        parameters = step["parameters"]
        if root is not None:
            root = Path(root).resolve(strict=True)
            for key in ("path", "source", "destination"):
                if key in parameters:
                    parameters[key] = workspace_path(root, parameters[key]).relative_to(root).as_posix()
            if "sources" in parameters:
                parameters["sources"] = [workspace_path(root, path).relative_to(root).as_posix()
                                         for path in parameters["sources"]]
        if step["operation"] == "copy_files" and parameters.get("preserve_structure") is False:
            parameters.pop("preserve_structure")
        if step["operation"] == "copy_files" and parameters.get("preserve_structure") is True:
            source_step = parameters.get("source_step")
            if source_step is not None and steps[source_step - 1]["parameters"].get("recursive") is False:
                parameters.pop("preserve_structure")
        if step["operation"] in {"find_files", "count_files"} and parameters.get("pattern") == "*":
            parameters.pop("pattern")
    return steps


def matches_expected(plan, case, root=None):
    payload = plan_payload(plan)
    if payload["status"] != case["expected_status"]:
        return False
    if payload["status"] != "ready":
        return bool(payload.get("clarification") or payload.get("reason"))
    return normalized_steps(plan, root) == case["expected_steps"]


def execution_allowed(plan, case, root):
    if case["expected_status"] != "ready" or plan_payload(plan)["status"] != "ready":
        return False
    steps = normalized_steps(plan, root)
    expected = case["expected_steps"]
    if steps == expected:
        return True
    extra = len(steps) - len(expected)
    # ponytail: only independent leading file queries; other equivalent plans remain unmeasured.
    return (extra > 0 and steps[extra:] == expected
            and all(step["operation"] in {"find_files", "count_files"} for step in steps[:extra])
            and not any("source_step" in step["parameters"] for step in steps))


def check_result(case, outcomes, root, before):
    """独立文件期望；不把执行器的成功标签直接当成完成用户任务。"""
    if not outcomes or any(item["status"] != "verified" for item in outcomes):
        return False
    if case["selected"] is not None:
        actual = [Path(name).as_posix() for name in outcomes[0].get("files", [])]
        if sorted(actual) != sorted(case["selected"]):
            return False
    if case["count"] is not None and outcomes[0].get("count") != case["count"]:
        return False
    expected = dict(before)
    for path, value in case["changes"].items():
        if value == "absent":
            expected.pop(path, None)
        elif value == "empty":
            expected[path] = hashlib.sha256(b"").hexdigest()
        elif value == "directory":
            expected[path] = "directory"
        else:
            expected[path] = before[value]
    if case["trashed"]:
        records = sorted((root / ".nl2shell-trash").glob("*/record.json"))
        if len(records) != len(case["trashed"]):
            return False
        originals = []
        expected[".nl2shell-trash"] = "directory"
        for record in records:
            identifier = record.parent.name
            if not re.fullmatch(r"[0-9a-f]{32}", identifier):
                return False
            data = json.loads(record.read_text(encoding="utf-8"))
            original = data.get("original")
            if original not in case["trashed"] or data.get("id") != identifier:
                return False
            originals.append(original)
            entry = f".nl2shell-trash/{identifier}"
            expected[entry] = "directory"
            expected[entry + "/data"] = before[original]
            expected[entry + "/record.json"] = hashlib.sha256(record.read_bytes()).hexdigest()
        if sorted(originals) != sorted(case["trashed"]):
            return False
    return tree_state(root) == expected


def run_eval(limit=200, delay=0, backend=None, execute_safe=False, output_dir=None):
    if type(limit) is not int or limit <= 0:
        raise ValueError("limit 必须是正整数")
    configuration = model_configuration(backend)
    backend = configuration["backend"]
    cases = load_testcases()[:limit]
    results = []
    for case in cases:
        started = time.monotonic()
        result = {"id": case["id"], "input": case["input"], "category": case["category"],
                  "expected_status": case["expected_status"], "expected_steps": case["expected_steps"],
                  "planning_correct": False, "executed": False, "task_completed": None}
        with tempfile.TemporaryDirectory(prefix="nl2shell-eval-") as directory:
            root = Path(directory)
            create_fixture(root)
            before = tree_state(root)
            engine = None
            try:
                engine = Engine(backend=backend, ssh_hosts=[])
                plan = engine.generate_task_plan(case["input"], str(root))
                result["generated"] = plan_payload(plan)
                result["planning_correct"] = matches_expected(plan, case, root)
                if execute_safe and case["expected_status"] == "ready":
                    result["task_completed"] = False
                    result["execution_allowed"] = execution_allowed(plan, case, root)
                    if result["execution_allowed"]:
                        prepared = prepare_plan(plan, str(root), timeout_seconds=10)
                        outcomes = []
                        for action in prepared["actions"]:
                            outcome = execute_action(prepared, action, BashExecutor(), timeout_seconds=10)
                            outcomes.append(outcome)
                            if outcome["status"] != "verified":
                                break
                        result.update(executed=any(item["executed"] for item in outcomes), outcomes=outcomes,
                                      task_completed=check_result(case, outcomes, root, before))
                    else:
                        result["execution_block_reason"] = "计划超出本用例允许执行的操作或参数"
            except (ValueError, RuntimeError, OSError) as error:
                result["error"] = str(error)
                if execute_safe and case["expected_status"] == "ready":
                    result["task_completed"] = False
            finally:
                result["model_attempts"] = getattr(engine, "plan_attempts", [])
        result["duration_seconds"] = time.monotonic() - started
        results.append(result)
        print(f"{case['id']}: {'正确' if result['planning_correct'] else '错误'} | {case['input']}")
        if delay:
            time.sleep(delay)
    ready = [r for r in results if r["expected_status"] == "ready"]
    clarification = [r for r in results if r["expected_status"] == "need_clarification"]
    refusal = [r for r in results if r["expected_status"] == "unsupported"]

    def rate(items, field="planning_correct"):
        return sum(bool(item[field]) for item in items) / len(items) * 100 if items else None

    try:
        repo = Path(__file__).parent.parent
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                  capture_output=True, text=True, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                                    capture_output=True, text=True, check=True).stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        revision, dirty = "unknown", None
    repo = Path(__file__).parent.parent
    source = hashlib.sha256()
    for path in sorted([repo / "cli.py", *repo.glob("core/*.py"), *repo.glob("eval/*.py")]):
        source.update(path.relative_to(repo).as_posix().encode() + b"\0" + path.read_bytes())
    report = {**model_configuration(backend), "timestamp": datetime.now(timezone.utc).isoformat(),
              "evaluation_version": 3,
              "code_revision": revision, "working_tree_dirty": dirty,
              "source_version": source.hexdigest(),
              "dataset_version": hashlib.sha256(json.dumps(load_testcases(), ensure_ascii=False,
                                                            sort_keys=True).encode()).hexdigest(),
              "total": len(results), "planning_accuracy": rate(ready),
              "condition_understanding_accuracy": rate([r for r in results if r["category"] == "条件理解"]),
              "necessary_clarification_rate": rate(clarification), "unsupported_refusal_rate": rate(refusal),
              "task_completion_rate": rate(ready, "task_completed") if execute_safe else None,
              "execution_requested": execute_safe, "details": results}
    report = redact_value(report)
    output = Path(output_dir) if output_dir else Path(__file__).parent
    output.mkdir(parents=True, exist_ok=True)
    filename = f"eval_result_{backend}_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_{uuid4().hex[:8]}.json"
    destination = output / filename
    with destination.open("x", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
    print(f"结果已保存：{destination}")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["local", "deepseek"])
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--execute-safe", action="store_true", help="在临时目录执行匹配期望的固定操作")
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("--limit 必须大于 0")
    run_eval(limit=args.limit, backend=args.backend, execute_safe=args.execute_safe)
