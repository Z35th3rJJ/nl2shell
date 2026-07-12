"""读取 OpenSSH 配置元数据，不接触私钥内容。"""
import os
from dataclasses import dataclass
from pathlib import Path


def ssh_config_path() -> Path:
    return Path(os.environ.get("SSH_CONFIG_PATH", "~/.ssh/config")).expanduser()


@dataclass(frozen=True)
class SSHHost:
    alias: str
    hostname: str = ""
    user: str = ""
    port: str = ""
    identity_file: str = ""


def load_ssh_profiles(path: Path | None = None) -> list[SSHHost]:
    """读取显式 Host 块的常用连接字段；不读取 IdentityFile 指向的文件。"""
    config_path = path or ssh_config_path()
    if not config_path.exists():
        return []

    profiles: dict[str, dict[str, str]] = {}
    active: list[str] = []
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        key, *values = line.split()
        lowered = key.lower()
        if lowered == "host":
            active = [value for value in values if "*" not in value and "?" not in value]
            for alias in active:
                profiles.setdefault(alias, {})
            continue
        if lowered not in {"hostname", "user", "port", "identityfile"} or not active or not values:
            continue
        field = {"hostname": "hostname", "user": "user", "port": "port", "identityfile": "identity_file"}[lowered]
        for alias in active:
            profiles[alias].setdefault(field, values[0])
    return [SSHHost(alias, **values) for alias, values in profiles.items()]


def load_ssh_hosts(path: Path | None = None) -> list[str]:
    return [profile.alias for profile in load_ssh_profiles(path)]
