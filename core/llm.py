import os
import time
import httpx
from openai import APIConnectionError, APITimeoutError, OpenAI, OpenAIError
from .redaction import redact_messages, redact_text

# 按后端缓存客户端，支持同进程内切换（对比实验用）
_clients: dict[str, tuple[OpenAI, str]] = {}


def model_configuration(backend: str | None = None) -> dict:
    backend = (backend or os.environ.get("LLM_BACKEND", "local")).lower()
    if backend not in {"local", "deepseek"}:
        raise ValueError("LLM_BACKEND 必须是 local 或 deepseek")
    if backend in _clients:
        client, model = _clients[backend]
        return {"backend": backend, "model": model, "base_url": str(client.base_url)}
    return {"backend": backend,
            "model": os.environ.get("LOCAL_MODEL", "qwen2.5-coder:7b") if backend == "local"
            else os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            "base_url": os.environ.get("LOCAL_BASE_URL", "http://localhost:11434/v1")
            if backend == "local" else "https://api.deepseek.com"}


def _get_client(backend: str | None = None) -> tuple[OpenAI, str]:
    """返回 (client, model_name)。backend 为 None 时读 LLM_BACKEND 环境变量。"""
    if backend is None:
        backend = os.environ.get("LLM_BACKEND", "local").lower()
    if backend not in {"local", "deepseek"}:
        raise ValueError("LLM_BACKEND 必须是 local 或 deepseek")

    if backend in _clients:
        return _clients[backend]

    if backend == "local":
        base_url = os.environ.get("LOCAL_BASE_URL", "http://localhost:11434/v1")
        model    = os.environ.get("LOCAL_MODEL", "qwen2.5-coder:7b")
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
    safe_messages = redact_messages(messages)
    for attempt in range(2):
        try:
            response = client.chat.completions.create(
                model=model, messages=safe_messages, temperature=0,
            )
            if not response.choices:
                raise RuntimeError("模型返回了空响应，请重试")
            content = response.choices[0].message.content
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError("模型返回了空响应，请重试")
            return content.strip()
        except (APIConnectionError, APITimeoutError) as error:
            if attempt == 0:
                time.sleep(0.2)
                continue
            raise RuntimeError(f"模型连接失败：{redact_text(str(error))}") from error
        except OpenAIError as error:
            raise RuntimeError(f"模型请求失败：{redact_text(str(error))}") from error
    raise RuntimeError("模型请求失败")
