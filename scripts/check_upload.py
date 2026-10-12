"""Check Git blobs before publication; never print matched private values."""
import argparse
import ipaddress
from pathlib import PurePosixPath
import re
import subprocess
import sys


ROOT_FILES = {
    ".gitignore", ".gitattributes", ".env.example", "README.md", "LICENSE",
    "cli.py", "pyproject.toml", "requirements.txt", "eval/testcases.json",
    ".githooks/pre-commit", ".githooks/pre-push",
}
TOKEN = re.compile(r"\b(?:gh[opsu]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})\b")
PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")
ASSIGNMENT = re.compile(
    r"(?i)\b[A-Z0-9_]*(?:API[_-]?KEY|TOKEN|PASSWORD|PASSWD|SECRET|COOKIE|DATABASE_URL|DB_URL)(?:_[A-Z0-9]+)*"
    r"[\"']?\s*[:=]\s*([fr]?\"[^\"\n]*\"|[fr]?'[^'\n]*'|\{[A-Za-z_]\w*\}|[^\s,;}]+)"
)
AUTH = re.compile(r"(?i)\bBearer\s+([^\s\"']+)|https?://[^\s/:@]+:([^\s/@]+)@")
HOME = re.compile(r"(?:[A-Za-z]:[/\\]+Users[/\\]+|/home/|/Users/)([\w.-]+)")
IDENTITY = re.compile(r"(?:学号|身份证|手机号|姓名)\s*[:：=]\s*[\"']?[^\s\"',}]+")
ADDRESS = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def placeholder(value):
    if len(value) > 1 and value[0].lower() in {"f", "r"} and value[1] in "\"'":
        value = value[1:]
    value = value.strip("\"'")
    return (value.lower() in {"", "=", "ollama", "dummy", "example", "placeholder", "sk-",
                              "test-key", "test-password", "test-placeholder"}
            or value.lower().startswith(("your_", "你的"))
            or re.fullmatch(r"<REDACTED(?:_[A-Z]+)*>", value, re.IGNORECASE) is not None
            or re.fullmatch(r"(?:\{[A-Za-z_]\w*\}|\\n)+", value) is not None)


def allowed_path(name):
    path = PurePosixPath(name)
    if name in ROOT_FILES:
        return True
    if path.parent.as_posix() in {"core", "tests", "eval", "scripts"}:
        return path.suffix == ".py"
    return path.parent.as_posix() == ".github/workflows" and path.suffix in {".yml", ".yaml"}


def inspect_blob(name, mode, content):
    if not allowed_path(name):
        return ["file outside publication allowlist"]
    if mode not in {"100644", "100755"}:
        return ["links and submodules cannot be published"]
    if len(content) > 1024 * 1024 or content.startswith((b"%PDF-", b"PK\x03\x04", b"\xd0\xcf\x11\xe0")):
        return ["binary document or oversized file"]
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ["file must contain UTF-8 text"]
    if "\0" in text:
        return ["binary content"]
    if re.search(r"(?m)^\s*\\(?:documentclass|begin\{document\})", text):
        return ["LaTeX document disguised as code"]
    if re.search(r"(?m)^#{1,6}\s+(?:[一二三四五]、)?(?:开题报告|主要参考文献|研究工作进度)", text):
        return ["academic document heading"]
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        reasons = []
        if TOKEN.search(line) or PRIVATE_KEY.search(line):
            reasons.append("credential or private key")
        if any(not placeholder(m[1]) and not (name.endswith(".py") and
               re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?:\(.*)?",
                            m[1].rstrip(")]"))) for m in ASSIGNMENT.finditer(line)):
            reasons.append("literal credential assignment")
        if any(not placeholder(m[1] or m[2]) for m in AUTH.finditer(line)):
            reasons.append("authorization or URL credential")
        if any(m[1] not in {"user", "user1", "demo", "example"} and not placeholder(m[1])
               for m in HOME.finditer(line)):
            reasons.append("personal home path")
        if IDENTITY.search(line):
            reasons.append("personal identity field")
        for match in ADDRESS.finditer(line):
            if any(line[match.start():].startswith(network) for network in
                   ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
                continue
            try:
                address = ipaddress.ip_address(match[0])
            except ValueError:
                continue
            if any(address in ipaddress.ip_network(network) for network in
                   ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
                reasons.append("private network address")
                break
        if reasons:
            findings.append(f"line {number}: {', '.join(reasons)}")
    return findings


def git(*args):
    result = subprocess.run(["git", *args], capture_output=True, check=True)
    return result.stdout


def entries(revision=None):
    raw = git("ls-tree", "-rz", revision) if revision else git("ls-files", "--stage", "-z")
    result = {}
    for item in raw.split(b"\0"):
        if not item:
            continue
        info, path = item.split(b"\t", 1)
        mode, middle, last = info.decode().split()
        if revision:
            oid = last
        else:
            if last != "0":
                raise ValueError("Resolve index conflicts before checking publication")
            oid = middle
        result[path.decode("utf-8")] = (mode, oid)
    return result


def check(mode="staged", revisions=None):
    snapshots = []
    if mode == "commits":
        commits = git("rev-list", "--reverse", *revisions, "--").decode().splitlines()
        for commit in commits:
            names = git("diff-tree", "--root", "-m", "--no-commit-id", "--name-only",
                        "--diff-filter=ACMR", "-r", "-z", commit).split(b"\0")
            tree = entries(commit)
            snapshots.extend((name.decode("utf-8"), tree) for name in set(names)
                             if name and name.decode("utf-8") in tree)
    else:
        tree = entries()
        names = tree if mode == "tracked" else [p.decode("utf-8") for p in
                git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split(b"\0") if p]
        snapshots = [(name, tree) for name in names]
    failures = []
    seen = set()
    for name, tree in snapshots:
        file_mode, oid = tree[name]
        if (name, oid) in seen:
            continue
        seen.add((name, oid))
        if not allowed_path(name) or file_mode not in {"100644", "100755"}:
            failures.extend(f"{name}: {reason}" for reason in inspect_blob(name, file_mode, b""))
            continue
        if int(git("cat-file", "-s", oid)) > 1024 * 1024:
            failures.append(f"{name}: oversized file")
            continue
        failures.extend(f"{name}: {reason}" for reason in
                        inspect_blob(name, file_mode, git("cat-file", "blob", oid)))
    return failures, len(seen)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--tracked", action="store_true", help="check the complete Git index")
    group.add_argument("--commits", nargs="+", help="check changed blobs in each new commit")
    args = parser.parse_args()
    try:
        failures, count = check("commits" if args.commits else "tracked" if args.tracked else "staged",
                                args.commits)
    except (subprocess.CalledProcessError, ValueError, KeyError, UnicodeError):
        print("Upload check failed: Git data could not be checked. Nothing is approved.", file=sys.stderr)
        return 1
    if failures:
        print("Upload blocked:\n" + "\n".join(failures), file=sys.stderr)
        return 1
    print(f"Upload check passed: {count} Git blobs checked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
