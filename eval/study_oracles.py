"""离线语义判分与公共执行边界；不向模型提供预期答案。"""
import json
import math
from pathlib import Path
import re
import shlex
from core.execution import ExecutionResult

from core.bash_queries import prepare_queries, review_commands, execute_query, _verify_output, _sensitive
from core.engine import _normalize_paths
from core.operations import prepare_plan, execute_action, preparation_changed
from core.task_plan import TaskPlan, TaskStep, READ_OPERATIONS, parse_operation_plan
from eval.run_eval import normalized_steps, tree_state


def decode(argv, root, now, result="output"):
    if isinstance(argv, str):
        argv = shlex.split(argv)
    if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or "\0" in a for a in argv):
        raise ValueError("候选不是合法参数数组")
    name, args = argv[0], argv[1:]
    if name == "pwd" and args in ([], ["-P"], ["-L"]):
        operation, p = "working_directory", {}
    elif name == "ls":
        flags = {a for a in args if a.startswith('-') and a != '--'}
        if flags - {"-1", "-a", "-A", "-l", "--color=never", "--quoting-style=literal"}:
            raise ValueError("无法判定 ls 参数")
        paths = [a for a in args if not a.startswith('-')]
        if len(paths) > 1:
            raise ValueError("只判定一个目录")
        operation, p = "list_directory", {"path": paths[0] if paths else ".", "include_hidden": bool(flags & {"-a", "-A"})}
    elif name == "find":
        if not args:
            raise ValueError("查找缺少范围")
        p = {"path": args[0], "recursive": True}
        index, lower, upper = 1, None, None
        while index < len(args):
            flag = args[index]
            if flag == "!" and index + 2 < len(args) and args[index+1] in {"-ipath", "-path"}:
                index += 3
                continue
            if flag == "!" and index + 2 < len(args) and args[index+1] == "-newermt":
                upper = float(args[index+2].lstrip('@'))
                index += 3
                continue
            if flag in {"-print", "-print0"}:
                index += 1
                continue
            if index + 1 >= len(args):
                raise ValueError("查找参数缺值")
            value = args[index+1]
            if flag == "-maxdepth" and value == "1":
                p["recursive"] = False
            elif flag == "-mindepth" and value == "1":
                pass
            elif flag == "-type" and value == "f":
                pass
            elif flag == "-name":
                p["pattern"] = value
            elif flag == "-mtime" and re.fullmatch(r'-\d+', value):
                p["modified_within_days"] = int(value[1:])
            elif flag == "-newermt" and value.startswith('@'):
                lower = float(value[1:])
            elif flag == "-size" and re.fullmatch(r'\+\d+c', value):
                p["size_gt_bytes"] = int(value[1:-1])
            else:
                raise ValueError("查找包含不可判定或有副作用的参数")
            index += 2
        if lower is not None:
            days = (now - lower) / 86400
            matched = next((d for d in (1, 7, 14, 30) if abs(days-d) < 1e-7), None)
            if matched is None or upper is None or abs(upper-now) > 1e-6:
                raise ValueError("时间界限不能独立解释")
            p["modified_within_days"] = matched
        operation = "count_files" if result == "count" else "find_files"
    elif name == "grep":
        if len(args) == 7 and args[:3] == ['-F', '-n', '-e'] and args[4] == '--':
            raise ValueError("grep 参数多余")
        if len(args) == 6 and args[:3] == ['-F', '-n', '-e'] and args[4] == '--':
            pattern, path = args[3], args[5]
        elif len(args) == 2 and not any(c in args[0] for c in '.^$*+?[](){}|\\'):
            pattern, path = args
        else:
            raise ValueError("仅离线判定明确字面搜索")
        operation, p = "search_text", {"path": path, "pattern": pattern}
    elif name == "wc" and len(args) in {2, 3} and args[0] in {'-l', '-w', '-c'}:
        if len(args) == 3 and args[1] != '--':
            raise ValueError("统计参数错误")
        operation, p = "text_stats", {"path": args[-1], "metric": {'-l':'lines','-w':'words','-c':'bytes'}[args[0]]}
    elif name in {"head", "tail"} and len(args) in {3, 4} and args[0] == '-n':
        if len(args) == 4 and args[2] != '--':
            raise ValueError("文本参数错误")
        operation, p = "read_text", {"path": args[-1], "end": name, "lines": int(args[1])}
    elif name == "free" and args in (['-b'], ['-h']):
        operation, p = "system_info", {"query": "memory"}
    elif name == "uname" and args in (['-s','-r','-m'], ['-srm'], ['-a']):
        operation, p = "system_info", {"query": "system"}
    elif name == "df" and args:
        if args[:2] == ['-B1', '--output=size,used,avail']:
            if len(args) not in {2, 4} or len(args) == 4 and args[2] != '--':
                raise ValueError("磁盘参数错误")
            path = args[-1] if len(args) == 4 else "."
        elif args[0] == '-h' and len(args) <= 2:
            path = args[-1] if len(args) == 2 else "."
        else:
            raise ValueError("磁盘参数不可判定")
        operation, p = "system_info", {"query": "disk", "path": path}
    elif name in {"cp", "mv"}:
        operands = args[1:] if args[:1] == ['--'] else args
        if len(operands) < 2 or any(a.startswith('-') for a in operands):
            raise ValueError("只转译明确的无覆盖文件操作，不执行这些 Bash 命令")
        target = operands[-1]
        if name == 'mv' and len(operands) == 2 and not (root/target).is_dir() and not target.endswith('/'):
            operation, p = "rename", {"source": operands[0], "destination": target}
        else:
            operation, p = "copy_files" if name == 'cp' else "move_files", {"sources": operands[:-1], "destination": target}
    elif name in {"touch", "mkdir"}:
        operands = args[1:] if args[:1] == ['--'] else args
        if len(operands) != 1 or operands[0].startswith('-'):
            raise ValueError("只转译单一明确新路径")
        operation, p = "create_file" if name == 'touch' else "create_directory", {"path": operands[0]}
    else:
        raise ValueError("候选不属于可离线判定的受限语法")
    plan = parse_operation_plan(json.dumps({"status":"ready", "steps":[{"operation":operation,"parameters":p}]}))
    return _normalize_paths(plan, str(root)).steps[0], argv


