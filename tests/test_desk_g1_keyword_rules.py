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
from datetime import datetime, timezone
from unittest import mock

sys.dont_write_bytecode = True
SCRIPTS = os.environ.get("SCRIPTS") or os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts"))
REAL_DB = "/workspace/line-inbox/messages.db"
TOKEN = "test-token-SECRET123"
NET_CALLS = []
OLD_SCHEMA = """
CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, direction TEXT NOT NULL, at TEXT NOT NULL,
 source_type TEXT, user_id TEXT, message_type TEXT, text TEXT, webhook_event_id TEXT UNIQUE,
 status TEXT, raw TEXT);
CREATE TABLE conversations(chat_key TEXT PRIMARY KEY, source_type TEXT,
 state TEXT NOT NULL DEFAULT '未対応', memo TEXT, updated_at TEXT);
INSERT INTO messages(direction, at, source_type, user_id, message_type, text) VALUES
 ('in', '2026-01-01T00:00:00+00:00', 'user', 'Uold', 'text', 'こんにちは'),
 ('out', '2026-01-01T00:01:00+00:00', 'user', 'Uold', 'text', 'お返事です');
INSERT INTO conversations(chat_key, source_type, state) VALUES('Uold', 'user', '対応中');
"""


def file_state(path):
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return st.st_mtime_ns, st.st_size


REAL_BEFORE = file_state(REAL_DB)


