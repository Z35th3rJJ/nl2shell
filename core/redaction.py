"""统一敏感信息脱敏，覆盖模型请求、历史与日志。"""
import re


_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:API[_-]?KEY|TOKEN|PASSWORD|PASSWD|SECRET|COOKIE|DATABASE_URL|DB_URL)[A-Z0-9_]*)"
    r"\s*([=:])\s*([^\s,'\"}]+)"
)
_BEARER = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]+")
_COMMON_TOKEN = re.compile(r"\b(?:gh[opsu]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,})\b")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
    re.DOTALL,
)


def redact_text(text: str) -> str:
    text = _PRIVATE_KEY.sub("<REDACTED_PRIVATE_KEY>", text)
    text = _ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}<REDACTED>", text)
    text = _BEARER.sub(r"\1<REDACTED>", text)
    return _COMMON_TOKEN.sub("<REDACTED_TOKEN>", text)


def redact_value(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            key: "<REDACTED>" if str(key).lower() in {
                "api_key", "token", "password", "secret", "cookie", "environment",
            } else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        redacted = [redact_value(item) for item in value]
        return tuple(redacted) if isinstance(value, tuple) else redacted
    return value


def redact_messages(messages: list[dict]) -> list[dict]:
    return [redact_value(message) for message in messages]
