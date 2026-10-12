"""Publication checks run without third-party packages or a real network."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.check_upload import check, inspect_blob


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
            (root / "core").mkdir()
            file = root / "core/example.py"
            file.write_text("=".join(["TOKEN", "sensitive-value"]), encoding="utf-8")
            self.git(root, "add", "core/example.py")
            self.git(root, "commit", "-m", "unsafe")
            file.write_text("print('safe')", encoding="utf-8")
            self.git(root, "add", "core/example.py")
            self.git(root, "commit", "-m", "safe")
            with patch("scripts.check_upload.git", side_effect=lambda *a: self.git(root, *a)):
                self.assertTrue(check("commits", [base + "..HEAD"])[0])

    @staticmethod
    def git(root, *args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              check=True).stdout


if __name__ == "__main__":
    unittest.main()