def load(name):
    spec = importlib.util.spec_from_file_location("g1_" + name, os.path.join(SCRIPTS, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


KW, INIT = load("keyword_reply"), load("init_db")


def fake_urlopen(*args, **kwargs):
    NET_CALLS.append(args)
    raise AssertionError("network access attempted")


def no_subprocess(*args, **kwargs):
    raise AssertionError("subprocess attempted (logging / sending is out of scope here)")


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.db = os.path.join(self.tmp, "messages.db")
        self.kpath = os.path.join(self.tmp, "keyword.json")
        for p in (mock.patch.dict(os.environ, {"DESK_DB": self.db, "LINE_CHANNEL_ACCESS_TOKEN": TOKEN}),
                  mock.patch.object(KW.urllib.request, "urlopen", fake_urlopen),
                  mock.patch.object(KW.subprocess, "run", no_subprocess)):
            p.start()
            self.addCleanup(p.stop)
        self.assertNotEqual(os.path.abspath(os.environ["DESK_DB"]), REAL_DB)
        code, _, err = self.run_main(INIT, [])
        self.assertEqual(code, 0, err)
        self.assertEqual(mode(self.db), 0o600)

    def run_main(self, mod, argv, stdin=""):
        self.assertNotEqual(os.path.abspath(os.environ.get("DESK_DB") or REAL_DB), REAL_DB)
        out, err = io.StringIO(), io.StringIO()
        fake_in = io.TextIOWrapper(io.BytesIO(stdin.encode("utf-8")), encoding="utf-8")
        with mock.patch.object(sys, "stdin", fake_in), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            try:
                code = mod.main(argv)
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def kw(self, *argv, stdin=""):
        return self.run_main(KW, list(argv), stdin)

    def kjson(self, *argv):
        code, out, err = self.kw(*argv, "--json")
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def add(self, keyword, reply, *extra):
        path = os.path.join(self.tmp, "reply.txt")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(reply)
        return self.kw("add", keyword, "--reply-file", path, *extra)

    def q(self, sql, *params, db=None):
        con = sqlite3.connect(db or self.db)
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    def rule(self, keyword):
        rows = self.q("SELECT id, keyword_norm, match_type, reply_text, enabled FROM keyword_rules"
                      " WHERE keyword=?", keyword)
        self.assertEqual(len(rows), 1, keyword)
        return rows[0]

    def count(self):
        return self.q("SELECT count(*) FROM keyword_rules")[0][0]

    def settings_file(self):
        with open(self.kpath, encoding="utf-8") as f:
            data = json.load(f)
        return data["auto_send"], data["window_hours"], data["min_remaining"]


class RuleTests(Base):
    def test_add_list_show_edit(self):
        text = "営業時間は10時〜19時です。\n{{x}} と {{名前}} はそのまま\tです"
        code, _, err = self.add("営業時間", text)
        self.assertEqual(code, 0, err)
        rid = self.rule("営業時間")[0]
        self.assertEqual(self.rule("営業時間")[1:], ("営業時間", "exact", text, 1))
        code, _, err = self.kw("add", "料金", "--reply", "-", "--match", "contains",
                               stdin="\n料金表は\r\nこちら  \n\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rule("料金")[2:], ("contains", "料金表は\nこちら", 1))
        code, out, err = self.kw("list")
        self.assertEqual(code, 0, err)
        self.assertIn("営業時間", out)
        self.assertEqual({r["keyword"] for r in self.kjson("list")}, {"営業時間", "料金"})
        out_path = os.path.join(self.tmp, "show_out.txt")
        with open(out_path, "w") as f:
            f.write("old content, longer than the reply " * 20)
        os.chmod(out_path, 0o644)
        code, _, err = self.kw("show", str(rid), "--out", out_path)
        self.assertEqual(code, 0, err)
        with open(out_path, "rb") as f:
            self.assertEqual(f.read(), text.encode("utf-8"))
        self.assertEqual(mode(out_path), 0o600)
        d = self.kjson("show", str(rid))
        self.assertEqual((d["reply_text"], d["enabled"], d["match_type"]), (text, True, "exact"))
        self.assertEqual(self.kw("show", "999")[0], 2)
        self.assertEqual(self.kw("edit", str(rid))[0], 2)
        self.assertEqual(self.kw("edit", "999", "--keyword", "x")[0], 2)
        code, _, err = self.kw("edit", str(rid), "--keyword", "受付 時間", "--match", "contains")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rule("受付 時間"), (rid, "受付時間", "contains", text, 1))
        code, _, err = self.kw("edit", str(rid), "--reply", "-", stdin="新しい返信です")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rule("受付 時間")[3], "新しい返信です")

    def test_enable_disable_remove(self):
        for k in ("A", "B"):
            self.assertEqual(self.add(k, "reply " + k)[0], 0)
        self.assertEqual(self.add("C", "reply C", "--disabled")[0], 0)
        a, b, c = (self.rule(k)[0] for k in "ABC")
        self.assertEqual(self.rule("C")[4], 0)
        self.assertEqual(self.kw("disable", str(a))[0], 0)
        self.assertEqual([r["id"] for r in self.kjson("list")], [b])
        self.assertEqual({r["id"]: r["enabled"] for r in self.kjson("list", "--all")},
                         {a: False, b: True, c: False})
        self.assertEqual(self.kw("enable", str(a))[0], 0)
        self.assertEqual(self.rule("A")[4], 1)
        code, out, err = self.kw("remove", str(a))
        self.assertEqual(code, 0, err)
        self.assertIn("would remove", out)
        self.assertEqual(self.count(), 3)
        code, _, err = self.kw("remove", str(a), "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.q("SELECT id FROM keyword_rules ORDER BY id"), [(b,), (c,)])
        for cmd in ("remove", "enable", "disable", "show"):
            self.assertEqual(self.kw(cmd, str(a))[0], 2, cmd)

    def test_duplicate_after_normalization(self):
        self.assertEqual(self.add("Hello World", "hi")[0], 0)
        for dup in ("ｈｅｌｌｏ\u3000ｗｏｒｌｄ", " HELLO  world ", "helloworld"):
            with self.subTest(dup=dup):
                code, _, err = self.add(dup, "x")
                self.assertEqual(code, 2, err)
                self.assertIn("error", err)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.add("HELLO WORLD", "y", "--match", "contains")[0], 0)
        cid = self.rule("HELLO WORLD")[0]
        self.assertEqual(self.kw("edit", str(cid), "--match", "exact")[0], 2)
        self.assertEqual(self.rule("HELLO WORLD")[2], "contains")
        self.assertEqual(self.add("Bye", "bye")[0], 0)
        self.assertEqual(self.kw("edit", str(self.rule("Bye")[0]), "--keyword", "ＨｅｌｌｏＷｏｒｌｄ")[0], 2)
        self.assertEqual(self.rule("Bye")[1], "bye")

    def test_reply_validation(self):
        bad = {"placeholder": "{名前}様、ご予約ありがとうございます", "too_long": "あ" * 5001,
               "control": "ベル\x07", "blank": " \n\n\t"}
        for i, (why, text) in enumerate(bad.items()):
            with self.subTest(why):
                code, _, err = self.add("bad%d" % i, text)
                self.assertEqual(code, 2, err)
        self.assertEqual(self.kw("add", "k", "--reply", "inline text")[0], 2)
        self.assertEqual(self.count(), 0)
        ok = "{{x}} は {{名前}} のまま"
        self.assertEqual(self.add("braces", ok)[0], 0)
        self.assertEqual(self.rule("braces")[3], ok)
        self.assertEqual(self.add("long", "あ" * 5000)[0], 0)
        self.assertEqual(len(self.rule("long")[3]), 5000)
        code, _, err = self.kw("edit", str(self.rule("braces")[0]), "--reply", "-", stdin="{名前}様")
        self.assertEqual(code, 2, err)
        self.assertEqual(self.rule("braces")[3], ok)


class SettingTests(Base):
    def test_setting_default_on_off(self):
        s = self.kjson("setting")
        self.assertEqual((s["auto_send"], s["window_hours"], s["min_remaining"]), (False, 24, 0))
        self.assertFalse(os.path.exists(self.kpath))
        code, _, err = self.kw("setting", "on")
        self.assertEqual(code, 0, err)
        self.assertEqual(mode(self.kpath), 0o600)
        self.assertEqual(self.settings_file(), (True, 24, 0))
        self.assertTrue(self.kjson("setting")["auto_send"])
        code, _, err = self.kw("setting", "off", "--window-hours", "6", "--min-remaining", "20")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.settings_file(), (False, 6, 20))
        self.assertEqual(mode(self.kpath), 0o600)
        self.assertEqual([f for f in os.listdir(self.tmp) if f.startswith(".keyword")], [])
        with open(self.kpath, "rb") as f:
            saved = f.read()
        for argv in (["--window-hours", "0"], ["on", "--window-hours", "0"], ["--min-remaining", "-1"],
                     ["maybe"]):
            with self.subTest(argv=argv):
                code, _, err = self.kw("setting", *argv)
                self.assertEqual(code, 2, err)
                self.assertIn("error", err)
        self.assertIn("window", self.kw("setting", "--window-hours", "0")[2])
        with open(self.kpath, "rb") as f:
            self.assertEqual(f.read(), saved)

    def test_broken_settings_file_counts_as_off(self):
        for content in ('{"auto_send": true,', '["auto_send"]', '{"auto_send": "yes", "window_hours": 0}'):
            with self.subTest(content=content):
                with open(self.kpath, "w", encoding="utf-8") as f:
                    f.write(content)
                code, out, err = self.kw("setting", "--json")
                self.assertEqual(code, 0, err)
                s = json.loads(out)
                self.assertEqual((s["auto_send"], s["window_hours"]), (False, 24))
                self.assertIn("warning", err.lower())
        self.assertEqual(self.add("予約", "ご予約はこちらから", "--match", "contains")[0], 0)
        with open(self.kpath, "w", encoding="utf-8") as f:
            f.write('{"auto_send": true,')
        con = sqlite3.connect(self.db)
        with con:
            mid = con.execute("INSERT INTO messages(direction, at, source_type, user_id, message_type, text)"
                              " VALUES('in', ?, 'user', 'Utest1', 'text', ?)",
                              (datetime.now(timezone.utc).isoformat(), "予約したいです")).lastrowid
        con.close()
        code, out, err = self.kw("match", str(mid), "--remaining", "unlimited", "--json")
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertEqual((res["action"], res["reason"]), ("suggest", "off"))
        self.assertIn("warning", err.lower())
        self.assertNotIn("予約したいです", out + err)
        self.assertEqual(self.q("SELECT decision FROM keyword_matches WHERE message_id=?", mid),
                         [("suggested",)])


