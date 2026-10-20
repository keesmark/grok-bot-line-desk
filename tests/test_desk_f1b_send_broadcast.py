import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
import urllib.request
from unittest import mock

sys.dont_write_bytecode = True
SCRIPTS = os.environ.get("SCRIPTS") or os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts"))
LIVE_DB = "/workspace/line-inbox/messages.db"
TOKEN = "test-token-SECRET123"
T0 = 1700000000000
OLD_LAYOUT = """
CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT,
 direction TEXT NOT NULL CHECK(direction IN ('in','out')), at TEXT NOT NULL,
 source_type TEXT, user_id TEXT, group_id TEXT, room_id TEXT, message_type TEXT, text TEXT,
 line_message_id TEXT, webhook_event_id TEXT UNIQUE, status TEXT, raw TEXT, read_at TEXT);
CREATE TABLE conversations(chat_key TEXT PRIMARY KEY, source_type TEXT,
 state TEXT NOT NULL DEFAULT '未対応', memo TEXT, updated_at TEXT);
"""


def live_db_stat():
    try:
        st = os.stat(LIVE_DB)
    except FileNotFoundError:
        return None
    return st.st_mtime_ns, st.st_size


def load(name):
    spec = importlib.util.spec_from_file_location(name[:-3], os.path.join(SCRIPTS, name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LIVE_BEFORE = live_db_stat()
INIT_DB, LOG_EVENT = load("init_db.py"), load("log_event.py")
NET_CALLS, OUTPUTS = [], []


def fake_urlopen(*args, **kwargs):
    NET_CALLS.append(args)
    raise OSError("network is disabled in tests")


def event(uid, kind, n, ts=T0):
    ev = {"type": kind, "timestamp": ts, "webhookEventId": f"ev-{uid}-{n}",
          "source": {"type": "user", "userId": uid}, "deliveryContext": {"isRedelivery": False}}
    if kind == "message":
        ev["message"] = {"type": "text", "id": f"m-{uid}-{n}", "text": "hi"}
    return ev


class Base(unittest.TestCase):
    INIT = True

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = os.path.join(tmp.name, "messages.db")
        for p in (mock.patch.dict(os.environ, {"DESK_DB": self.db, "LINE_CHANNEL_ACCESS_TOKEN": TOKEN}),
                  mock.patch.object(urllib.request, "urlopen", fake_urlopen)):
            p.start()
            self.addCleanup(p.stop)
        self.assert_safe_db()
        self.text_file = os.path.join(tmp.name, "msg.txt")
        with open(self.text_file, "w", encoding="utf-8") as f:
            f.write("Hello all\n")
        if self.INIT:
            self.assertEqual(self.run_script(INIT_DB, [])[0], 0)
            self.assertEqual(stat.S_IMODE(os.stat(self.db).st_mode), 0o600)

    def tearDown(self):
        self.assertEqual(NET_CALLS, [], "a network call was attempted")

    def assert_safe_db(self):
        desk_db = os.environ.get("DESK_DB", "")
        self.assertEqual(desk_db, self.db)
        self.assertNotEqual(os.path.realpath(desk_db), os.path.realpath(LIVE_DB))

    def run_script(self, mod, argv, stdin=""):
        self.assert_safe_db()
        out, err = io.StringIO(), io.StringIO()
        fake_in = io.TextIOWrapper(io.BytesIO(stdin.encode("utf-8")), encoding="utf-8")
        with mock.patch.object(sys, "stdin", fake_in), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            try:
                code = mod.main(argv)
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        OUTPUTS.append(out.getvalue() + err.getvalue())
        self.assertNotIn(TOKEN, OUTPUTS[-1])
        return code, out.getvalue(), err.getvalue()

    def log_out(self, *argv):
        return self.run_script(LOG_EVENT, ["--out", *argv])

    def rows(self, sql, *params):
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            return con.execute(sql, params).fetchall()

    def assert_rejected(self, argv, *needles):
        code, out, err = self.log_out(*argv)
        self.assertEqual(code, 1, err)
        self.assertIn("error", err)
        for needle in needles:
            self.assertIn(needle, err)
        self.assertNotIn("inserted", out)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM messages WHERE direction = 'out'"), [(0,)])
        if self.INIT:
            self.assertEqual(self.rows("SELECT COUNT(*) FROM send_recipients"), [(0,)])


class BroadcastLinkKnown(Base):
    def test_link_known_links_only_current_friends(self):
        body = {"destination": "Ubot", "events": [
            event("U1", "message", 1),
            event("U2", "message", 1), event("U2", "follow", 2, T0 + 60000),
            event("U3", "message", 1), event("U3", "unfollow", 2, T0 + 60000)]}
        code, _, err = self.run_script(LOG_EVENT, [], json.dumps(body))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM messages WHERE direction = 'in'"), [(5,)])

        code, out, err = self.log_out("--broadcast", "--link-known", "--text-file", self.text_file)
        self.assertEqual(code, 0, err)
        self.assertIn("send_kind=broadcast", out)
        self.assertIn("recipients_linked=2", out)
        sent = self.rows("SELECT id, user_id, send_kind, recipient_scope, text, status"
                         " FROM messages WHERE direction = 'out'")
        self.assertEqual([r[1:] for r in sent], [(None, "broadcast", "all", "Hello all", "sent")])
        self.assertEqual(self.rows("SELECT user_id, known_at_send FROM send_recipients"
                                   " WHERE message_id = ? ORDER BY user_id", sent[0][0]),
                         [("U1", 1), ("U2", 1)])

        code, out, err = self.log_out("--broadcast", "--text-file", self.text_file)
        self.assertEqual(code, 0, err)
        self.assertIn("recipients_linked=0", out)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM messages WHERE direction = 'out'"), [(2,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM send_recipients"), [(2,)])


