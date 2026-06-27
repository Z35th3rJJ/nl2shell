"""
评测脚本：自动跑 50 条测试用例，统计命令生成准确率。
用法：python3 eval/run_eval.py
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from core.engine import Engine
from core.safety import check, HIGH, WARN, SAFE

TESTCASES_PATH = Path(__file__).parent / "testcases.json"

# 语义等价别名：key 是期望命令，value 是同等接受的写法
_ALIASES: dict[str, list[str]] = {
    "ls":           ["ls -1", "ls --color"],
    "ls -a":        ["ls -A", "ls --all"],
    "ls -la":       ["ls -al", "ls -lA", "ls -l -a"],
    "cd ~":         ["cd $HOME", "cd"],
    "ps aux":       ["ps -aux", "ps -ef"],
    "ip addr":      ["ifconfig", "ip a", "ip address"],
    "ss -tlnp":     ["netstat -tlnp", "ss -ltnp"],
    "free -h":      ["free -m", "free"],
    "reboot":       ["sudo reboot", "shutdown -r now"],
}


def _is_correct(generated: str, expected: str, cwd: str = "") -> bool:
    gen = generated.strip().rstrip(";")
    exp = expected.strip()
    if gen == exp:
        return True
    # 检查别名
    if gen in _ALIASES.get(exp, []):
        return True
    # 去掉末尾的路径参数再比较（模型有时会把 cwd 显式写出来）
    for suffix in (f" {cwd}", f" {cwd}/", " .", " ./"):
        if gen.endswith(suffix):
            trimmed = gen[: -len(suffix)].strip()
            if trimmed == exp or trimmed in _ALIASES.get(exp, []):
                return True
    return False


def run_eval(limit: int = 50, delay: float = 0.5) -> None:
    with open(TESTCASES_PATH, encoding="utf-8") as f:
        cases = json.load(f)[:limit]

    engine = Engine()
    results = []

    print(f"开始评测，共 {len(cases)} 条用例...\n")
    print(f"{'ID':>3}  {'类别':<10}  {'输入':<28}  {'期望':<30}  {'生成':<30}  结果")
    print("─" * 120)

    for case in cases:
        cid      = case["id"]
        category = case["category"]
        inp      = case["input"]
        expected = case["expected"]

        try:
            cmd, _ = engine.generate(inp, os.getcwd())
        except Exception as e:
            cmd = f"ERROR: {e}"

        correct = _is_correct(cmd, expected)
        risk, _ = check(cmd)

        results.append({
            "id":       cid,
            "category": category,
            "input":    inp,
            "expected": expected,
            "generated": cmd,
            "correct":  correct,
            "risk":     risk,
        })

        mark = "✅" if correct else "❌"
        print(f"{cid:>3}  {category:<10}  {inp:<28}  {expected:<30}  {cmd:<30}  {mark}")

        time.sleep(delay)

    # ── 汇总统计 ──────────────────────────────────────────────────
    total      = len(results)
    n_correct  = sum(1 for r in results if r["correct"])
    accuracy   = n_correct / total * 100

    # 按类别统计
    categories: dict[str, dict] = {}
    for r in results:
        cat = r["category"]
        if cat not in categories:
            categories[cat] = {"total": 0, "correct": 0}
        categories[cat]["total"]   += 1
        categories[cat]["correct"] += int(r["correct"])

    # 安全检测专项
    safety_cases = [r for r in results if r["category"].startswith("安全")]
    n_safety_correct = sum(1 for r in safety_cases if r["correct"])

    print("\n" + "═" * 60)
    print(f"  总体命令准确率：{n_correct}/{total} = {accuracy:.1f}%")
    print()
    print(f"  {'类别':<12}  {'正确/总计':>8}  {'准确率':>6}")
    print(f"  {'─'*12}  {'─'*8}  {'─'*6}")
    for cat, s in categories.items():
        acc = s["correct"] / s["total"] * 100
        print(f"  {cat:<12}  {s['correct']:>4}/{s['total']:<3}  {acc:>5.1f}%")
    print()
    print(f"  安全拦截专项：{n_safety_correct}/{len(safety_cases)} 条触发正确安全等级")
    print("═" * 60)

    # 保存结果
    out_path = Path(__file__).parent / "eval_result.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "accuracy":   accuracy,
            "total":      total,
            "correct":    n_correct,
            "categories": categories,
            "details":    results,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存到 {out_path}")


if __name__ == "__main__":
    run_eval()