class UpgradeTests(Base):
    def test_old_layout_db(self):
        old = os.path.join(self.tmp, "old", "messages.db")
        os.mkdir(os.path.dirname(old), 0o700)
        con = sqlite3.connect(old)
        con.executescript(OLD_SCHEMA)
        con.close()

        def tables():
            return {r[0] for r in self.q("SELECT name FROM sqlite_master WHERE type='table'", db=old)}

        def rows():
            return (self.q("SELECT id, direction, at, user_id, text FROM messages ORDER BY id", db=old),
                    self.q("SELECT chat_key, state FROM conversations", db=old))

        def schema():
            return self.q("SELECT type, name, sql FROM sqlite_master ORDER BY type, name", db=old)

        before = rows()
        with mock.patch.dict(os.environ, {"DESK_DB": old}):
            for argv in (["list"], ["add", "x", "--reply", "-"]):
                code, _, err = self.kw(*argv, stdin="hi")
                self.assertEqual(code, 1, err)
                self.assertIn("init_db", err)
            self.assertFalse({"keyword_rules", "keyword_matches"} & tables())
            code, _, err = self.run_main(INIT, [])
            self.assertEqual(code, 0, err)
            self.assertLessEqual({"keyword_rules", "keyword_matches", "customer_tags", "send_recipients"},
                                 tables())
            cols = {r[1] for r in self.q("PRAGMA table_info(messages)", db=old)}
            self.assertLessEqual({"send_kind", "recipient_scope", "media_path", "read_at"}, cols)
            self.assertEqual(rows(), before)
            self.assertEqual(mode(old), 0o600)
            first = schema()
            code, _, err = self.run_main(INIT, [])
            self.assertEqual(code, 0, err)
            self.assertEqual(schema(), first)
            self.assertEqual(rows(), before)
            self.assertEqual(self.add("営業時間", "10時からです")[0], 0)
            self.assertEqual(self.q("SELECT keyword FROM keyword_rules", db=old), [("営業時間",)])
        self.assertEqual(self.count(), 0)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    problems = []
    if file_state(REAL_DB) != REAL_BEFORE:
        problems.append("%s changed during the run" % REAL_DB)
    if NET_CALLS:
        problems.append("network access attempted %d times" % len(NET_CALLS))
    for p in problems:
        print("FAIL: " + p)
    ok = result.wasSuccessful() and not problems
    print("summary: %d tests, %d failures, %d errors, %d other problems -> %s" % (
        result.testsRun, len(result.failures), len(result.errors), len(problems), "OK" if ok else "FAILED"))
    sys.exit(0 if ok else 1)
