import os
import httpx
from openai import OpenAI

# 按后端缓存客户端，支持同进程内切换（对比实验用）
_clients: dict[str, tuple[OpenAI, str]] = {}


def _get_client(backend: str | None = None) -> tuple[OpenAI, str]:
    """返回 (client, model_name)。backend 为 None 时读 LLM_BACKEND 环境变量。"""
    if backend is None:
        backend = os.environ.get("LLM_BACKEND", "deepseek").lower()

    if backend in _clients:
        return _clients[backend]

    if backend == "local":
        base_url = os.environ.get("LOCAL_BASE_URL", "http://localhost:11434/v1")
        model    = os.environ.get("LOCAL_MODEL", "qwen2.5-coder:7b-instruct")
        client   = OpenAI(
            api_key="ollama",          # Ollama 不校验 key，随便填
            base_url=base_url,
            http_client=httpx.Client(trust_env=False, timeout=120),  # 本地冷启动慢
        )
    else:
        # 默认 deepseek
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "未找到 DEEPSEEK_API_KEY。\n"
                "请在项目目录下创建 .env 文件，写入：\n"
                "  DEEPSEEK_API_KEY=你的密钥"
            )
        model  = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
        client = OpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com",
            http_client=httpx.Client(trust_env=False, timeout=30),
        )

    _clients[backend] = (client, model)
    return client, model


def chat(messages: list[dict], backend: str | None = None) -> str:
    client, model = _get_client(backend)
    return client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0,
    ).choices[0].message.content.strip()
