import os
import httpx
from openai import OpenAI

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "未找到 DEEPSEEK_API_KEY。\n"
                "请在项目目录下创建 .env 文件，写入：\n"
                "  DEEPSEEK_API_KEY=你的密钥"
            )
        _client = OpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com",
            http_client=httpx.Client(trust_env=False, timeout=30),
        )
    return _client


def chat(messages: list[dict]) -> str:
    return _get_client().chat.completions.create(
        model="deepseek-v4-flash",
        messages=messages,
        temperature=0,
    ).choices[0].message.content.strip()
