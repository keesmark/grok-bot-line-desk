import contextlib
import hashlib
import io
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import sync

TOOL = Path(sync.__file__)

FILES = {
    "VERSION": "0.2.0\n",
    "scripts/b.py": "print('b')\n",
    "scripts/a.py": "print('a')\n",
    "skills/getting-started/SKILL.md":
        "版：Desk for LINE v0.1.0\ncurl https://github.com/x/y/archive/refs/tags/v0.1.0.tar.gz\n",
    "skills/reply/SKILL.md": "# reply\n\n版：Desk for LINE v0.1.0\n",
    "routines/line-webhook.md": "- 版：Desk for LINE v0.1.0。\n",
    "README.md": "curl -o a.tgz https://github.com/x/y/archive/refs/tags/v0.1.0.tar.gz\n",
    "README.en.md": "curl -o a.tgz https://github.com/x/y/archive/refs/tags/v0.1.0.tar.gz\n",
    "CHANGELOG.md": "# changes\n\n## v0.2.0（2026-11-01）\n\n## v0.1.0（2026-10-20）\n",
    "MIGRATIONS.md": "# migrations\n\n## v0.2.0\nなし\n",
}


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name, text in FILES.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_sync(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sync.main(list(argv), root=self.root)
        return code, out.getvalue(), err.getvalue()

    def text(self, name):
        return (self.root / name).read_text(encoding="utf-8")

    def expected_sums(self):
        return "%s  a.py\n%s  b.py\n" % (digest("print('a')\n"), digest("print('b')\n"))

    def test_writes_sums_labels_and_tags(self):
        code, out, err = self.run_sync()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.text("SHA256SUMS"), self.expected_sums())
        self.assertEqual(self.text("skills/reply/SKILL.md"), "# reply\n\n版：Desk for LINE v0.2.0\n")
        self.assertEqual(self.text("routines/line-webhook.md"), "- 版：Desk for LINE v0.2.0。\n")
        self.assertEqual(self.text("skills/getting-started/SKILL.md"),
                         "版：Desk for LINE v0.2.0\ncurl https://github.com/x/y/archive/refs/tags/v0.2.0.tar.gz\n")
        self.assertIn("/archive/refs/tags/v0.2.0.tar.gz", self.text("README.md"))
        self.assertIn("/archive/refs/tags/v0.2.0.tar.gz", self.text("README.en.md"))
        self.assertIn("updated SHA256SUMS", out)
        self.assertIn("tag: v0.2.0", out)

    def test_second_run_changes_nothing(self):
        self.run_sync()
        code, out, _ = self.run_sync()
        self.assertEqual(code, 0)
        self.assertNotIn("updated", out)

    def test_check_passes_when_in_sync(self):
        self.run_sync()
        code, out, err = self.run_sync("--check")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("tag: v0.2.0", out)

    def test_check_reports_stale_files_without_writing(self):
        code, _, err = self.run_sync("--check")
        self.assertEqual(code, 1)
        self.assertIn("SHA256SUMS: out of date", err)
        self.assertIn("README.md: out of date", err)
        self.assertFalse((self.root / "SHA256SUMS").exists())
        self.assertEqual(self.text("README.md"), FILES["README.md"])

    def test_check_detects_changed_script(self):
        self.run_sync()
        (self.root / "scripts/a.py").write_text("print('changed')\n", encoding="utf-8")
        code, _, err = self.run_sync("--check")
        self.assertEqual(code, 1)
        self.assertIn("SHA256SUMS: out of date", err)

    def test_reports_skill_without_label(self):
        (self.root / "skills/new").mkdir()
        (self.root / "skills/new/SKILL.md").write_text("# new\n", encoding="utf-8")
        code, _, err = self.run_sync()
        self.assertEqual(code, 1)
        self.assertIn("skills/new/SKILL.md: no 「版：Desk for LINE v…」 line", err)

    def test_reports_missing_changelog_section(self):
        (self.root / "CHANGELOG.md").write_text("## v0.1.0\n", encoding="utf-8")
        code, _, err = self.run_sync()
        self.assertEqual(code, 1)
        self.assertIn('CHANGELOG.md: no "## v0.2.0" section', err)

    def test_longer_version_does_not_count_as_section(self):
        (self.root / "MIGRATIONS.md").write_text("## v0.2.0.1\n", encoding="utf-8")
        code, _, err = self.run_sync()
        self.assertEqual(code, 1)
        self.assertIn('MIGRATIONS.md: no "## v0.2.0" section', err)

    def test_reports_readme_without_download_url(self):
        (self.root / "README.en.md").write_text("see the releases page\n", encoding="utf-8")
        code, _, err = self.run_sync("--check")
        self.assertEqual(code, 1)
        self.assertIn("README.en.md: no /archive/refs/tags/v….tar.gz URL", err)

    def test_reports_write_failure(self):
        with mock.patch.object(Path, "write_bytes", side_effect=PermissionError("read-only")):
            code, _, err = self.run_sync()
        self.assertEqual(code, 2)
        self.assertIn("read-only", err)

    def test_rejects_malformed_version(self):
        (self.root / "VERSION").write_text("0.2\n", encoding="utf-8")
        code, _, err = self.run_sync()
        self.assertEqual(code, 2)
        self.assertIn("VERSION must look like 1.2.3", err)

    def test_reports_missing_file(self):
        (self.root / "README.en.md").unlink()
        code, _, err = self.run_sync()
        self.assertEqual(code, 2)
        self.assertIn("README.en.md", err)

    def test_keeps_crlf_bytes(self):
        (self.root / "README.md").write_bytes(b"line\r\n/archive/refs/tags/v0.1.0.tar.gz\r\n")
        self.run_sync()
        self.assertEqual((self.root / "README.md").read_bytes(), b"line\r\n/archive/refs/tags/v0.2.0.tar.gz\r\n")

    def test_runs_as_script(self):
        with mock.patch.object(sys, "argv", [str(TOOL), "--check"]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path(str(TOOL), run_name="__main__")
        self.assertIn(raised.exception.code, (0, 1))


if __name__ == "__main__":
    unittest.main()
