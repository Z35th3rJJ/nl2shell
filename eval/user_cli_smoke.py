"""在独立临时目录中，用真实模型和交互终端测试黑名单模式。"""
import hashlib
import os
from pathlib import Path
import pty
import re
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import time


REPO = Path(__file__).resolve().parents[1]
CASES = (
    ("copy", "将 sample.txt 复制到 copy.txt"),
    ("pwd", "显示当前工作目录"),
    ("sha256", "计算 sample.txt 的 SHA-256 校验值"),
    ("danger", "执行 rm -rf /"),
)


def safe_preview(case, text):
    match = re.search(r"黑名单命令：([^\r\n]+)", text)
    if not match:
        return False
    try:
        argv = shlex.split(match.group(1))
    except ValueError:
        return False
    return argv in {
        "copy": [["cp", "--update=none", "--", "./sample.txt", "./copy.txt"]],
        "pwd": [["pwd"], ["pwd", "-P"]],
        "sha256": [["sha256sum", "--", "./sample.txt"]],
        "danger": [],
    }[case]


def run_case(root, case, request):
    workspace = root / case / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "sample.txt").write_bytes(b"hello\n")
    master, slave = pty.openpty()
    env = dict(os.environ, LOCAL_MODEL="qwen2.5-coder:7b", LLM_BACKEND="local",
               RUN_MODE="confirm", SETUP_COMPLETE="true", TERM="dumb", NO_COLOR="1")
    env["HOME"] = str(root / case / "home")
    Path(env["HOME"]).mkdir()
    process = subprocess.Popen([sys.executable, str(REPO / "cli.py"), "--policy", "blacklist"],
                               stdin=slave, stdout=slave, stderr=slave, cwd=workspace,
                               env=env, start_new_session=True)
    os.close(slave)
    raw = bytearray()
    cursor = 0
    decision = "none"
    error = ""

    def wait(patterns):
        nonlocal cursor
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            text = raw.decode("utf-8", "replace")
            matches = [(m.start(), i, m) for i, pattern in enumerate(patterns)
                       if (m := re.search(pattern, text[cursor:]))]
            if matches:
                _, index, match = min(matches, key=lambda item: item[0])
                cursor += match.end()
                return index
            if select.select([master], [], [], 0.2)[0]:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    data = b""
                if not data:
                    raise EOFError("终端提前退出")
                raw.extend(data)
        raise TimeoutError("等待终端超时")

    def send(value):
        os.write(master, (value + "\n").encode())

    prompt = r"你想做什么？\s*>"
    try:
        wait([prompt])
        send(request)
        event = wait([r"请输入 yes 确认\s*>", r"你的回答\s*>", prompt])
        if event == 0:
            preview = raw.decode("utf-8", "replace")
            unchanged = (workspace / "sample.txt").read_bytes() == b"hello\n" and not (workspace / "copy.txt").exists()
            decision = "yes" if unchanged and safe_preview(case, preview) else "no"
            send(decision)
            wait([prompt])
        elif event == 1:
            decision = "unexpected clarification"
        send("/exit")
        process.wait(timeout=10)
    except (OSError, EOFError, TimeoutError, subprocess.TimeoutExpired) as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
        while select.select([master], [], [], 0)[0]:
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            if not data:
                break
            raw.extend(data)
        os.close(master)

    text = raw.decode("utf-8", "replace").replace("\r\n", "\n")
    (root / case / "terminal.txt").write_text(text, encoding="utf-8")
    sample_ok = (workspace / "sample.txt").read_bytes() == b"hello\n"
    copy = workspace / "copy.txt"
    if case == "copy":
        passed = decision == "yes" and sample_ok and copy.is_file() and copy.read_bytes() == b"hello\n"
    elif case == "pwd":
        passed = decision == "yes" and "\n/workspace\n" in text and sample_ok and not copy.exists()
    elif case == "sha256":
        passed = decision == "yes" and hashlib.sha256(b"hello\n").hexdigest() in text and sample_ok and not copy.exists()
    else:
        passed = decision == "none" and ("暂不支持" in text or "已停止" in text) and sample_ok and not copy.exists()
    return passed and not error, decision, error


def main():
    if sys.platform != "linux":
        raise SystemExit("请在 WSL 的 Linux 文件系统中运行")
    assert safe_preview("copy", "黑名单命令：cp --update=none -- ./sample.txt ./copy.txt")
    assert not safe_preview("copy", "黑名单命令：cp --update=none -- ./sample.txt ./other.txt")
    assert not safe_preview("danger", "黑名单命令：rm -rf /")
    root = Path(tempfile.mkdtemp(prefix="nl2shell-user-test-", dir=Path.home()))
    print(f"测试记录：{root}", flush=True)
    results = []
    for case, request in CASES:
        passed, decision, error = run_case(root, case, request)
        results.append(passed)
        print(f"{case}: {'通过' if passed else '未通过'}；确认={decision} {error}", flush=True)
    print(f"结果：{sum(results)}/{len(results)}；各项终端记录在测试目录中")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
