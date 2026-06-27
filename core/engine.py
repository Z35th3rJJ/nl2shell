from .llm import chat

_SYSTEM = """你是一个 Linux Shell 命令专家助手。用户用中文描述想要执行的操作，你只输出对应的 Shell 命令。

规则：
- 只输出命令本身，不加任何解释、不加 markdown 代码块标记（不要用反引号包裹）
- 若需要多条命令，用 && 连接写在一行
- 若意图模糊或无法转为命令，输出：CANNOT_GENERATE: <简短原因>"""


class Engine:
    def __init__(self):
        self._history: list[tuple[str, str]] = []

    def generate(self, user_input: str, cwd: str) -> str:
        messages = [{"role": "system", "content": _SYSTEM}]

        for past_input, past_cmd in self._history[-3:]:
            messages.append({"role": "user", "content": past_input})
            messages.append({"role": "assistant", "content": past_cmd})

        messages.append({"role": "user", "content": f"当前目录：{cwd}\n{user_input}"})

        cmd = chat(messages)
        self._history.append((user_input, cmd))
        return cmd