class OutGuards(Base):
    def test_broadcast_with_to_is_rejected(self):
        for extra in ([], ["--link-known"]):
            with self.subTest(extra=extra):
                self.assert_rejected(["--broadcast", "--to", "U1", "--text-file", self.text_file, *extra],
                                     "--to", "--broadcast")

    def test_link_known_without_broadcast_is_rejected(self):
        for tos in (["--to", "U1"], ["--to", "U1", "--to", "U2"]):
            with self.subTest(tos=tos):
                self.assert_rejected([*tos, "--link-known", "--text-file", self.text_file], "--link-known")

    def test_app_reply_with_several_to_is_rejected(self):
        for extra in ([], ["--text-file", self.text_file]):
            with self.subTest(extra=extra):
                self.assert_rejected(["--to", "U1", "--to", "U2", "--status", "app_reply", *extra], "app_reply")


class OldLayoutDb(Base):
    INIT = False

    def setUp(self):
        super().setUp()
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            con.executescript(OLD_LAYOUT)
        os.chmod(self.db, 0o600)

    def test_segment_and_broadcast_need_init_db_but_single_to_works(self):
        for argv in (["--to", "U1", "--to", "U2"], ["--broadcast"], ["--broadcast", "--link-known"]):
            with self.subTest(argv=argv):
                self.assert_rejected([*argv, "--text-file", self.text_file], "init_db")
        code, out, err = self.log_out("--to", "U1", "--text-file", self.text_file)
        self.assertEqual(code, 0, err)
        self.assertIn("inserted", out)
        self.assertEqual(self.rows("SELECT direction, user_id, source_type, text, status FROM messages"),
                         [("out", "U1", "user", "Hello all", "sent")])
        tables = {r[0] for r in self.rows("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertNotIn("send_recipients", tables)


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=2).result
    problems = []
    if live_db_stat() != LIVE_BEFORE:
        problems.append(f"{LIVE_DB} changed (mtime/size)")
    if any(TOKEN in o for o in OUTPUTS):
        problems.append("fake token appeared in captured output")
    if NET_CALLS:
        problems.append(f"{len(NET_CALLS)} network call(s) attempted")
    ok = result.wasSuccessful() and not problems
    print(f"\ntest_f1b summary: {result.testsRun} run, {len(result.failures)} failed, "
          f"{len(result.errors)} errors; "
          + ("; ".join(problems) or "live DB untouched, no token leak, no network")
          + (" -> OK" if ok else " -> FAIL"))
    sys.exit(0 if ok else 1)
