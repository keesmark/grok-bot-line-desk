import contextlib
import importlib.util
import io
import os
import re
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
REAL_DB = "/workspace/line-inbox/messages.db"
TOKEN = "test-token-SECRET123"
OUTPUTS = []
NET_CALLS = []


def load(name):
    spec = importlib.util.spec_from_file_location("desk_" + name[:-3], os.path.join(SCRIPTS, name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


INIT, LOG = load("init_db.py"), load("log_event.py")


def real_db_state():
    try:
        st = os.stat(REAL_DB)
    except FileNotFoundError:
        return None
    return st.st_mtime_ns, st.st_size


def fake_urlopen(*args, **kwargs):
    NET_CALLS.append(args)
    raise AssertionError("network access attempted in a test")


class DeskTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.db = os.path.join(self.dir, "messages.db")
        env = mock.patch.dict(os.environ, {"DESK_DB": self.db, "LINE_CHANNEL_ACCESS_TOKEN": TOKEN})
        env.start()
        self.addCleanup(env.stop)
        self.assertNotEqual(os.path.abspath(os.environ["DESK_DB"]), REAL_DB)
        for mod in (INIT, LOG):
            req = getattr(getattr(mod, "urllib", None), "request", urllib.request)
            net = mock.patch.object(req, "urlopen", fake_urlopen)
            net.start()
            self.addCleanup(net.stop)

    def run_script(self, mod, *argv, stdin=""):
        self.assertNotEqual(os.path.abspath(os.environ["DESK_DB"]), REAL_DB)
        out, err = io.StringIO(), io.StringIO()
        fake_in = io.TextIOWrapper(io.BytesIO(stdin.encode("utf-8")), encoding="utf-8")
        with mock.patch.object(sys, "stdin", fake_in), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = mod.main(list(argv))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        OUTPUTS.append(out.getvalue() + err.getvalue())
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def init(self):
        code, out, err = self.run_script(INIT)
        self.assertEqual(code, 0, err)
        return out

    def q(self, sql, *args):
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            return con.execute(sql, args).fetchall()

    def cols(self, table):
        return {r[1] for r in self.q(f"PRAGMA table_info({table})")}

    def tables(self):
        return {r[0] for r in self.q("SELECT name FROM sqlite_master WHERE type='table'")}

    def snapshot(self):
        schema = self.q("SELECT type, name, sql FROM sqlite_master ORDER BY type, name")
        return schema, self.q("SELECT * FROM messages ORDER BY id"), self.q("SELECT * FROM conversations")

    def text_file(self, body="hello from the desk\n"):
        path = os.path.join(self.dir, "msg.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return path

    def log_out(self, *argv, stdin=""):
        code, out, err = self.run_script(LOG, "--out", *argv, stdin=stdin)
        self.assertEqual(code, 0, err)
        m = re.search(r"inserted (\d+)", out)
        self.assertIsNotNone(m, out)
        return int(m.group(1)), out

    def out_row(self, mid):
        rows = self.q("SELECT direction, user_id, send_kind, recipient_scope, status, text"
                      " FROM messages WHERE id = ?", mid)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def recipients(self, mid):
        return self.q("SELECT user_id, known_at_send FROM send_recipients WHERE message_id = ?"
                      " ORDER BY user_id", mid)


class InitDbTest(DeskTest):
    def test_fresh_db_has_send_schema(self):
        self.init()
        self.assertTrue({"messages", "conversations", "send_recipients"} <= self.tables())
        self.assertTrue({"send_kind", "recipient_scope"} <= self.cols("messages"))
        self.assertTrue({"message_id", "user_id", "known_at_send"} <= self.cols("send_recipients"))
        indexes = {r[0] for r in self.q("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("send_recipients_user", indexes)
        self.assertEqual(stat.S_IMODE(os.stat(self.db).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dir, "media")).st_mode), 0o700)
        snap = self.snapshot()
        self.init()
        self.assertEqual(self.snapshot(), snap)

    def test_old_db_upgrade_keeps_rows_and_rerun_is_idempotent(self):
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.executescript("""
CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT,
 direction TEXT NOT NULL CHECK(direction IN ('in','out')), at TEXT NOT NULL,
 source_type TEXT, user_id TEXT, group_id TEXT, room_id TEXT, message_type TEXT, text TEXT,
 line_message_id TEXT, webhook_event_id TEXT UNIQUE, status TEXT, raw TEXT, read_at TEXT);
CREATE TABLE conversations(chat_key TEXT PRIMARY KEY, source_type TEXT,
 state TEXT NOT NULL DEFAULT '未対応', memo TEXT, updated_at TEXT);
INSERT INTO messages(direction, at, source_type, user_id, message_type, text, webhook_event_id, status)
 VALUES('in', '2025-01-01T10:00:00+09:00', 'user', 'Uold1', 'text', 'old hello', 'ev-old-1', 'received');
INSERT INTO messages(direction, at, source_type, user_id, message_type, text, status)
 VALUES('out', '2025-01-01T10:05:00+09:00', 'user', 'Uold1', 'text', 'old reply', 'sent');
INSERT INTO conversations(chat_key, source_type, state) VALUES('Uold1', 'user', '対応中');
""")
        keep = "SELECT id, direction, at, user_id, text, webhook_event_id, status FROM messages ORDER BY id"
        before = self.q(keep)
        self.assertNotIn("send_recipients", self.tables())
        self.init()
        self.assertIn("send_recipients", self.tables())
        self.assertTrue({"send_kind", "recipient_scope"} <= self.cols("messages"))
        self.assertEqual(self.q(keep), before)
        self.assertEqual(self.q("SELECT send_kind, recipient_scope FROM messages"), [(None, None)] * 2)
        self.assertEqual(self.q("SELECT state FROM conversations WHERE chat_key='Uold1'"), [("対応中",)])
        self.assertEqual(stat.S_IMODE(os.stat(self.db).st_mode), 0o600)
        snap = self.snapshot()
        self.init()
        self.assertEqual(self.snapshot(), snap)


class LogOutTest(DeskTest):
    def test_single_to_is_push_one(self):
        self.init()
        mid, out = self.log_out("--to", "Ualice", "--text-file", self.text_file())
        self.assertRegex(out.strip(), r"^inserted \d+$")
        self.assertEqual(self.out_row(mid), ("out", "Ualice", "push", "one", "sent", "hello from the desk"))
        self.assertEqual(self.recipients(mid), [("Ualice", 0)])

    def test_two_to_with_duplicate_is_one_segment_list_row(self):
        self.init()
        mid, out = self.log_out("--to", "U1", "--to", "U2", "--to", "U1", "--text-file", self.text_file())
        for part in ("send_kind=segment", "scope=list", "recipients_linked=2"):
            self.assertIn(part, out)
        self.assertEqual(self.q("SELECT COUNT(*) FROM messages WHERE direction='out'"), [(1,)])
        self.assertEqual(self.out_row(mid), ("out", None, "segment", "list", "sent", "hello from the desk"))
        self.assertEqual(self.recipients(mid), [("U1", 0), ("U2", 0)])

    def test_broadcast_from_stdin_is_all_without_recipients(self):
        self.init()
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute("INSERT INTO messages(direction, at, source_type, user_id, message_type, status)"
                        " VALUES('in', '2025-01-01T10:00:00+09:00', 'user', 'Ufriend', 'follow', 'received')")
        mid, out = self.log_out("--broadcast", "--text", "-", stdin="news for everyone\n")
        for part in ("send_kind=broadcast", "scope=all", "recipients_linked=0"):
            self.assertIn(part, out)
        self.assertEqual(self.out_row(mid), ("out", None, "broadcast", "all", "sent", "news for everyone"))
        self.assertEqual(self.recipients(mid), [])
        self.assertEqual(self.q("SELECT COUNT(*) FROM send_recipients"), [(0,)])


if __name__ == "__main__":
    real_before = real_db_state()
    result = unittest.main(exit=False, verbosity=2).result
    real_ok = real_db_state() == real_before
    leaks = sum(TOKEN in o for o in OUTPUTS)
    ok = result.wasSuccessful() and real_ok and not leaks and not NET_CALLS
    print(f"\ntest_f1a: {result.testsRun} run, {len(result.failures)} failures, {len(result.errors)} errors;"
          f" real DB unchanged: {real_ok}; token leaks: {leaks}; network calls: {len(NET_CALLS)}")
    print("RESULT: " + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)
