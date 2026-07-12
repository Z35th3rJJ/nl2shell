import re
from .llm import chat
from .ssh_config import load_ssh_hosts
from .task_plan import TaskPlan, parse_task_plan

# 模型输出前缀常量（供 cli 和测试复用）
CANNOT_GENERATE_PREFIX = "CANNOT_GENERATE:"
CLARIFY_PREFIX         = "CLARIFY:"


def classify_output(text: str) -> str:
    """解析模型输出类型，返回 'clarify' | 'cannot' | 'command'。"""
    t = text.strip()
    if t.startswith(CLARIFY_PREFIX):
        return "clarify"
    if t.startswith(CANNOT_GENERATE_PREFIX):
        return "cannot"
    return "command"


def _strip_fences(text: str) -> str:
    """剥除模型偶尔输出的代码围栏和反引号，只保留命令本身。"""
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip("`").strip()


_SYSTEM = """你是一个 Linux Shell 命令专家助手。用户用中文描述想要执行的操作，你输出以下内容之一：

【情况一：意图明确】输出两行：
第一行：命令本身，不加任何标记或反引号
第二行：一句简短的中文，说明这条命令的作用

【情况二：意图模糊、但追问一个问题就能明确】输出一行：
CLARIFY: <向用户提出的一个具体问题>

【情况三：根本无法转为 Linux 命令】输出一行：
CANNOT_GENERATE: <简短原因>

注意：
- 绝大多数指令都是明确的，请直接给命令，不要过度追问
- 只有在「不追问就无法选择正确命令」时才使用 CLARIFY，且每次只问一个问题
- 我会告诉你当前目录，这只是上下文参考，不要把当前目录路径作为参数附加到命令里
- 若需要多条命令，用 && 连接写在第一行"""

_AGENT_SYSTEM = """你是安全可控 Linux Shell Agent。把用户任务拆成最多 3 个按顺序执行的步骤，只输出 JSON：
{"steps":[{"command":"...","explanation":"中文说明","expected":"预期结果","verification":"只读验证命令"}]}
规则：
- command 是 Bash 命令；每一步都必须独立可执行。
- verification 只能是 ls、test、cat、grep、head、tail、wc、stat 等只读命令；不确定时用空字符串。
- 不生成 sudo、网络下载、破坏性系统命令；必要时让 command 为空并在 explanation 说明不能执行。
- 当前目录只是上下文，不要把它无意义地展开成绝对路径。
- 不要猜测文件名。复制或移动任务没有给出目标路径时，输出 {"clarification":"需要询问的问题"}。
- 不使用 Markdown 代码围栏。"""


class Engine:
    def __init__(self, backend: str | None = None, ssh_hosts: list[str] | None = None):
        self._history: list[tuple[str, str]] = []
        self._backend = backend  # None 表示读环境变量
        self._ssh_hosts = load_ssh_hosts() if ssh_hosts is None else ssh_hosts

    def remember(self, user_input: str, command: str) -> None:
        """把已完成生成的命令加入短期模型上下文。"""
        self._history.append((user_input, command))

    def generate(
        self,
        user_input: str,
        cwd: str,
        followups: list[tuple[str, str]] | None = None,
    ) -> tuple[str, str]:
        """返回 (输出, 说明)。
        输出可能是：正常命令 / CLARIFY:<问题> / CANNOT_GENERATE:<原因>。
        followups: 本轮澄清对话的 [(assistant的CLARIFY串, 用户回答), ...]，
                   不为空时追加在当前 user 消息之后，让模型带上下文再生成。
        """
        system = _SYSTEM
        if self._ssh_hosts:
            system += "\n可用 SSH Host 别名：" + ", ".join(self._ssh_hosts)
        messages = [{"role": "system", "content": system}]

        for past_input, past_cmd in self._history[-3:]:
            messages.append({"role": "user", "content": past_input})
            messages.append({"role": "assistant", "content": past_cmd})

        messages.append({"role": "user", "content": f"当前目录：{cwd}\n{user_input}"})

        # 把澄清对话轮次追加到当前 user 消息之后
        if followups:
            for clarify_q, user_ans in followups:
                messages.append({"role": "assistant", "content": clarify_q})
                messages.append({"role": "user", "content": user_ans})

        raw = chat(messages, backend=self._backend)
        lines = raw.strip().split("\n", 1)
        first = _strip_fences(lines[0].strip())
        rest  = lines[1].strip() if len(lines) > 1 else ""

        return first, rest

    def generate_task_plan(self, user_input: str, cwd: str, clarifications: list[str] | None = None) -> TaskPlan:
        """生成结构化任务计划；旧两行命令输出会自动降级为单步计划。"""
        system = _AGENT_SYSTEM
        if self._ssh_hosts:
            system += "\n可用 SSH Host 别名：" + ", ".join(self._ssh_hosts)
        prompt = f"当前目录：{cwd}\n{user_input}"
        if clarifications:
            prompt += "\n用户补充确认：\n- " + "\n- ".join(clarifications)
        raw = chat([
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ], backend=self._backend)
        return parse_task_plan(raw)

    def suggest_fix(self, command: str, detail: str) -> str:
        """仅生成修复建议，不执行建议中的命令。"""
        return chat([
            {"role": "system", "content": "你是 Linux Shell 故障诊断助手。只用中文给出一条简短修复建议，不输出会自动执行的命令。"},
            {"role": "user", "content": f"命令：{command}\n失败信息：{detail}"},
        ], backend=self._backend)
