"""Publication checks run without third-party packages or a real network."""
from pathlib import Path
from contextlib import redirect_stderr
from io import StringIO
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import check_upload
from scripts.check_upload import check, inspect_blob, main


class UploadGuardTest(unittest.TestCase):
    def test_rejects_documents_results_and_links(self):
        for name in ["report.tex", "report.pdf", "notes.md", ".env", "eval/result.json",
                     "core/paper.docx", "docs/guide.md"]:
            self.assertTrue(inspect_blob(name, "100644", b"safe text"), name)
        self.assertTrue(inspect_blob("core/link.py", "120000", b"../outside"))
        self.assertTrue(inspect_blob("core/renamed.py", "100644", b"%PDF-1.7"))
        latex = "\\document" + "class{ctexart}"
        self.assertTrue(inspect_blob("core/renamed.py", "100644", latex.encode()))
        self.assertTrue(inspect_blob("README.md", "100644", "# 开题报告".encode()))

    def test_rejects_private_values_without_echoing_them(self):
        token = "sk-" + "a" * 32
        examples = [token, "=".join(["TOKEN", "sensitive-value"]), "Bearer " + "private-token",
                    "https://" + "person:private-password@host.example/path",
                    "C:/Users/" + "private-person/config", "姓名：" + "测试人员",
                    ".".join(map(str, [172, 18, 0, 1]))]
        for value in examples:
            findings = inspect_blob("core/example.py", "100644", value.encode())
            self.assertTrue(findings, value)
            self.assertNotIn(value, "\n".join(findings))
        key = "-----BEGIN " + "PRIVATE KEY-----"
        self.assertTrue(inspect_blob("core/example.py", "100644", key.encode()))

    def test_allows_code_and_placeholder_configuration(self):
        for name, text in [("core/example.py", "print('hello')"),
                           (".env.example", "API_KEY=your_api_key_here\nURL=http://localhost:11434"),
                           ("tests/example.py", 'token = "sk-" + "a" * 32'),
                           ("README.md", "/home/user/project")]:
            self.assertEqual(inspect_blob(name, "100644", text.encode()), [], name)

    def test_password_punctuation_is_not_a_placeholder(self):
        for value in ["S3cr(et!", "my<password", "literal{value}"]:
            text = "=".join(["PASSWORD", repr(value)])
            self.assertTrue(inspect_blob(".env.example", "100644", text.encode()))

    def test_rejects_large_blob_without_reading_content(self):
        calls = []

        def fake_git(*args):
            calls.append(args)
            if args[0] == "ls-files":
                return b"100644 abc 0\tcore/example.py\0"
            if args == ("cat-file", "-s", "abc"):
                return str(1024 * 1024 + 1).encode()
            raise AssertionError("Large blob content must not be read")

        with patch("scripts.check_upload.git", side_effect=fake_git):
            failures, count = check("tracked")
        self.assertEqual(count, 1)
        self.assertIn("oversized file", failures[0])
        self.assertNotIn(("cat-file", "blob", "abc"), calls)

    def test_reads_staged_blob_and_deleted_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.git(root, "init")
            (root / "core").mkdir()
            file = root / "core/example.py"
            file.write_text("=".join(["TOKEN", "sensitive-value"]), encoding="utf-8")
            self.git(root, "add", "core/example.py")
            file.write_text("print('safe working tree')", encoding="utf-8")
            with patch("scripts.check_upload.git", side_effect=lambda *a: self.git(root, *a)):
                failures, count = check()
                self.assertEqual(count, 1)
                self.assertTrue(failures)
            self.git(root, "rm", "--cached", "-f", "core/example.py")
            with patch("scripts.check_upload.git", side_effect=lambda *a: self.git(root, *a)):
                self.assertEqual(check(), ([], 0))

    def test_detects_secret_in_intermediate_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.git(root, "init")
            self.git(root, "config", "user.name", "Test User")
            self.git(root, "config", "user.email", "test@example.invalid")
            (root / "README.md").write_text("example", encoding="utf-8")
            self.git(root, "add", "README.md")
            self.git(root, "commit", "-m", "baseline")
            base = self.git(root, "rev-parse", "HEAD").decode().strip()
            self.publish(root, Path(temp) / "remote.git")
            (root / "core").mkdir()
            file = root / "core/example.py"
            file.write_text("=".join(["TOKEN", "sensitive-value"]), encoding="utf-8")
            self.git(root, "add", "core/example.py")
            self.git(root, "commit", "-m", "unsafe")
            file.write_text("print('safe')", encoding="utf-8")
            self.git(root, "add", "core/example.py")
            self.git(root, "commit", "-m", "safe")
            with patch("scripts.check_upload.git", side_effect=lambda *a, **kw: self.git(root, *a, **kw)):
                self.assertTrue(check("commits", [base + "..HEAD"])[0])

    def test_new_branch_checks_all_six_repair_files_not_published_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.repository(Path(temp) / "local")
            self.commit_file(root, "notes.md", "published document")
            self.publish(root, Path(temp) / "remote.git")
            self.git(root, "checkout", "-b", "repair")
            names = ["core/intent.py", "core/engine.py", "tests/test_intent.py",
                     "tests/test_engine.py", "tests/test_bash_queries.py", "tests/test_cli_display.py"]
            for name in names:
                file = root / name
                file.parent.mkdir(exist_ok=True)
                file.write_text("print('safe')", encoding="utf-8")
            self.git(root, "add", "--", *names)
            self.git(root, "commit", "-m", "repair")
            with patch("scripts.check_upload.git", side_effect=lambda *a, **kw: self.git(root, *a, **kw)), \
                    patch("scripts.check_upload.inspect_blob", wraps=inspect_blob) as inspect:
                self.assertEqual(check("commits", ["HEAD"]), ([], 6))
            self.assertEqual({call.args[0] for call in inspect.call_args_list}, set(names))

    def test_multiple_unpublished_commits_keep_order_and_blob_deduplication(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.repository(Path(temp) / "local")
            self.publish(root, Path(temp) / "remote.git")
            first = self.commit_file(root, "core/example.py", "print('first')")
            second = self.commit_file(root, "core/example.py", "print('second')")
            third = self.commit_file(root, "core/example.py", "print('first')")
            with patch("scripts.check_upload.git", side_effect=lambda *a, **kw: self.git(root, *a, **kw)), \
                    patch("scripts.check_upload.entries", wraps=check_upload.entries) as entries:
                self.assertEqual(check("commits", ["HEAD"]), ([], 2))
            self.assertEqual([call.args[0] for call in entries.call_args_list], [first, second, third])

    def test_live_other_branch_overrides_stale_tracking_refs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.repository(Path(temp) / "local")
            base = self.git(root, "rev-parse", "HEAD").decode().strip()
            published = self.commit_file(root, "notes.md", "already published")
            remote = Path(temp) / "remote.git"
            self.publish(root, remote)
            self.git(remote, "update-ref", "refs/heads/main", base)
            self.git(remote, "update-ref", "refs/heads/published", published)
            self.git(root, "update-ref", "refs/remotes/origin/published", base)
            current = self.commit_file(root, "core/example.py", "print('new')")
            with patch("scripts.check_upload.git", side_effect=lambda *a, **kw: self.git(root, *a, **kw)), \
                    patch("scripts.check_upload.entries", wraps=check_upload.entries) as entries:
                self.assertEqual(check("commits", ["HEAD"]), ([], 1))
            self.assertEqual([call.args[0] for call in entries.call_args_list], [current])

    def test_stale_tracking_ref_cannot_hide_an_unpublished_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.repository(Path(temp) / "local")
            self.publish(root, Path(temp) / "remote.git")
            current = self.commit_file(root, "core/example.py", "=".join(["TOKEN", "sensitive-value"]))
            self.git(root, "update-ref", "refs/remotes/origin/deleted", current)
            with patch("scripts.check_upload.git", side_effect=lambda *a, **kw: self.git(root, *a, **kw)):
                failures, count = check("commits", ["HEAD"])
            self.assertEqual(count, 1)
            self.assertTrue(failures)

    def test_unavailable_remote_rejects_before_scanning(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.repository(Path(temp) / "local")
            self.git(root, "remote", "add", "origin", str(Path(temp) / "missing.git"))
            self.assert_publication_stops(root)

    def test_missing_live_remote_commit_rejects_before_scanning(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.repository(Path(temp) / "local")
            other = self.repository(Path(temp) / "other")
            self.commit_file(other, "README.md", "different remote history")
            remote = Path(temp) / "remote.git"
            self.git(other, "clone", "--bare", str(other), str(remote))
            self.git(root, "remote", "add", "origin", str(remote))
            self.assert_publication_stops(root)

    def test_remote_timeout_and_malformed_refs_reject(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.repository(Path(temp) / "local")
            self.publish(root, Path(temp) / "remote.git")
            for result in [subprocess.TimeoutExpired("git ls-remote", 30), b"invalid refs/heads/main\n"]:
                with self.subTest(result=type(result).__name__):
                    def run(*args, **kwargs):
                        if args[0] == "ls-remote":
                            if isinstance(result, Exception):
                                raise result
                            return result
                        return self.git(root, *args, **kwargs)
                    self.assert_publication_stops(root, run)

    def test_ambiguous_remotes_reject(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.repository(Path(temp) / "local")
            self.publish(root, Path(temp) / "remote.git")
            self.git(root, "remote", "add", "other", str(Path(temp) / "other.git"))
            self.assert_publication_stops(root)

    def test_multiple_push_destinations_reject(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.repository(Path(temp) / "local")
            remote = Path(temp) / "remote.git"
            self.publish(root, remote)
            self.git(root, "config", "--add", "remote.origin.pushurl", str(remote))
            self.git(root, "config", "--add", "remote.origin.pushurl", str(Path(temp) / "other.git"))
            self.assert_publication_stops(root)

    def test_published_tip_has_no_files_to_rescan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.repository(Path(temp) / "local")
            self.commit_file(root, "notes.md", "published document")
            self.publish(root, Path(temp) / "remote.git")
            with patch("scripts.check_upload.git", side_effect=lambda *a, **kw: self.git(root, *a, **kw)), \
                    patch("scripts.check_upload.entries") as entries:
                self.assertEqual(check("commits", ["HEAD"]), ([], 0))
            entries.assert_not_called()

    def test_empty_remote_scans_all_local_commits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.repository(Path(temp) / "local")
            remote = Path(temp) / "remote.git"
            self.git(root, "init", "--bare", str(remote))
            self.git(root, "remote", "add", "origin", str(remote))
            with patch("scripts.check_upload.git", side_effect=lambda *a, **kw: self.git(root, *a, **kw)):
                self.assertEqual(check("commits", ["HEAD"]), ([], 1))

    def test_unpublished_rename_and_copy_scan_full_content(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.repository(Path(temp) / "local")
            content = "=".join(["TOKEN", "sensitive-value"])
            self.commit_file(root, "core/source.py", content)
            self.publish(root, Path(temp) / "remote.git")
            self.git(root, "mv", "core/source.py", "core/renamed.py")
            (root / "core/copied.py").write_text(content, encoding="utf-8")
            self.git(root, "add", "core/copied.py")
            self.git(root, "commit", "-m", "rename and copy")
            with patch("scripts.check_upload.git", side_effect=lambda *a, **kw: self.git(root, *a, **kw)):
                failures, count = check("commits", ["HEAD"])
            self.assertEqual(count, 2)
            self.assertEqual({item.split(":", 1)[0] for item in failures}, {"core/renamed.py", "core/copied.py"})

    def test_unpublished_files_keep_secret_path_type_and_size_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.repository(Path(temp) / "local")
            self.publish(root, Path(temp) / "remote.git")
            files = {"notes.md": b"safe text", "core/document.py": b"%PDF-1.7",
                     "core/large.py": b"x" * (1024 * 1024 + 1), "core/binary.py": b"text\0",
                     "core/encoding.py": b"\xff", "core/credential.py": ("sk-" + "z" * 32).encode()}
            for name, content in files.items():
                file = root / name
                file.parent.mkdir(exist_ok=True)
                file.write_bytes(content)
            self.git(root, "add", "--", *files)
            self.git(root, "commit", "-m", "unsafe fixtures")
            with patch("scripts.check_upload.git", side_effect=lambda *a, **kw: self.git(root, *a, **kw)):
                failures, count = check("commits", ["HEAD"])
            self.assertEqual(count, len(files))
            self.assertEqual({item.split(":", 1)[0] for item in failures}, set(files))
            self.assertNotIn("sk-" + "z" * 32, "\n".join(failures))

    def assert_publication_stops(self, root, run=None):
        error = StringIO()
        with patch("scripts.check_upload.git", side_effect=run or (lambda *a, **kw: self.git(root, *a, **kw))), \
                patch("scripts.check_upload.entries") as entries, \
                patch("sys.argv", ["check_upload.py", "--commits", "HEAD"]), redirect_stderr(error):
            self.assertEqual(main(), 1)
        entries.assert_not_called()
        self.assertIn("Nothing is approved", error.getvalue())

    def repository(self, root):
        root.mkdir()
        self.git(root, "init", "-b", "main")
        self.git(root, "config", "user.name", "Test User")
        self.git(root, "config", "user.email", "test@example.invalid")
        self.commit_file(root, "README.md", "example")
        return root

    def commit_file(self, root, name, content):
        file = root / name
        file.parent.mkdir(exist_ok=True)
        file.write_text(content, encoding="utf-8")
        self.git(root, "add", "--", name)
        self.git(root, "commit", "-m", "fixture")
        return self.git(root, "rev-parse", "HEAD").decode().strip()

    def publish(self, root, remote):
        self.git(root, "clone", "--bare", str(root), str(remote))
        self.git(root, "remote", "add", "origin", str(remote))

    @staticmethod
    def git(root, *args, timeout=None):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              check=True, timeout=timeout).stdout


if __name__ == "__main__":
    unittest.main()
