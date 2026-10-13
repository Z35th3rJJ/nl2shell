"""受限只读命令：计划对照、参数审查和独立结果验证。"""
import fnmatch
import json
import math
import os
from pathlib import Path
import re
import shlex
import stat
import time

from .operations import workspace_path, find_files, _check_deadline
from .task_plan import plan_payload, validate_parameters
from .redaction import redact_text

QUERY_OPERATIONS = {"working_directory", "list_directory", "find_files", "count_files",
                    "search_text", "text_stats", "read_text", "system_info"}
COMMANDS = {"pwd", "ls", "find", "grep", "wc", "head", "tail", "df", "free", "uname"}
SENSITIVE = (".git", ".ssh", ".aws", ".config", ".nl2shell-trash", ".env", ".env.*",
             "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*",
             "credentials", ".netrc", ".npmrc")
FIND_EXCLUSIONS = tuple("*/" + name for name in SENSITIVE) + tuple(
    "*/" + name + "/*" for name in (".git", ".ssh", ".aws", ".config", ".nl2shell-trash"))
TEXT_LIMIT = 1024 * 1024


def uses_bash(plan):
    return bool(plan.steps) and all(step.operation in QUERY_OPERATIONS for step in plan.steps)


def same_query_plan(plan, reference):
    def signature(value):
        result = []
        for step in value.steps:
            p = dict(step.parameters)
            if step.operation in {"find_files", "count_files"}:
                p.setdefault("pattern", "*")
            if step.operation == "system_info" and p["query"] == "disk":
                p.setdefault("path", ".")
            result.append((step.operation, p))
        return result
    return reference.status == "ready" and signature(plan) == signature(reference)


def _sensitive(path, root):
    return any(fnmatch.fnmatchcase(part.lower(), pattern) for part in (*root.parts, *path.relative_to(root).parts)
               for pattern in SENSITIVE)


def _path(root, value, excluded):
    path = workspace_path(root, value)
    if _sensitive(path, root) or any(path == item or item in path.parents for item in excluded):
        raise ValueError("敏感文件或审计记录不允许读取")
    if any(char in str(path) for char in "\r\n"):
        raise ValueError("暂不支持名称中包含换行的文件")
    info = path.lstat()
    mode = info.st_mode
    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise ValueError("只支持普通文件和目录")
    if stat.S_ISREG(mode) and info.st_nlink != 1:
        raise ValueError("不读取硬链接文件")
    return path


def _identity(path):
    value = path.stat()
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _operand(value):
    return "." if value == "." else "./" + value


def _time_bounds(p, now):
    return math.ceil((now - p["modified_within_days"] * 86400) * 1e9), math.floor(now * 1e9)


def _timestamp(value):
    magnitude = abs(value)
    return f"@{'-' if value < 0 else ''}{magnitude // 10**9}.{magnitude % 10**9:09d}"


def required_argv(operation, p, now, excluded=()):
    """每个槽位唯一对应计划条件；不使用宽泛的命令名白名单。"""
    path = _operand(p["path"]) if "path" in p else "."
    if operation == "working_directory":
        return ["pwd", "-P"]
    if operation == "list_directory":
        return ["ls", "-1", "--color=never", "--quoting-style=literal", *(["-A"] if p["include_hidden"] else []), "--", path]
    if operation in {"find_files", "count_files"}:
        argv = ["find", path, "-mindepth", "1"]
        if not p["recursive"]:
            argv += ["-maxdepth", "1"]
        argv += ["-type", "f"]
        for pattern in FIND_EXCLUSIONS:
            argv += ["!", "-ipath", pattern]
        for pattern in excluded:
            argv += ["!", "-path", pattern]
        if p.get("pattern", "*") != "*":
            argv += ["-name", p["pattern"]]
        if "modified_within_days" in p:
            lower, upper = _time_bounds(p, now)
            argv += ["-newermt", _timestamp(lower - 1), "!", "-newermt", _timestamp(upper)]
        if "size_gt_bytes" in p:
            argv += ["-size", "+" + str(math.floor(p["size_gt_bytes"])) + "c"]
        return argv + ["-print0"]
    if operation == "search_text":
        return ["grep", "-F", "-n", "-e", p["pattern"], "--", path]
    if operation == "text_stats":
        return ["wc", {"lines": "-l", "words": "-w", "bytes": "-c"}[p["metric"]], "--", path]
    if operation == "read_text":
        return [p["end"], "-n", str(p["lines"]), "--", path]
    if operation == "system_info":
        return {"disk": ["df", "-B1", "--output=size,used,avail", "--", path],
                "memory": ["free", "-b"], "system": ["uname", "-s", "-r", "-m"]}[p["query"]]
    raise ValueError("不支持的只读任务")


