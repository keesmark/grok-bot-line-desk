import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.dont_write_bytecode = True
SCRIPTS = os.environ.get("SCRIPTS") or os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts"))
LIVE_DB = "/workspace/line-inbox/messages.db"
TOKEN = "test-token-SECRET123"
API = "https://api.line.me"
EMOJI = "\U0001F600"


def live_db_stat():
    try:
        st = os.stat(LIVE_DB)
    except FileNotFoundError:
        return None
    return st.st_mtime_ns, st.st_size


LIVE_BEFORE = live_db_stat()


def load(name):
    path = os.path.join(SCRIPTS, name + ".py")
    spec = importlib.util.spec_from_file_location("desk_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeResp:
    def __init__(self, data):
        self.body = json.dumps(data).encode("utf-8")

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_line_api(limit, used, calls):
    def urlopen(req, timeout=None, **kw):
        calls.append((req.full_url, req.get_header("Authorization")))
        if req.full_url == API + "/v2/bot/message/quota":
            return FakeResp({"type": "limited", "value": limit})
        if req.full_url == API + "/v2/bot/message/quota/consumption":
            return FakeResp({"totalUsage": used})
        raise AssertionError("unexpected URL " + req.full_url)
    return urlopen


class DeskTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.db = os.path.join(self.tmp, "messages.db")
        env = mock.patch.dict(os.environ, {"DESK_DB": self.db,
                                           "LINE_CHANNEL_ACCESS_TOKEN": TOKEN})
        env.start()
        self.addCleanup(env.stop)
        code, out, err = self.script("init_db")
        self.assertEqual(code, 0, err)
        self.assertEqual(os.stat(self.db).st_mode & 0o777, 0o600)

    def script(self, name, *argv, stdin=None, urlopen=None):
        db = os.environ.get("DESK_DB", "")
        self.assertTrue(db, "DESK_DB must be set")
        self.assertNotEqual(os.path.realpath(db), os.path.realpath(LIVE_DB))
        mod = load(name)
        net = getattr(getattr(mod, "urllib", None), "request", urllib.request)
        guard = mock.Mock(side_effect=AssertionError("network call"))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(net, "urlopen", urlopen or guard))
            if stdin is not None:
                stack.enter_context(mock.patch.object(sys, "stdin", io.TextIOWrapper(
                    io.BytesIO(stdin.encode("utf-8")), encoding="utf-8")))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            try:
                code = mod.main(list(argv))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        out, err = out.getvalue(), err.getvalue()
        self.assertNotIn(TOKEN, out + err, "token leaked by %s" % name)
        self.assertFalse(guard.called, "%s used the network" % name)
        return (0 if code is None else code), out, err


class QuotaPeekTest(DeskTest):
    def test_peek_is_alias_of_no_update(self):
        state = os.path.join(self.tmp, "quota_state.json")
        calls = []
        api = fake_line_api(1000, 990, calls)
        code, peek_out, err = self.script("quota_check", "--peek", urlopen=api)
        self.assertEqual(code, 0, err)
        self.assertIn("remaining: 10", peek_out)
        self.assertFalse(os.path.exists(state), "--peek wrote the state file")
        code, no_update_out, err = self.script("quota_check", "--no-update", urlopen=api)
        self.assertEqual(code, 0, err)
        self.assertEqual(peek_out, no_update_out)
        code, out, err = self.script("quota_check", "--peek", "--json", urlopen=api)
        self.assertEqual(code, 0, err)
        r = json.loads(out)
        self.assertEqual((r["limit"], r["used"], r["remaining"]), (1000, 990, 10))
        self.assertIs(r["notice_due"], True)
        self.assertIs(r["state_saved"], False)
        self.assertFalse(os.path.exists(state))
        self.assertEqual({u for u, _ in calls},
                         {API + "/v2/bot/message/quota",
                          API + "/v2/bot/message/quota/consumption"})
        self.assertTrue(all(h == "Bearer " + TOKEN for _, h in calls))
        code, out, err = self.script("quota_check", "--json", urlopen=api)
        self.assertEqual(code, 0, err)
        self.assertIs(json.loads(out)["state_saved"], True)
        self.assertEqual(os.stat(state).st_mode & 0o777, 0o600)
        with open(state, "rb") as f:
            saved = f.read()
        before = os.stat(state).st_mtime_ns
        code, out, err = self.script("quota_check", "--peek", "--json",
                                     urlopen=fake_line_api(1000, 999, []))
        self.assertEqual(code, 0, err)
        r = json.loads(out)
        self.assertEqual(r["remaining"], 1)
        self.assertIs(r["state_saved"], False)
        with open(state, "rb") as f:
            self.assertEqual(f.read(), saved)
        self.assertEqual(os.stat(state).st_mtime_ns, before)

    def test_peek_http_error_hides_token(self):
        def denied(req, timeout=None, **kw):
            raise urllib.error.HTTPError(
                req.full_url, 401, "Unauthorized", {},
                io.BytesIO(("invalid token " + TOKEN).encode("utf-8")))
        code, out, err = self.script("quota_check", "--peek", urlopen=denied)
        self.assertNotEqual(code, 0)
        self.assertIn("401", err)
        self.assertEqual(out, "")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "quota_state.json")))


