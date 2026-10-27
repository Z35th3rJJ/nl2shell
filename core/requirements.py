"""仅从完整原始需求和问答解析可证明的条件，不读取模型计划或文件事实。"""
import json
import re

from .task_plan import parse_operation_plan


DEFAULT_INCLUDE_HIDDEN = False
UNSPECIFIED_DESTINATIONS = {"合适的目录", "合适的文件夹", "合适的地方", "某个目录", "某个文件夹", "目标目录"}
PATH_TOKEN = r'''(?:"[^"\r\n]+"|'[^'\r\n]+'|“[^”\r\n]+”|‘[^’\r\n]+’|[^\s，。；,;"'“”‘’]+)'''
_TARGET_MARKER = r"(?:到|进|入|至|放在|存放在|保存在|目标目录(?:为|是|[:：]))"
_COPY_DESTINATION_PATTERN = re.compile(_TARGET_MARKER + r"\s*(?:名为\s*)?(?P<path>" + PATH_TOKEN + ")")


def path_value(token):
    return token[1:-1] if (token[0], token[-1]) in {('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")} else token


def transfer_expression(user_input):
    # shortcut: 裸空格路径仅接受完整单源直接传输；连词、条件或额外动作必须继续澄清。
    spaced_path = r"[\w./\\()\[\]+@%-]+(?:[ \t]+[\w./\\()\[\]+@%-]+)*?"
    path = "(?:" + PATH_TOKEN + "|" + spaced_path + ")"
    request = re.fullmatch(r"(?:将|把)\s*(?P<source>" + path + r")\s*(?P<verb>复制|移动)到\s*"
                          r"(?P<target>" + path + r")(?P<directory>\s+(?:目录|文件夹))?", user_input.strip())
    if request is None:
        return None
    for field in ("source", "target"):
        fragment = request[field]
        if fragment[0] in "\"'“‘":
            continue
        if not re.fullmatch(spaced_path, fragment) or re.search(
                r"还是|命名为|改名|复制到|移动到|不要|如果|(?:^|\s)(?:和|或|或者|以及|并|并且|然后)(?:\s|$)", fragment):
            return None
    return request


def explicit_transfer_operation(user_input, answers):
    # shortcut: 仅核对完整的单源直接传输语法；条件、否定、多操作或补充回答交回原流程。
    if answers:
        return None
    request = transfer_expression(user_input)
    if request is None or request["target"] in UNSPECIFIED_DESTINATIONS:
        return None
    return "copy_files" if request["verb"] == "复制" else "move_files"


def count_request_recursive(user_input, answers):
    plan = _directory_query(user_input, answers, "count_files")
    return None if plan is None else plan.steps[0].parameters["recursive"]


def _directory_query(user_input, answers, operation):
    # shortcut: 只解析完整的单目录、无额外筛选的计数或最大值；其他条件和回答仍交回模型。
    verb = r"(?:查找|找一下|找出|找)" if operation == "largest_file" else "统计"
    result = r"(?:的)?最大(?:的)?(?:普通)?文件" if operation == "largest_file" else r"的(?:普通)?文件(?:数量|个数)"
    scope = None if answers else re.fullmatch(
        r"(?P<recursive>递归)?" + verb + r"[ \t]*(?:(?P<current>当前目录)|(?P<path>" + PATH_TOKEN + r")[ \t]*(?:目录|文件夹))"
        r"(?P<children>(?:及|和)(?:其|所有)?子目录)?(?P<level>当前层|这一层)?"
        r"(?:中|下)?" + result +
        r"(?:[，,](?P<include>包括|包含|不包括|不包含)(?:所有)?子目录)?[。]?", user_input.strip())
    if scope is None:
        return None
    recursive = bool(scope["recursive"] or scope["children"] or scope["include"] in {"包括", "包含"})
    if (scope["level"] and recursive or (scope["recursive"] or scope["children"]) and
            scope["include"] in {"不包括", "不包含"}):
        return None
    path = "." if scope["current"] else path_value(scope["path"])
    if not path.strip():
        return None
    return parse_operation_plan(json.dumps({"status": "ready", "steps": [{
        "operation": operation, "parameters": {"path": path, "recursive": recursive}}]}))



def _directory_request(user_input, clarifications):
    # shortcut: 仅接受完整单路径浏览语法及隐藏项问答；扩展表达前须增加无歧义解析测试。
    request = re.fullmatch(
        r"(?:列出[ \t]*(?P<bare_current>当前目录|本目录)(?:这一层|当前层)?|"
        r"(?:列出|查看|浏览)[ \t]*(?:(?P<current>当前目录|本目录|这里)(?:这一层|当前层)?(?:的)?(?:内容|文件和文件夹)|"
        r"(?P<path>" + PATH_TOKEN + r")[ \t]+(?:目录|文件夹)(?:的)?内容))"
        r"(?:[，,][ \t]*(?P<hidden>(?:包括|包含|显示|列出|不包括|不包含|不显示|不列)隐藏(?:文件|项)))?[。]?",
        user_input.strip())
    if request is None:
        return None
    hidden = None if request["hidden"] is None else not request["hidden"].startswith("不")
    for clarification in clarifications:
        answer = re.fullmatch(
            r"问题：(?:是否包含隐藏(?:文件|项)|隐藏(?:文件|项)是否包含)[？?]?；用户回答："
            r"(?P<answer>是|否|包含|不包含|包括|不包括|yes|no)", clarification, re.I)
        if answer is None:
            return None
        confirmed = answer["answer"].lower() in {"是", "包含", "包括", "yes"}
        if hidden is not None and hidden != confirmed:
            return None
        hidden = confirmed
    parameters = {"path": '.' if request['current'] or request['bare_current'] else path_value(request['path'])}
    parameters["include_hidden"] = DEFAULT_INCLUDE_HIDDEN if hidden is None else hidden
    raw = json.dumps({"status": "ready", "steps": [{"operation": "list_directory", "parameters": parameters}]})
    return parse_operation_plan(raw)


def _file_query(user_input, clarifications):
    if clarifications:
        return None
    inode = re.fullmatch(r"(?:显示|查看|查询)[ \t]*(?P<path>" + PATH_TOKEN +
                         r")[ \t]*的[ \t]*inode", user_input.strip(), re.I)
    literal = re.fullmatch(r"在[ \t]*(?P<path>" + PATH_TOKEN +
                           r")[ \t]*中搜索字面(?:文本|字符串)[ \t]+(?P<pattern>" + PATH_TOKEN + r")", user_input.strip())
    if inode:
        operation, parameters = 'file_inode', {'path':path_value(inode['path'])}
    elif literal:
        operation, parameters = 'search_text', {'path':path_value(literal['path']),
                                                'pattern':path_value(literal['pattern'])}
    else:
        return None
    return parse_operation_plan(json.dumps({'status':'ready','steps':[{'operation':operation,'parameters':parameters}]}))



def resolve_request(user_input, clarifications=()):
    """完整匹配才返回独立需求；存在未解释的条件或回答时交回模型流程。"""
    directory = _directory_request(user_input, clarifications)
    if directory is not None:
        return directory
    file_query = _file_query(user_input, clarifications)
    if file_query is not None:
        return file_query
    for operation in ("count_files", "largest_file"):
        plan = _directory_query(user_input, clarifications, operation)
        if plan is not None:
            return plan
    return None