def prepare_queries(plan, cwd, *, now=None, timeout_seconds=60, excluded_paths=()):
    if (not uses_bash(plan) or plan.status != "ready" or plan.refused or plan.clarification
            or not 1 <= len(plan.steps) <= 3):
        raise ValueError("不是完整只读查询计划")
    root = Path(cwd).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("工作目录必须是目录")
    now = time.time() if now is None else now
    deadline = time.monotonic() + timeout_seconds
    excluded = [Path(value).resolve() for value in excluded_paths]
    excluded_arguments = ["./" + value.relative_to(root).as_posix() for value in excluded
                          if value.is_relative_to(root)]
    actions = []
    for index, step in enumerate(plan.steps, 1):
        _check_deadline(deadline)
        if step.command or step.verification:
            raise ValueError("不能执行旧格式自由命令")
        validate_parameters(step.operation, step.parameters, index - 1, plan.steps[:index - 1])
        p, operation = dict(step.parameters), step.operation
        if "path" in p:
            path = _path(root, p["path"], excluded)
            p["path"] = path.relative_to(root).as_posix()
        action = {"step": index, "operation": operation, "parameters": p,
                  "explanation": step.explanation, "items": [], "files": [],
                  "execution_kind": "model_bash_argv", "_identities": {}}
        if operation in {"find_files", "count_files"}:
            query = {k: v for k, v in p.items() if k != "modified_within_days"}
            files = find_files(root, query, now, excluded, deadline)
            files = [value for value in files if not _sensitive(value, root)]
            if "modified_within_days" in p:
                lower, upper = _time_bounds(p, now)
                files = [value for value in files if lower <= value.stat().st_mtime_ns <= upper]
            for value in files:
                _check_deadline(deadline)
                _path(root, str(value), excluded)
                name = value.relative_to(root).as_posix()
                action["files"].append(name)
                action["_identities"][name] = _identity(value)
        elif operation == "list_directory":
            if not path.is_dir():
                raise ValueError("列出内容需要目录路径")
            names = []
            for value in path.iterdir():
                _check_deadline(deadline)
                if p["include_hidden"] or not value.name.startswith("."):
                    if value.is_symlink() or any(char in value.name for char in "\r\n"):
                        raise ValueError("查询范围包含链接或换行名称")
                    names.append(value.name)
            action["_expected"] = sorted(names)
        elif operation in {"search_text", "text_stats", "read_text"}:
            if not path.is_file() or path.stat().st_size > TEXT_LIMIT:
                raise ValueError("只读取不超过 1 MiB 的普通 UTF-8 文本文件")
            with path.open("rb") as file:
                data = file.read(TEXT_LIMIT + 1)
            if len(data) > TEXT_LIMIT:
                raise ValueError("文本文件超过 1 MiB")
            text = data.decode("utf-8")
            if redact_text(text) != text:
                raise ValueError("文本包含凭据或私钥，禁止读取")
            if b"\0" in data:
                raise ValueError("不读取二进制文件")
            action["_identities"][p["path"]] = _identity(path)
            lines = data.split(b"\n")
            lines = [line + b"\n" for line in lines[:-1]] + ([lines[-1]] if lines[-1] else [])
            if operation == "search_text":
                pattern = p["pattern"].encode("utf-8")
                action["_expected"] = b"".join(str(i).encode() + b":" + line.rstrip(b"\n") + b"\n"
                    for i, line in enumerate(lines, 1) if pattern in line).decode("utf-8")
            elif operation == "read_text":
                selected = lines[:p["lines"]] if p["end"] == "head" else lines[-p["lines"]:]
                action["_expected"] = b"".join(selected).decode("utf-8")
            else:
                if p["metric"] == "words" and any(not char.isprintable() or char.isspace()
                    for char in text if char not in " \t\n\r\v\f"):
                    raise ValueError("单词统计暂不支持特殊 Unicode 空白或控制字符")
                action["_expected"] = {"lines": data.count(b"\n"), "words": len(text.split()),
                                       "bytes": len(data)}[p["metric"]]
        action["_required_argv"] = required_argv(operation, p, now, excluded_arguments)
        actions.append(action)
    return {"cwd": str(root), "now": now, "actions": actions,
            "excluded": [str(value) for value in excluded], "read_only": True}


