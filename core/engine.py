from .llm import chat

_SYSTEM = """你是一个 Linux Shell 命令专家助手。用户用中文描述想要执行的操作，你输出两行内容：

第一行：命令本身，不加任何标记或反引号
第二行：一句简短的中文，说明这条命令的作用

注意：
- 我会告诉你当前目录，这只是上下文参考，不要把当前目录路径作为参数附加到命令里
- 若需要多条命令，用 && 连接写在第一行
- 若意图模糊或无法转为命令，输出：CANNOT_GENERATE: <简短原因>"""


class Engine:
    def __init__(self):
        self._history: list[tuple[str, str]] = []

    def generate(self, user_input: str, cwd: str) -> tuple[str, str]:
        """返回 (命令, 说明)。命令可能是 CANNOT_GENERATE: ... 开头的错误信息。"""
        messages = [{"role": "system", "content": _SYSTEM}]

        for past_input, past_cmd in self._history[-3:]:
            messages.append({"role": "user", "content": past_input})
            messages.append({"role": "assistant", "content": past_cmd})

        messages.append({"role": "user", "content": f"当前目录：{cwd}\n{user_input}"})

        raw = chat(messages)
        lines = raw.strip().split("\n", 1)
        cmd = lines[0].strip()
        explanation = lines[1].strip() if len(lines) > 1 else ""

        self._history.append((user_input, cmd))
        return cmd, explanation