def decode_candidates(payload, root, now):
    if not isinstance(payload, dict) or payload.get('status', 'ready') != 'ready':
        raise ValueError("没有可判定命令候选")
    commands = payload.get('commands')
    if commands is None and isinstance(payload.get('command'),str):
        commands=[{'argv':payload['command']}]
    if commands is None and isinstance(payload.get('__unparsed_response__'),str):
        commands=[{'argv':payload['__unparsed_response__']}]
    if not isinstance(commands, list) or not 1 <= len(commands) <= 3:
        raise ValueError("候选必须包含一至三条独立命令")
    steps, arrays = [], []
    for command in commands:
        if not isinstance(command, dict) or command.get('result', 'output') not in {'output','count'}:
            raise ValueError("结果展示类型错误")
        value, argv = decode(command.get('argv'), root, now, command.get('result', 'output'))
        steps.append(value)
        arrays.append(argv)
    return TaskPlan(tuple(steps)), arrays


def signature(plan, root, now):
    if plan.status != 'ready':
        return {'status':plan.status}
    rows = []
    prepared = prepare_plan(plan, str(root), now=now, timeout_seconds=10) if any(s.operation not in READ_OPERATIONS for s in plan.steps) else None
    for index, s in enumerate(plan.steps):
        if s.operation in READ_OPERATIONS:
            normalized = normalized_steps(TaskPlan((s,)), root)[0]
            if s.operation == 'system_info' and s.parameters['query']=='disk':
                normalized['parameters'].setdefault('path','.')
            rows.append(normalized)
        else:
            rows.append({'operation':s.operation, 'effects': sorted((i.get('source',''),i['destination']) for i in prepared['actions'][index]['items'])})
    return rows


def expected_plan(case):
    return parse_operation_plan(json.dumps({'status':'ready','steps':case['expected_steps']}))


def writes(plan, root, now):
    if not any(s.operation not in READ_OPERATIONS for s in plan.steps):
        return []
    return [row for row in signature(plan, root, now) if 'effects' in row]


def common_gate(plan, arrays, root, now):
    previews = []
    for s, argv in zip(plan.steps, arrays):
        one = TaskPlan((s,))
        if s.operation in READ_OPERATIONS:
            prepared = prepare_queries(one, str(root), now=now, timeout_seconds=10)
            review_commands(prepared, {'commands':[{'step':1,'argv':argv,'explanation':'评测候选，不改写'}]})
        else:
            prepared = prepare_plan(one, str(root), now=now, timeout_seconds=10)
            for item in prepared['actions'][0]['items']:
                for key in ('source','destination'):
                    if key in item and _sensitive(root/item[key],root):
                        raise ValueError('公共边界拒绝敏感路径')
        previews.append(prepared)
    return previews


def authorize(case, plan, root, now):
    if not case['executable']:
        return False
    if not any(s.operation not in READ_OPERATIONS for s in plan.steps):
        return True
    return writes(plan, root, now) == writes(expected_plan(case), root, now)


def execute(previews, executor, goal_queries):
    outcomes = []
    for prepared in previews:
        action = prepared['actions'][0]
        if not prepared['read_only']:
            current = prepare_plan(TaskPlan((TaskStep(operation=action['operation'],parameters=action['parameters']),)),
                                   prepared['cwd'], now=prepared['now'], timeout_seconds=10)
            if preparation_changed(prepared, current):
                raise ValueError("确认前文件范围变化")
        outcome = (execute_query if prepared['read_only'] else execute_action)(prepared, action, executor, timeout_seconds=10)
        outcome['execution_kind'] = 'model_bash_argv' if prepared['read_only'] else 'protected_python_translation'
        index = len(outcomes)
        goal = goal_queries.get(index)
        if prepared['read_only']:
            outcome['goal_output_verified'] = bool(goal and action['operation'] == goal['actions'][0]['operation'] and
                _verify_output(goal, goal['actions'][0], ExecutionResult(outcome.get('exit_code'), outcome.get('stdout',''),
                    outcome.get('stderr',''), outcome.get('duration_seconds',0))))
        outcomes.append(outcome)
        if outcome['status'] not in {'verified','no_matches'}:
            break
    return outcomes


def final_oracle(case, outcomes, root, before, now):
    if not outcomes or any(o['status'] not in {'verified','no_matches'} for o in outcomes):
        return False
    expected = dict(before)
    for path, value in case['changes'].items():
        if value == 'absent':
            expected.pop(path, None)
        elif value == 'empty':
            import hashlib
            expected[path] = hashlib.sha256(b'').hexdigest()
        elif value == 'directory':
            expected[path] = 'directory'
        else:
            expected[path] = before[value]
    if tree_state(root) != expected:
        return False
    # 原始查询的正确性在产生写入前判定，不能拿模型或执行器成功标签代替。
    return all(o.get('goal_output_verified') for o in outcomes if o['execution_kind']=='model_bash_argv') and len(outcomes) >= len(case['expected_steps'])
