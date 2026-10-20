import contextlib
import importlib.util
import io
import itertools
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime
from unittest import mock

sys.dont_write_bytecode = True
SCRIPTS = os.environ.get("SCRIPTS") or os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts"))
REAL_DB = "/workspace/line-inbox/messages.db"
TOKEN = "test-token-SECRET123"
LIMIT = 1000
OUTPUTS = []
SEQ = itertools.count(1)
FILL = {"type": "text", "message_type": "text", "kind": "text", "text": "hello"}
_MODS = {}


def load(name):
    if name not in _MODS:
        spec = importlib.util.spec_from_file_location(
            "desk_" + name, os.path.join(SCRIPTS, name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MODS[name] = mod
    return _MODS[name]


def real_db_snapshot():
    try:
        st = os.stat(REAL_DB)
    except FileNotFoundError:
        return None
    return st.st_mtime_ns, st.st_size


def is_real_db(path):
    return os.path.realpath(path or "") == os.path.realpath(REAL_DB)


class FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeNet:

    def __init__(self, handler=None, errors=()):
        self.handler = handler
        self.errors = list(errors)
        self.requests = []

    def __call__(self, req, timeout=None, **kwargs):
        self.requests.append(req)
        if self.errors:
            raise self.errors.pop(0)
        if self.handler is None:
            raise AssertionError("unexpected request to %s" % req.full_url)
        status, body = self.handler(req)
        return FakeResp(status, body)


def http_error(url, code, body):
    return urllib.error.HTTPError(url, code, "error", {}, io.BytesIO(body))


class DeskCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.db = os.path.join(self.tmp, "messages.db")
        env = mock.patch.dict(os.environ, {
            "DESK_DB": self.db, "LINE_CHANNEL_ACCESS_TOKEN": TOKEN,
            "PYTHONDONTWRITEBYTECODE": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.assertFalse(is_real_db(os.environ["DESK_DB"]))
        p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "init_db.py")],
                           cwd=self.tmp, capture_output=True, text=True,
                           timeout=60)
        OUTPUTS.append(p.stdout + p.stderr)
        self.assertEqual(p.returncode, 0, "init_db.py failed: " + p.stderr)
        self.assertTrue(os.path.isfile(self.db))
        self.assertNotIn(TOKEN, p.stdout + p.stderr)
        sleep = mock.patch("time.sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def run_main(self, name, argv, net):
        self.assertFalse(is_real_db(os.environ.get("DESK_DB")))
        mod = load(name)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(mod.urllib.request, "urlopen", net), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = mod.main(argv)
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else int(e.code is not None)
        so, se = out.getvalue(), err.getvalue()
        OUTPUTS.append(so + se)
        self.assertNotIn(TOKEN, so + se, "access token leaked to output")
        return code, so, se


class MarkReadTest(DeskCase):
    def setUp(self):
        super().setUp()
        self.read_tokens = {}

    def add(self, hour, user="U1", group=None, room=None, direction="in",
            token=True, read_at=None):
        n = next(SEQ)
        source_type = "group" if group else "room" if room else "user"
        message = {"id": "m%d" % n, "type": "text", "text": "hello %d" % n}
        if token:
            message["markAsReadToken"] = "read-token-%d" % n
        source = {"type": source_type, "userId": user}
        if group:
            source["groupId"] = group
        if room:
            source["roomId"] = room
        event = {"type": "message", "timestamp": 1759280000000 + n,
                 "source": source, "message": message}
        vals = {"direction": direction, "at": "2026-10-01T%02d:00:00+09:00" % hour,
                "source_type": source_type, "user_id": user, "group_id": group,
                "room_id": room, "raw": json.dumps(event), "read_at": read_at}
        conn = sqlite3.connect(self.db)
        try:
            info = conn.execute("PRAGMA table_info(messages)").fetchall()
            for _, col, ctype, notnull, default, pk in info:
                if col not in vals and notnull and default is None and not pk:
                    vals[col] = FILL.get(col, n if "INT" in (ctype or "").upper()
                                         else "%s-%d" % (col, n))
            cols = list(vals)
            with conn:
                cur = conn.execute(
                    "INSERT INTO messages (%s) VALUES (%s)"
                    % (", ".join(cols), ", ".join("?" * len(cols))),
                    [vals[c] for c in cols])
        finally:
            conn.close()
        self.read_tokens[cur.lastrowid] = message.get("markAsReadToken")
        return cur.lastrowid

    def read_at(self):
        conn = sqlite3.connect(self.db)
        try:
            return dict(conn.execute("SELECT id, read_at FROM messages"))
        finally:
            conn.close()

    @staticmethod
    def ok_net(errors=()):
        return FakeNet(lambda req: (200, b"{}"), errors)

    def test_success_marks_this_and_earlier_inbound_of_same_chat(self):
        a = self.add(9)
        pre = self.add(9, read_at="2026-09-30T08:00:00+09:00")
        b = self.add(10)
        later = self.add(11)
        other_user = self.add(8, user="U2")
        group = self.add(8, user="U1", group="G1")
        outbound = self.add(9, direction="out", token=False)
        net = self.ok_net()
        code, out, err = self.run_main("mark_read", [str(b)], net)
        self.assertEqual(code, 0, err)
        self.assertRegex(out, r"\b2\b")
        reads = self.read_at()
        for mid in (a, b):
            self.assertRegex(reads[mid] or "", r"^\d{4}-\d\d-\d\dT")
        self.assertEqual(reads[pre], "2026-09-30T08:00:00+09:00")
        for mid in (later, other_user, group, outbound):
            self.assertIsNone(reads[mid], "message %d should stay unread" % mid)
        self.assertEqual(len(net.requests), 1)
        req = net.requests[0]
        self.assertTrue(req.full_url.endswith("/v2/bot/chat/markAsRead"))
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertEqual(json.loads(req.data.decode("utf-8"))["markAsReadToken"],
                         self.read_tokens[b])

    def test_group_chat_is_keyed_by_group_id(self):
        g1 = self.add(9, user="U1", group="G1")
        g2 = self.add(10, user="U2", group="G1")
        g_later = self.add(11, user="U1", group="G1")
        dm = self.add(9, user="U2")
        room = self.add(9, user="U2", room="R1")
        code, out, err = self.run_main("mark_read", [str(g2)], self.ok_net())
        self.assertEqual(code, 0, err)
        reads = self.read_at()
        self.assertIsNotNone(reads[g1])
        self.assertIsNotNone(reads[g2])
        for mid in (g_later, dm, room):
            self.assertIsNone(reads[mid])

    def test_connection_error_is_retried_once(self):
        mid = self.add(10)
        net = self.ok_net([urllib.error.URLError("Connection refused")])
        code, out, err = self.run_main("mark_read", [str(mid)], net)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(net.requests), 2)
        self.assertEqual(self.sleep.call_count, 1)
        self.assertIsNotNone(self.read_at()[mid])

    def test_failures_leave_db_unchanged_and_hide_token(self):
        mid = self.add(10)
        url = load("mark_read").MARK_READ_URL
        cases = [
            ("two connection errors",
             [urllib.error.URLError("refused " + TOKEN) for _ in range(2)], 2),
            ("HTTP 401 echoing the token",
             [http_error(url, 401, b'{"message":"bad ' + TOKEN.encode() + b'"}')], 1),
            ("HTTP 500 is not retried", [http_error(url, 500, b"oops")], 1),
        ]
        for label, errors, calls in cases:
            with self.subTest(label):
                net = self.ok_net(errors)
                code, out, err = self.run_main("mark_read", [str(mid)], net)
                self.assertNotEqual(code, 0)
                self.assertIn("error", err.lower())
                self.assertEqual(len(net.requests), calls)
                self.assertIsNone(self.read_at()[mid])

    def test_missing_token_errors_without_request(self):
        mid = self.add(10)
        for value in (None, "   "):
            with self.subTest(token=value):
                with mock.patch.dict(os.environ):
                    if value is None:
                        os.environ.pop("LINE_CHANNEL_ACCESS_TOKEN", None)
                    else:
                        os.environ["LINE_CHANNEL_ACCESS_TOKEN"] = value
                    net = FakeNet()
                    code, out, err = self.run_main("mark_read", [str(mid)], net)
                self.assertNotEqual(code, 0)
                self.assertIn("LINE_CHANNEL_ACCESS_TOKEN", err)
                self.assertEqual(net.requests, [])
                self.assertIsNone(self.read_at()[mid])


class QuotaCheckTest(DeskCase):
    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.tmp, "quota_state.json")

    def net(self, remaining, errors=()):
        def handler(req):
            self.assertEqual(req.get_method(), "GET")
            self.assertEqual(req.get_header("Authorization"), "Bearer " + TOKEN)
            if req.full_url.endswith("/v2/bot/message/quota"):
                body = {"type": "limited", "value": LIMIT}
            elif req.full_url.endswith("/v2/bot/message/quota/consumption"):
                body = {"totalUsage": LIMIT - remaining}
            else:
                raise AssertionError("unexpected URL " + req.full_url)
            return 200, json.dumps(body).encode("utf-8")
        return FakeNet(handler, errors)

    def quota(self, remaining, *flags):
        code, out, err = self.run_main("quota_check", ["--json"] + list(flags),
                                       self.net(remaining))
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["remaining"], remaining)
        return result

    def test_notice_thresholds(self):
        steps = [(60, False), (51, False), (50, True), (50, False), (49, False),
                 (21, False), (20, True), (19, False), (15, True), (11, False),
                 (10, True), (6, False), (5, True), (4, True), (3, True),
                 (2, True), (1, True), (0, True), (0, False)]
        for remaining, due in steps:
            with self.subTest(remaining=remaining):
                self.assertIs(self.quota(remaining)["notice_due"], due)

    def test_jumped_threshold_gives_one_notice(self):
        r = self.quota(12)
        self.assertTrue(r["notice_due"])
        self.assertIn("15", r["reason"])
        self.assertFalse(self.quota(11)["notice_due"])
        r = self.quota(3)
        self.assertTrue(r["notice_due"])
        self.assertIn("3", r["reason"])
        self.assertFalse(self.quota(3)["notice_due"])

    def test_state_file_is_0600(self):
        r = self.quota(50)
        self.assertTrue(r["notice_due"])
        self.assertTrue(r["state_saved"])
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o600)
        with open(self.state, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn(TOKEN, text)
        data = json.loads(text)
        self.assertEqual(data.get("month"),
                         datetime.now().astimezone().strftime("%Y-%m"))
        self.assertEqual(data.get("last_noticed_remaining"), 50)
        self.assertFalse(os.path.exists(self.state + ".tmp"))
        custom = os.path.join(self.tmp, "custom.json")
        with open(custom, "w", encoding="utf-8") as f:
            json.dump({"month": "2000-01", "last_noticed_remaining": 0}, f)
        os.chmod(custom, 0o644)
        self.assertTrue(self.quota(50, "--state", custom)["notice_due"])
        self.assertEqual(stat.S_IMODE(os.stat(custom).st_mode), 0o600)

    def test_connection_error_is_retried_once(self):
        net = self.net(50, [urllib.error.URLError("timed out")])
        code, out, err = self.run_main("quota_check", [], net)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(net.requests), 3)
        self.assertEqual(self.sleep.call_count, 1)
        self.assertRegex(out, r"remaining:\s*50")
        self.assertRegex(out, r"notice_due:\s*yes")

    def test_no_update_writes_no_state(self):
        r = self.quota(50, "--no-update")
        self.assertTrue(r["notice_due"])
        self.assertFalse(r["state_saved"])
        self.assertFalse([n for n in os.listdir(self.tmp)
                          if n.startswith("quota_state")])
        self.assertTrue(self.quota(50)["notice_due"])
        with open(self.state, "rb") as f:
            saved = f.read()
        mtime = os.stat(self.state).st_mtime_ns
        r = self.quota(10, "--no-update")
        self.assertTrue(r["notice_due"])
        self.assertFalse(r["state_saved"])
        with open(self.state, "rb") as f:
            self.assertEqual(f.read(), saved)
        self.assertEqual(os.stat(self.state).st_mtime_ns, mtime)
        self.assertTrue(self.quota(10)["notice_due"])

    def test_errors_exit_nonzero_and_hide_token(self):
        url = load("quota_check").API + "/v2/bot/message/quota"
        cases = [
            ("HTTP 401 echoing the token",
             [http_error(url, 401, b'{"message":"bad ' + TOKEN.encode() + b'"}')], 1),
            ("two connection errors",
             [urllib.error.URLError("no route " + TOKEN) for _ in range(2)], 2),
        ]
        for label, errors, calls in cases:
            with self.subTest(label):
                net = self.net(50, errors)
                code, out, err = self.run_main("quota_check", [], net)
                self.assertNotEqual(code, 0)
                self.assertIn("error", err.lower())
                self.assertEqual(len(net.requests), calls)
                self.assertFalse(os.path.exists(self.state))
        with mock.patch.dict(os.environ, {"LINE_CHANNEL_ACCESS_TOKEN": ""}):
            net = FakeNet()
            code, out, err = self.run_main("quota_check", [], net)
        self.assertNotEqual(code, 0)
        self.assertIn("LINE_CHANNEL_ACCESS_TOKEN", err)
        self.assertEqual(net.requests, [])
        self.assertFalse(os.path.exists(self.state))


def main():
    if not os.path.isdir(SCRIPTS):
        print("error: scripts folder not found: %s (set SCRIPTS)" % SCRIPTS,
              file=sys.stderr)
        return 2
    before = real_db_snapshot()
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    untouched = before is None or real_db_snapshot() == before
    leaks = sum(TOKEN in o for o in OUTPUTS)
    print("\nsummary: %d run, %d failures, %d errors, %d skipped; "
          "real DB %s; token leaks: %d"
          % (result.testsRun, len(result.failures), len(result.errors),
             len(result.skipped), "untouched" if untouched else "CHANGED", leaks))
    if not untouched:
        print("error: %s was modified during the run" % REAL_DB, file=sys.stderr)
    return 0 if result.wasSuccessful() and untouched and not leaks else 1


if __name__ == "__main__":
    sys.exit(main())