def command_request(plan, prepared):
    tasks = []
    for action in prepared["actions"]:
        argv = action["_required_argv"]
        p = action["parameters"]
        rule = {"step": action["step"], "operation": action["operation"], "parameters": p}
        if argv[0] in {"ls", "grep", "wc", "head", "tail", "df"}:
            rule["required_suffix"] = argv[-2:]
        # 固定安全槽位由程序规定；模型必须填写计划中的对象和全部筛选条件。
        if action["operation"] in {"find_files", "count_files"}:
            suffix_at = next((i for i, value in enumerate(argv) if value in {"-name", "-newermt", "-size", "-print0"}), len(argv))
            rule["mandatory_prefix"] = argv[:suffix_at]
            if "modified_within_days" in p:
                lower, upper = _time_bounds(p, prepared["now"])
                rule["time_bounds"] = {"after": _timestamp(lower - 1), "before": _timestamp(upper)}
        tasks.append(rule)
    rules = """根据已审查计划生成候选 Bash 参数数组及中文解释。只输出 {"commands":[{"step":1,"argv":["pwd","-P"],"explanation":"说明"}]}，每步一条。不输出 Markdown 或命令字符串。
参数规则（顺序固定，变量必须来自计划）：
working_directory: ["pwd","-P"]
list_directory: ["ls","-1","--color=never","--quoting-style=literal","--","PATH"]。include_hidden=true 时在 "--" 前加 "-A"。
find_files/count_files: 保留 mandatory_prefix；pattern 非 * 时追加 [-name,pattern]；有时间时追加 [-newermt,time_bounds.after,!, -newermt,time_bounds.before]；有大小时追加 [-size,+字节数c]；最后追加 -print0。禁止管道，计数由程序核验返回路径后计算。
search_text: ["grep","-F","-n","-e","PATTERN","--","PATH"]，字面匹配。
text_stats: ["wc","METRIC","--","PATH"]，lines=-l、words=-w、bytes=-c。
read_text: ["END","-n","LINES","--","PATH"]，END=head或tail，LINES 是计划行数的十进制字符串。
system_info: disk=["df","-B1","--output=size,used,avail","--","PATH"]，没有 path 时 PATH 为 .；memory=["free","-b"]；system=["uname","-s","-r","-m"]。
模板变量必须替换成计划实际值。"--" 是一个真实的独立参数，不是省略号，必须保留，不能遗漏。
每步有 required_suffix 时，必须把这两个实际参数逐字放在 argv 的最后。不要改回 plan 中的路径，不要省略 ./ 或把 -- 写成 -。
路径 . 保持 .，其他相对路径必须加 ./ 前缀。数组每个参数独立，不加 Shell 引号或转义，不把整条命令放进一个元素。
禁止额外参数、命令拼接、管道、重定向、命令替换、脚本、改变目录或修改文件。必须保留所有条件。"""
    return [{"role": "system", "content": rules},
            {"role": "user", "content": json.dumps({"plan": plan_payload(plan), "rules": tasks}, ensure_ascii=False)}]


def review_commands(prepared, payload):
    if not isinstance(payload, dict) or set(payload) != {"commands"}:
        raise ValueError("候选必须是 commands 对象，不能是任意命令字符串")
    commands = payload["commands"]
    if not isinstance(commands, list) or len(commands) != len(prepared["actions"]):
        raise ValueError("候选命令数量与计划不一致")
    for action, candidate in zip(prepared["actions"], commands):
        if (not isinstance(candidate, dict) or set(candidate) != {"step", "argv", "explanation"}
                or type(candidate["step"]) is not int or candidate["step"] != action["step"]
                or not isinstance(candidate["explanation"], str) or not candidate["explanation"].strip()):
            raise ValueError("候选缺少步骤、参数数组或解释")
        argv = candidate["argv"]
        if (not isinstance(argv, list) or not argv or
                any(not isinstance(value, str) or "\0" in value for value in argv)
                or argv != action["_required_argv"]):
            action["candidate_argv"] = argv
            action["review"] = "rejected"
            raise ValueError("候选命令不符合计划、参数或路径规则；未执行，也不替换命令")
        action.update(candidate_argv=list(argv), command=shlex.join(argv),
                      candidate_explanation=candidate["explanation"], review="accepted")
    return prepared


def query_preview(prepared):
    return [{key: value for key, value in action.items() if not key.startswith("_")}
            for action in prepared["actions"]]


