"""只读取标准 OpenSSH config 中的 Host 别名，不接触私钥内容。"""
import os
from pathlib import Path


def ssh_config_path() -> Path:
    return Path(os.environ.get("SSH_CONFIG_PATH", "~/.ssh/config")).expanduser()


def load_ssh_hosts(path: Path | None = None) -> list[str]:
    config_path = path or ssh_config_path()
    if not config_path.exists():
        return []

    hosts: list[str] = []
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        parts = line.split()
        if not parts or parts[0].lower() != "host":
            continue
        for host in parts[1:]:
            if "*" not in host and "?" not in host and host not in hosts:
                hosts.append(host)
    return hosts
