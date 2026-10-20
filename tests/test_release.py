import contextlib
import io
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import release

TOOL = Path(release.__file__)


def gpg(home, *args):
    return subprocess.run(["gpg", "--batch", "--homedir", str(home)] + list(args),
                          capture_output=True, text=True, check=True).stdout


def make_key(home, name):
    gpg(home, "--passphrase", "", "--quick-gen-key", name, "ed25519", "sign", "never")
    fpr = next(line.split(":")[9] for line in gpg(home, "--with-colons", "--list-keys").splitlines()
               if line.startswith("fpr:"))
    return fpr, gpg(home, "--armor", "--export", fpr)


def skill_text(fpr, key):
    return "# update\n\n## 署名の鍵\n\n鍵の番号：%s\n\n```text\n%s```\n" % (fpr, key)


class ReleaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.homes = tempfile.TemporaryDirectory()
        base = Path(cls.homes.name)
        cls.signer, cls.other = base / "signer", base / "other"
        for home in (cls.signer, cls.other):
            home.mkdir(mode=0o700)
        cls.fpr, cls.key = make_key(cls.signer, "release test")
        cls.other_fpr, cls.other_key = make_key(cls.other, "other test")

    @classmethod
    def tearDownClass(cls):
        for home in (cls.signer, cls.other):
            subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "all"], capture_output=True)
        cls.homes.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "skills/update").mkdir(parents=True)
        (self.root / "keys").mkdir()
        self.write("skills/update/SKILL.md", skill_text(self.fpr, self.key))
        self.write("keys/release.asc", self.key)
        self.write("SHA256SUMS", "abc  a.py\n")
        for name in ("README.md", "README.en.md"):
            self.write(name, "key %s\n" % self.fpr)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        (self.root / name).write_text(text, encoding="utf-8")

    def sign_with(self, home):
        gpg(home, "--yes", "--armor", "--detach-sign", "--output", str(self.root / "SHA256SUMS.asc"),
            str(self.root / "SHA256SUMS"))

    def run_release(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = release.main(list(argv), root=self.root)
        return code, out.getvalue(), err.getvalue()

    def test_verify_accepts_signature_by_release_key(self):
        self.sign_with(self.signer)
        code, out, err = self.run_release("--verify")
        self.assertEqual(code, 0, err)
        self.assertIn("ok, SHA256SUMS.asc is signed by the release key", out)

    def test_verify_rejects_changed_sums(self):
        self.sign_with(self.signer)
        self.write("SHA256SUMS", "def  a.py\n")
        code, _, err = self.run_release("--verify")
        self.assertEqual(code, 1)
        self.assertIn("SHA256SUMS.asc: not a good signature", err)

    def test_verify_rejects_signature_by_other_key(self):
        self.sign_with(self.other)
        code, _, err = self.run_release("--verify")
        self.assertEqual(code, 1)
        self.assertIn("SHA256SUMS.asc: not a good signature", err)

    def test_verify_rejects_missing_signature(self):
        code, _, err = self.run_release("--verify")
        self.assertEqual(code, 1)
        self.assertIn("SHA256SUMS.asc: not a good signature", err)

    def test_verify_rejects_skill_key_that_differs(self):
        self.sign_with(self.signer)
        self.write("skills/update/SKILL.md", skill_text(self.fpr, self.other_key))
        code, _, err = self.run_release("--verify")
        self.assertEqual(code, 1)
        self.assertIn("key block differs from keys/release.asc", err)

    def test_verify_rejects_skill_without_key_block(self):
        self.sign_with(self.signer)
        self.write("skills/update/SKILL.md", "鍵の番号：%s\n" % self.fpr)
        code, _, err = self.run_release("--verify")
        self.assertEqual(code, 1)
        self.assertIn("no public key block", err)

    def test_verify_rejects_skill_without_fingerprint(self):
        self.write("skills/update/SKILL.md", "```text\n%s```\n" % self.key)
        code, _, err = self.run_release("--verify")
        self.assertEqual(code, 1)
        self.assertIn("no 「鍵の番号：<40 hex>」 line", err)

    def test_verify_rejects_fingerprint_of_another_key(self):
        self.sign_with(self.signer)
        self.write("skills/update/SKILL.md", skill_text(self.other_fpr, self.key))
        code, _, err = self.run_release("--verify")
        self.assertEqual(code, 1)
        self.assertIn("are not just 鍵の番号 %s" % self.other_fpr, err)

    def test_verify_requires_fingerprint_in_readmes(self):
        self.sign_with(self.signer)
        self.write("README.en.md", "no key\n")
        code, _, err = self.run_release("--verify")
        self.assertEqual(code, 1)
        self.assertIn("README.en.md: does not mention 鍵の番号 %s" % self.fpr, err)

    def test_verify_reports_unreadable_key(self):
        (self.root / "keys/release.asc").unlink()
        code, _, err = self.run_release("--verify")
        self.assertEqual(code, 2)
        self.assertIn("release.asc", err)

    def test_sign_writes_good_signature(self):
        with mock.patch.object(release.sync, "main", return_value=0) as synced, \
                mock.patch.dict(os.environ, {"GNUPGHOME": str(self.signer)}):
            code, out, err = self.run_release()
        self.assertEqual(code, 0, err)
        synced.assert_called_once_with([], root=self.root)
        self.assertTrue((self.root / "SHA256SUMS.asc").exists())
        self.assertIn("ok", out)

    def test_sign_skips_when_signature_is_already_good(self):
        self.sign_with(self.signer)
        before = (self.root / "SHA256SUMS.asc").read_bytes()
        with mock.patch.object(release.sync, "main", return_value=0), \
                mock.patch.object(release.subprocess, "run", wraps=subprocess.run) as ran:
            code, out, _ = self.run_release()
        self.assertEqual(code, 0)
        self.assertIn("not signing again", out)
        self.assertEqual((self.root / "SHA256SUMS.asc").read_bytes(), before)
        self.assertFalse(any("--detach-sign" in call.args[0] for call in ran.call_args_list))

    def test_sign_stops_when_sync_fails(self):
        with mock.patch.object(release.sync, "main", return_value=1):
            code, _, err = self.run_release()
        self.assertEqual(code, 1)
        self.assertIn("signing failed", err)
        self.assertFalse((self.root / "SHA256SUMS.asc").exists())

    def test_sign_stops_without_fingerprint(self):
        self.write("skills/update/SKILL.md", "no key here\n")
        with mock.patch.object(release.sync, "main", return_value=0):
            code, _, err = self.run_release()
        self.assertEqual(code, 1)
        self.assertIn("signing failed", err)

    def test_sign_stops_when_gpg_fails(self):
        with mock.patch.object(release.sync, "main", return_value=0), \
                mock.patch.dict(os.environ, {"GNUPGHOME": str(self.other)}), \
                contextlib.redirect_stderr(io.StringIO()):
            code, _, err = self.run_release()
        self.assertEqual(code, 1)
        self.assertIn("signing failed", err)

    def test_runs_as_script(self):
        with mock.patch.object(sys, "argv", [str(TOOL), "--verify"]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path(str(TOOL), run_name="__main__")
        self.assertIn(raised.exception.code, (0, 1))


if __name__ == "__main__":
    unittest.main()