def _verify_output(prepared, action, result):
    root = Path(prepared["cwd"])
    operation, p = action["operation"], action["parameters"]
    for name, identity in action["_identities"].items():
        path = _path(root, name, [Path(value) for value in prepared["excluded"]])
        if _identity(path) != identity:
            raise ValueError("查询对象已变化，实际结果尚未验证")
    output = result.stdout
    if operation in {"find_files", "count_files"}:
        if output and not output.endswith("\0"):
            return False
        names = [_path(root, value, [Path(v) for v in prepared["excluded"]]).relative_to(root).as_posix()
                 for value in output.split("\0") if value]
        return sorted(names) == sorted(action["files"])
    if operation == "working_directory":
        return output == str(root) + "\n"
    if operation == "list_directory":
        return sorted(output.splitlines()) == action["_expected"]
    if operation in {"search_text", "read_text"}:
        return output == action["_expected"] and (operation != "search_text" or
               result.exit_code == (0 if action["_expected"] else 1))
    if operation == "text_stats":
        match = re.fullmatch(r"\s*(\d+)\s+" + re.escape(_operand(p["path"])) + r"\n", output)
        return bool(match) and int(match[1]) == action["_expected"]
    if p["query"] == "system":
        system = os.uname()
        return output == f"{system.sysname} {system.release} {system.machine}\n"
    if p["query"] == "memory":
        match = re.search(r"^Mem:\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$", output, re.M)
        total = int(re.search(r"^MemTotal:\s+(\d+) kB", Path('/proc/meminfo').read_text(), re.M)[1]) * 1024
        return bool(match) and int(match[1]) == total and all(0 <= int(v) <= total for v in match.groups()[1:])
    values = output.splitlines()
    if len(values) != 2 or values[0].split() != ["1B-blocks", "Used", "Avail"]:
        return False
    numbers = values[1].split()
    if len(numbers) != 3 or not all(value.isdigit() for value in numbers):
        return False
    total, used, available = map(int, numbers)
    info = os.statvfs(workspace_path(root, p.get("path", ".")))
    return total == info.f_blocks * info.f_frsize and 0 <= used <= total and 0 <= available <= total


def execute_query(prepared, action, executor, *, timeout_seconds=60):
    from .execution import ExecutionStartError
    outcome = {"step": action["step"], "operation": action["operation"], "status": "unverified",
               "execution_kind": "model_bash_argv", "candidate_argv": action.get("candidate_argv"),
               "review": action.get("review"), "items": [], "executed": False, "task_completed": False,
               "execution_state": "not_started"}
    started = time.monotonic()
    phase = "review"
    try:
        if action.get("review") != "accepted" or action.get("candidate_argv") != action["_required_argv"]:
            raise ValueError("未通过审查的命令不能执行")
        # 执行前重查真实环境；不把旧的文件快照当作当前状态。
        phase = "environment"
        from .task_plan import TaskPlan, TaskStep
        current = prepare_queries(TaskPlan((TaskStep(operation=action["operation"], parameters=action["parameters"]),)),
                                  prepared["cwd"], now=prepared["now"], timeout_seconds=timeout_seconds,
                                  excluded_paths=prepared["excluded"])["actions"][0]
        if any(current.get(key) != action.get(key) for key in ("files", "_identities", "_expected", "_required_argv")):
            raise ValueError("查询范围或文件已变化，请重新确认")
        phase = "execution"
        outcome["execution_state"] = "unknown"
        result, actual = executor.execute_query(action["candidate_argv"], action["operation"], action["parameters"],
                                               prepared["cwd"], prepared["now"], prepared["excluded"],
                                               timeout_seconds=timeout_seconds)
        outcome.update(executed=True, actual_argv=actual, exit_code=result.exit_code,
                       stdout=result.stdout, stderr=result.stderr, timed_out=result.timed_out,
                       output_truncated=result.output_truncated, command_succeeded=result.exit_code == 0,
                       execution_state=("exited" if not result.timed_out and type(result.exit_code) is int
                                        else result.process_state))
        phase = "verification"
        no_match = action["operation"] == "search_text" and result.exit_code == 1
        if result.timed_out or (result.exit_code != 0 and not no_match):
            phase = "execution"
            outcome.update(status="execution_failed", detail="命令失败或超时，未自动修复")
        elif result.output_truncated or result.stderr or not _verify_output(prepared, action, result):
            outcome.update(status="unverified", detail="命令已运行，但输出或实际环境未通过独立验证")
        else:
            empty = not result.stdout
            outcome.update(status="no_matches" if empty and action["operation"] != "count_files" else "verified",
                           task_completed=not empty or action["operation"] == "count_files",
                           result_kind="no_matches" if empty else "matched",
                           detail="没有匹配结果" if empty else "实际命令输出与独立检查一致")
            if action["operation"] == "count_files":
                outcome["count"] = len(action["files"])
    except (OSError, ValueError, RuntimeError, TypeError, KeyboardInterrupt) as error:
        status = "unverified" if phase == "verification" else "blocked" if phase in {"review", "environment"} and isinstance(error, ValueError) else "execution_failed"
        outcome.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else status,
                       detail=str(error), timed_out=isinstance(error, TimeoutError),
                       error_type=type(error).__name__, error_number=getattr(error, "errno", None))
        if isinstance(error, ExecutionStartError):
            outcome["execution_state"] = "not_started"
            outcome["attempted_argv"] = getattr(error, "attempted_argv", [])
    outcome["failure_phase"] = phase
    outcome["duration_seconds"] = time.monotonic() - started
    return outcome