class BroadcastPrepTest(DeskTest):
    def prep(self, text, *flags):
        path = os.path.join(self.tmp, "draft.txt")
        with open(path, "wb") as f:
            f.write(text.encode("utf-8"))
        return self.script("broadcast_prep", "--text-file", path, *flags)

    def remaining_line(self, out):
        lines = [line for line in out.splitlines() if "今月の残り" in line]
        self.assertEqual(len(lines), 1, out)
        return lines[0]

    def test_ok_summary(self):
        text = "春のセールのお知らせ\n全品10%オフ（3/31まで）\n\nご来店お待ちしています。\n"
        code, out, err = self.prep(text, "--remaining", "120")
        self.assertEqual(code, 0, err)
        self.assertIn(text, out)
        self.assertIn("友だち全員（人数は管理画面で確認）", out)
        self.assertIn("友だちの人数分", out)
        self.assertIn("120", self.remaining_line(out))
        self.assertIn("送信", out)
        self.assertNotIn("error", err)
        code, out, err = self.prep(text)
        self.assertEqual(code, 0, err)
        self.assertIn("不明", self.remaining_line(out))
        self.assertNotIn("120", self.remaining_line(out))
        code, out, err = self.script("broadcast_prep", "--text", "-", stdin=text)
        self.assertEqual(code, 0, err)
        self.assertIn(text, out)
        code, out, err = self.prep(text, "--remaining", "-1")
        self.assertEqual(code, 2)

    def test_empty_and_too_long_exit_2(self):
        for text in ("", " \n\n\t ", "あ" * 5001):
            with self.subTest(chars=len(text)):
                code, out, err = self.prep(text)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("error", err)
        code, out, err = self.prep("あ" * 5000)
        self.assertEqual(code, 0, err)

    def test_emoji_over_5000_utf16_units_exit_2(self):
        code, out, err = self.prep(EMOJI * 2501)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("error", err)
        code, out, err = self.prep(EMOJI * 2501, "--json")
        self.assertEqual(code, 2)
        r = json.loads(out)
        self.assertEqual((r["ok"], r["chars"], r["utf16_units"]), (False, 2501, 5002))
        code, out, err = self.prep(EMOJI * 2500)
        self.assertEqual(code, 0, err)

    def test_unfilled_placeholders_exit_3(self):
        code, out, err = self.prep("{名前}様、{日付}にお待ちしています。{名前}様")
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertIn("{名前}", err)
        self.assertIn("{日付}", err)
        code, out, err = self.prep("{名前}様\n" + "あ" * 5000)
        self.assertEqual(code, 2)

    def test_double_braces_are_literal(self):
        text = "{{literal}} はそのまま送ります }} {{"
        code, out, err = self.prep(text)
        self.assertEqual(code, 0, err)
        self.assertIn(text, out)
        self.assertNotIn("unfilled", err)

    def test_json_fields(self):
        text = "お知らせ" + EMOJI + "\n"
        code, out, err = self.prep(text, "--json", "--remaining", "120")
        self.assertEqual(code, 0, err)
        r = json.loads(out)
        self.assertLessEqual({"ok", "chars", "utf16_units", "remaining", "placeholders"}, set(r))
        self.assertEqual((r["ok"], r["chars"], r["utf16_units"], r["remaining"], r["placeholders"]),
                         (True, 6, 7, 120, []))
        code, out, err = self.prep("{名前}様 {{x}}", "--json")
        self.assertEqual(code, 3)
        r = json.loads(out)
        self.assertEqual((r["ok"], r["remaining"], r["placeholders"]), (False, None, ["名前"]))


if __name__ == "__main__":
    print("scripts: %s" % SCRIPTS)
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    live_ok = live_db_stat() == LIVE_BEFORE
    if not live_ok:
        print("FAIL: %s changed during the run" % LIVE_DB)
    print("summary: ran %d, failures %d, errors %d, skipped %d, live DB %s"
          % (result.testsRun, len(result.failures), len(result.errors),
             len(result.skipped), "unchanged" if live_ok else "CHANGED"))
    sys.exit(0 if result.wasSuccessful() and live_ok else 1)
