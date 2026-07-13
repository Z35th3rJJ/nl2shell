"""
对比报告：云端 DeepSeek vs 本地模型
用法：python3 eval/compare.py
前提：已先跑
  python3 eval/run_eval.py --backend deepseek
  python3 eval/run_eval.py --backend local
"""
import json
from pathlib import Path

EVAL_DIR = Path(__file__).parent

BACKENDS = [
    ("deepseek", "DeepSeek（云端）"),
    ("local",    "本地模型"),
]


def load(backend: str) -> dict:
    path = EVAL_DIR / f"eval_result_{backend}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"找不到 {path}，请先运行：\n"
            f"  python3 eval/run_eval.py --backend {backend}"
        )
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    data = {}
    for backend, label in BACKENDS:
        try:
            result = load(backend)
            model = result.get("model")
            data[backend] = (result, f"{label}（{model}）" if model else label)
        except FileNotFoundError as e:
            print(f"⚠  {e}\n")
            return

    print("\n" + "═" * 70)
    print("  云端 vs 本地 · 命令生成对比报告")
    print("═" * 70)

    # 总体指标对比
    header = f"  {'指标':<20}"
    for _, label in BACKENDS:
        header += f"  {label:<22}"
    print(header)
    print("  " + "─" * 66)

    def row(name: str, *vals: str) -> None:
        line = f"  {name:<20}"
        for v in vals:
            line += f"  {v:<22}"
        print(line)

    for metric_key, metric_name in [
        ("strict_accuracy",  "严格匹配准确率"),
        ("accuracy",         "语义等价准确率"),
    ]:
        vals = []
        for backend, _ in BACKENDS:
            d, _ = data[backend]
            v = d.get(metric_key, 0)
            n = d.get("strict_correct" if "strict" in metric_key else "correct", 0)
            t = d["total"]
            vals.append(f"{n}/{t} = {v:.1f}%")
        row(metric_name, *vals)

    # 安全拦截率
    inter_vals = []
    for backend, _ in BACKENDS:
        d, _ = data[backend]
        s = d.get("safety", {})
        inter_vals.append(f"{s.get('intercepted','?')}/{s.get('total','?')}")
    row("危险命令拦截率", *inter_vals)
    for metric_key, metric_name in [
        ("intent_accuracy", "意图识别准确率"),
        ("necessary_clarification_rate", "必要澄清率"),
        ("false_positive_rate", "正常命令误报率"),
        ("sensitive_leakage_rate", "敏感信息泄漏率"),
        ("error_fix_success_rate", "错误修复有效率"),
        ("task_completion_rate", "任务完成率"),
    ]:
        row(metric_name, *[f"{data[backend][0].get(metric_key, 0):.1f}%" for backend, _ in BACKENDS])
    row("平均响应时间", *[f"{data[backend][0].get('average_latency_seconds', 0):.2f}s"
                           for backend, _ in BACKENDS])

    # 按类别对比
    print()
    all_cats = list(next(iter(data.values()))[0]["categories"].keys())
    cat_header = f"  {'类别':<14}"
    for _, label in BACKENDS:
        cat_header += f"  {label:<22}"
    print(cat_header)
    print("  " + "─" * 66)

    for cat in all_cats:
        vals = []
        for backend, _ in BACKENDS:
            d, _ = data[backend]
            s = d["categories"].get(cat, {"correct": 0, "total": 0})
            acc = s["correct"] / s["total"] * 100 if s["total"] else 0
            vals.append(f"{s['correct']}/{s['total']} ({acc:.0f}%)")
        row(cat, *vals)

    print("═" * 70 + "\n")

    # 找出差距最大的类别
    diffs = []
    for cat in all_cats:
        accs = []
        for backend, _ in BACKENDS:
            d, _ = data[backend]
            s = d["categories"].get(cat, {"correct": 0, "total": 1})
            accs.append(s["correct"] / s["total"] * 100)
        diffs.append((abs(accs[0] - accs[1]), cat, accs[0], accs[1]))
    diffs.sort(reverse=True)

    print("  差距最大的类别（论文可重点分析）：")
    for diff, cat, a0, a1 in diffs[:3]:
        arrow = "↑" if a0 > a1 else "↓"
        print(f"    {cat:<12}  云端 {a0:.0f}%  vs  本地 {a1:.0f}%  (差 {diff:.0f}%{arrow})")
    print()


if __name__ == "__main__":
    main()
