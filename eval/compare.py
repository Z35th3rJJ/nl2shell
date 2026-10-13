"""比较两次操作计划评测；未测量指标不显示为零。"""
import argparse
import json
from pathlib import Path


def compare(first: Path, second: Path):
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in (first, second)]
    if reports[0]["dataset_version"] != reports[1]["dataset_version"] or reports[0]["total"] != reports[1]["total"]:
        raise ValueError("用例版本或数量不同，不能直接比较")
    if any('protocol_version' in r for r in reports):
        if (not all(r.get('complete') for r in reports) or reports[0].get('protocol_version') != reports[1].get('protocol_version')
                or reports[0].get('fixed_now') != reports[1].get('fixed_now')
                or reports[0].get('code_revision') != reports[1].get('code_revision')
                or {r['id'] for r in reports[0]['details']} != {r['id'] for r in reports[1]['details']}):
            raise ValueError('实验不完整、协议、初始时刻或任务编号不同，不能直接比较')
        for key in reports[0]['metrics']:
            values=[]
            for report in reports:
                value=report['metrics'][key]
                values.append('不适用' if value is None else f"{value['numerator']}/{value['denominator']} ({value['percent']:.2f}%)"
                              if isinstance(value,dict) and value['percent'] is not None else str(value))
            print(key,*values,sep=' | ')
        return
    print("模型：", *[report["model"] for report in reports], sep=" | ")
    if reports[0].get("evaluation_version", 1) != reports[1].get("evaluation_version", 1):
        print("注意：判分或执行规则不同，分数变化不能直接解释为模型能力提升。")
    for key, name in [("condition_understanding_accuracy", "条件理解正确率"),
                      ("planning_accuracy", "计划正确率"), ("necessary_clarification_rate", "必要追问率"),
                      ("unsupported_refusal_rate", "范围拒绝率"), ("task_completion_rate", "实际任务完成率")]:
        values = ["未测量" if report.get(key) is None else f"{report[key]:.1f}%" for report in reports]
        print(name, *values, sep=" | ")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    args = parser.parse_args()
    compare(args.first, args.second)
