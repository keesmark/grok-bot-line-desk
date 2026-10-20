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
LIVE_DB = "/workspace/line-inbox/messages.db"
TOKEN = "test-token-SECRET123"
TABLES = {"messages", "conversations", "customers", "canned_replies", "customer_tags"}
NEW_MESSAGE_COLUMNS = {"read_at", "media_path", "preview_path", "media_saved_at"}


def load(name):
    path = os.path.join(SCRIPTS, name)
    if not os.path.isfile(path):
        sys.exit(f"script not found: {path} (set env SCRIPTS)")
    spec = importlib.util.spec_from_file_location("desk_" + name[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


INIT = load("init_db.py")
PURGE = load("purge_old.py")


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def live_db_state():
    try:
        st = os.stat(LIVE_DB)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


class DeskCase(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.db = os.path.join(self.dir, "messages.db")
        self.net_calls = []

        def fake_urlopen(*args, **kwargs):
            self.net_calls.append(args)
            raise OSError("network is disabled in tests")

        patches = [mock.patch.dict(os.environ, {"DESK_DB": self.db, "LINE_CHANNEL_ACCESS_TOKEN": TOKEN}),
                   mock.patch.object(urllib.request, "urlopen", fake_urlopen)]
        for mod in (INIT, PURGE):
            if hasattr(mod, "urlopen"):
                patches.append(mock.patch.object(mod, "urlopen", fake_urlopen))
            if hasattr(mod, "DEFAULT_DB"):
                patches.append(mock.patch.object(mod, "DEFAULT_DB", self.db))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.assertNotEqual(os.path.abspath(os.environ["DESK_DB"]), LIVE_DB)

    def run_script(self, mod, *argv):
        self.assertNotEqual(os.path.abspath(os.environ.get("DESK_DB", "")), LIVE_DB)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = mod.main(list(argv))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        out, err = out.getvalue(), err.getvalue()
        self.assertNotIn(TOKEN, out + err)
        self.assertEqual(self.net_calls, [], "a script tried to use the network")
        return code, out, err

    def init(self):
        code, out, err = self.run_script(INIT)
        self.assertEqual(code, 0, err)
        return out

    def sql(self, query, params=()):
        con = sqlite3.connect(self.db)
        try:
            rows = con.execute(query, params).fetchall()
            con.commit()
            return rows
        finally:
            con.close()

    def tables(self):
        return {r[0] for r in self.sql("SELECT name FROM sqlite_master WHERE type='table'")}

    def columns(self, table):
        return {r[1] for r in self.sql(f"PRAGMA table_info({table})")}

    def dump(self):
        return {t: sorted(self.sql(f"SELECT * FROM {t}"), key=repr) for t in sorted(self.tables())}


OLD_LAYOUT = """
CREATE TABLE messages(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 direction TEXT NOT NULL CHECK(direction IN ('in','out')),
 at TEXT NOT NULL,
 source_type TEXT, user_id TEXT, group_id TEXT, room_id TEXT,
 message_type TEXT, text TEXT, line_message_id TEXT,
 webhook_event_id TEXT UNIQUE,
 status TEXT, raw TEXT);
CREATE TABLE conversations(
 chat_key TEXT PRIMARY KEY, source_type TEXT,
 state TEXT NOT NULL DEFAULT '未対応' CHECK(state IN ('未対応','対応中','完了')),
 memo TEXT, updated_at TEXT);
CREATE TABLE customers(
 user_id TEXT PRIMARY KEY, display_name TEXT, note TEXT, evidence TEXT, updated_at TEXT);
INSERT INTO messages(direction, at, source_type, user_id, message_type, text, webhook_event_id)
 VALUES('in', '2025-05-01T10:00:00+09:00', 'user', 'U1', 'text', 'hello', 'ev1');
INSERT INTO conversations(chat_key, source_type, state, memo, updated_at)
 VALUES('U1', 'user', '対応中', 'memo1', '2025-05-01T10:00:00+09:00');
INSERT INTO customers(user_id, display_name, note, updated_at)
 VALUES('U1', 'Alice', 'likes tea', '2025-05-01T10:00:00+09:00');
"""


class InitDbTest(DeskCase):
    def test_fresh_db_tables_and_modes(self):
        self.init()
        self.assertLessEqual(TABLES, self.tables())
        self.assertEqual(mode(self.db), 0o600)
        self.assertEqual(mode(self.dir), 0o700)
        media = os.path.join(self.dir, "media")
        self.assertTrue(os.path.isdir(media))
        self.assertEqual(mode(media), 0o700)
        self.assertEqual(self.sql("PRAGMA journal_mode")[0][0].lower(), "wal")
        self.assertLessEqual(NEW_MESSAGE_COLUMNS, self.columns("messages"))
        self.assertIn("profile_at", self.columns("customers"))

    def test_rerun_is_idempotent_and_keeps_rows(self):
        self.init()
        self.sql("INSERT INTO messages(direction, at, user_id, text) "
                 "VALUES('in', '2026-01-01T00:00:00+09:00', 'U1', 'hi')")
        self.sql("INSERT INTO conversations(chat_key, state) VALUES('U1', '対応中')")
        self.sql("INSERT INTO customers(user_id, note) VALUES('U1', 'note')")
        self.sql("INSERT INTO canned_replies(name, body) VALUES('thanks', 'Thank you!')")
        self.sql("INSERT INTO customer_tags(user_id, tag, created_at) VALUES('U1', 'vip', '2026-01-01')")
        schema = self.sql("SELECT type, name, sql FROM sqlite_master ORDER BY name")
        rows = self.dump()
        os.chmod(self.db, 0o644)
        os.chmod(os.path.join(self.dir, "media"), 0o755)
        for _ in range(2):
            self.init()
        self.assertEqual(self.sql("SELECT type, name, sql FROM sqlite_master ORDER BY name"), schema)
        self.assertEqual(self.dump(), rows)
        self.assertEqual(mode(self.db), 0o600)
        self.assertEqual(mode(os.path.join(self.dir, "media")), 0o700)

    def test_old_layout_upgrades_and_keeps_rows(self):
        con = sqlite3.connect(self.db)
        con.executescript(OLD_LAYOUT)
        con.close()
        os.chmod(self.db, 0o644)
        self.init()
        self.assertLessEqual(TABLES, self.tables())
        self.assertLessEqual(NEW_MESSAGE_COLUMNS, self.columns("messages"))
        self.assertIn("profile_at", self.columns("customers"))
        self.assertEqual(self.sql("SELECT text, webhook_event_id, read_at, media_path, preview_path, "
                                  "media_saved_at FROM messages"), [("hello", "ev1", None, None, None, None)])
        self.assertEqual(self.sql("SELECT state, memo FROM conversations WHERE chat_key='U1'"),
                         [("対応中", "memo1")])
        self.assertEqual(self.sql("SELECT display_name, note, profile_at FROM customers"),
                         [("Alice", "likes tea", None)])
        self.assertEqual(mode(self.db), 0o600)
        self.assertEqual(mode(os.path.join(self.dir, "media")), 0o700)
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("INSERT INTO messages(direction, at, webhook_event_id) VALUES('in', '2025-05-02', 'ev1')")
        self.sql("INSERT INTO customer_tags(user_id, tag, created_at) VALUES('U1', 'vip', '2026-01-01')")
        rows = self.dump()
        self.init()
        self.assertEqual(self.dump(), rows)

    def test_customer_tags_primary_key(self):
        self.init()
        add = "INSERT INTO customer_tags(user_id, tag, created_at) VALUES(?, ?, ?)"
        for tag in ("vip", "new", "repeat"):
            self.sql(add, ("U1", tag, "2026-01-01"))
        self.sql(add, ("U2", "vip", "2026-01-01"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql(add, ("U1", "vip", "2026-02-02"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("INSERT INTO customer_tags(user_id, tag) VALUES('U3', 'x')")
        self.assertEqual(self.sql("SELECT user_id, COUNT(*) FROM customer_tags GROUP BY user_id ORDER BY user_id"),
                         [("U1", 3), ("U2", 1)])


OLD, NEW = "2025-01-10T09:00:00+09:00", "2026-09-01T09:00:00+09:00"
MESSAGES = [
    ("in", OLD, "user", "U1", None, "U1 old"),
    ("out", NEW, "user", "U1", None, "U1 new"),
    ("in", "2026-09-01T10:00:00+09:00", "group", "U1", "G1", "U1 in G1"),
    ("in", "2025-02-01T09:00:00+09:00", "user", "U2", None, "U2 old"),
    ("in", "2026-09-02T09:00:00+09:00", "user", "U2", None, "U2 new"),
]
CONVERSATIONS = [("U1", "user", OLD), ("U2", "user", NEW), ("G1", "group", NEW),
                 ("U3", "user", "2025-01-01T09:00:00+09:00")]
CUSTOMERS = [("U1", "Alice", OLD), ("U2", "Bob", NEW)]
TAGS = [("U1", "vip", OLD), ("U1", "new", NEW), ("U2", "vip", "2025-03-01T09:00:00+09:00"), ("U2", "repeat", NEW)]
USER_U1 = {"messages": 3, "conversations": 1, "customers": 1, "customer_tags": 2}
BEFORE_2026 = {"messages": 2, "conversations": 1, "customers": 1, "customer_tags": 2}


class PurgeOldTest(DeskCase):
    def setUp(self):
        super().setUp()
        self.init()
        con = sqlite3.connect(self.db)
        try:
            con.executemany("INSERT INTO messages(direction, at, source_type, user_id, group_id, message_type, text) "
                            "VALUES(?, ?, ?, ?, ?, 'text', ?)", MESSAGES)
            con.executemany("INSERT INTO conversations(chat_key, source_type, updated_at) VALUES(?, ?, ?)",
                            CONVERSATIONS)
            con.executemany("INSERT INTO customers(user_id, display_name, updated_at) VALUES(?, ?, ?)", CUSTOMERS)
            con.executemany("INSERT INTO customer_tags(user_id, tag, created_at) VALUES(?, ?, ?)", TAGS)
            con.commit()
        finally:
            con.close()

    def purge(self, *argv):
        code, out, err = self.run_script(PURGE, *argv)
        self.assertEqual(code, 0, err)
        return out

    def counts(self, out):
        return {t: int(n) for t, n in re.findall(r"^(\w+):[ \t]*(\d+)[ \t]*$", out, re.M)}

    def assertCounts(self, out, expected):
        got = self.counts(out)
        self.assertEqual({t: got.get(t) for t in expected}, expected, out)

    def remaining(self, tags=True):
        left = {"messages": sorted(r[0] for r in self.sql("SELECT text FROM messages")),
                "conversations": sorted(r[0] for r in self.sql("SELECT chat_key FROM conversations")),
                "customers": sorted(r[0] for r in self.sql("SELECT user_id FROM customers"))}
        if tags:
            left["customer_tags"] = sorted(self.sql("SELECT user_id, tag FROM customer_tags"))
        return left

    def test_dry_run_changes_nothing(self):
        before = self.dump()
        out = self.purge("--user", "U1")
        self.assertIn("dry run", out.lower())
        self.assertCounts(out, USER_U1)
        out = self.purge("--before", "2026-01-01")
        self.assertIn("dry run", out.lower())
        self.assertCounts(out, BEFORE_2026)
        self.assertEqual(self.dump(), before)

    def test_user_yes_deletes_only_that_user(self):
        out = self.purge("--user", "U1", "--yes")
        self.assertCounts(out, USER_U1)
        self.assertIn("vacuum", out.lower())
        self.assertEqual(self.remaining(), {
            "messages": ["U2 new", "U2 old"],
            "conversations": ["G1", "U2", "U3"],
            "customers": ["U2"],
            "customer_tags": [("U2", "repeat"), ("U2", "vip")]})
        self.assertEqual(mode(self.db), 0o600)

    def test_before_yes_deletes_old_rows_and_tags(self):
        out = self.purge("--before", "2026-01-01", "--yes")
        self.assertCounts(out, BEFORE_2026)
        self.assertEqual(self.remaining(), {
            "messages": ["U1 in G1", "U1 new", "U2 new"],
            "conversations": ["G1", "U1", "U2"],
            "customers": ["U2"],
            "customer_tags": [("U1", "new"), ("U2", "repeat")]})

    def test_works_without_customer_tags_table(self):
        self.sql("DROP TABLE customer_tags")
        before = self.dump()
        out = self.purge("--user", "U1")
        self.assertNotIn("customer_tags", self.counts(out))
        self.assertEqual(self.dump(), before)
        out = self.purge("--user", "U1", "--yes")
        self.assertCounts(out, {"messages": 3, "conversations": 1, "customers": 1})
        out = self.purge("--before", "2026-01-01", "--yes")
        self.assertCounts(out, {"messages": 1, "conversations": 1, "customers": 0})
        self.assertEqual(self.remaining(tags=False),
                         {"messages": ["U2 new"], "conversations": ["G1", "U2"], "customers": ["U2"]})
        self.assertNotIn("customer_tags", self.tables())

    def test_bad_arguments_change_nothing(self):
        before = self.dump()
        for argv in ([], ["--yes"], ["--before", "2026-13-01", "--yes"], ["--user", "  ", "--yes"],
                     ["--user", "U1", "--before", "2026-01-01", "--yes"]):
            code, _, err = self.run_script(PURGE, *argv)
            self.assertNotEqual(code, 0, argv)
            self.assertTrue(err.strip(), argv)
        self.assertEqual(self.dump(), before)


def main():
    live_before = live_db_state()
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
    live_ok = live_db_state() == live_before
    if not live_ok:
        print(f"FAIL: {LIVE_DB} changed during the run (mtime or size)")
    ok = result.wasSuccessful() and live_ok
    print(f"summary: {result.testsRun} tests, {len(result.failures)} failures, {len(result.errors)} errors, "
          f"{len(result.skipped)} skipped; live DB {'unchanged' if live_ok else 'CHANGED'} -> "
          f"{'OK' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
