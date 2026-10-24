"""仅从完整原始需求和问答解析可证明的条件，不读取模型计划或文件事实。"""
import json
import re

from .task_plan import parse_operation_plan


DEFAULT_INCLUDE_HIDDEN = False
UNSPECIFIED_DESTINATIONS = {"合适的目录", "合适的文件夹", "合适的地方", "某个目录", "某个文件夹", "目标目录"}
PATH_TOKEN = r'''(?:"[^"\r\n]+"|'[^'\r\n]+'|“[^”\r\n]+”|‘[^’\r\n]+’|[^\s，。；,;"'“”‘’]+)'''


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
    # shortcut: 仅恢复完整、明确的当前目录统计句式；其他表达或补充回答仍由原流程处理。
    scope = None if answers else re.fullmatch(
        r"统计当前目录(?P<children>(?:及|和)(?:所有)?子目录)?(?P<level>当前层|这一层)?"
        r"(?:中|下)?的(?:普通)?文件(?:数量|个数)(?P<include>[，,](?:包括|包含)(?:所有)?子目录)?",
        user_input)
    if scope is None or (scope["level"] and (scope["children"] or scope["include"])):
        return None
    return bool(scope["children"] or scope["include"])



def _directory_request(user_input, clarifications):
    # shortcut: 仅接受完整单路径浏览语法及隐藏项问答；扩展表达前须增加无歧义解析测试。
    request = re.fullmatch(
        r"(?:列出|查看|浏览)[ \t]+(?P<path>" + PATH_TOKEN + r")[ \t]+(?:目录|文件夹)(?:的)?内容"
        r"(?:[，,][ \t]*(?P<hidden>(?:包括|包含|显示|不包括|不包含|不显示)隐藏(?:文件|项)))?[。]?",
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
    path = request["path"]
    parameters = {"path": path_value(path)}
    parameters["include_hidden"] = DEFAULT_INCLUDE_HIDDEN if hidden is None else hidden
    raw = json.dumps({"status": "ready", "steps": [{"operation": "list_directory", "parameters": parameters}]})
    return parse_operation_plan(raw)



def resolve_request(user_input, clarifications=()):
    """完整匹配才返回独立需求；存在未解释的条件或回答时交回模型流程。"""
    directory = _directory_request(user_input, clarifications)
    if directory is not None:
        return directory
    recursive = count_request_recursive(user_input, clarifications)
    if recursive is not None:
        return parse_operation_plan(json.dumps({"status": "ready", "steps": [{
            "operation": "count_files", "parameters": {"path": ".", "recursive": recursive}}]}))
    return None
