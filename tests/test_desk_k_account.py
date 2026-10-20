import contextlib
import datetime
import importlib.util
import io
import itertools
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock

sys.dont_write_bytecode = True
SCRIPTS = os.environ.get("SCRIPTS") or os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts"))
REAL_DB = "/workspace/line-inbox/messages.db"
TOKEN = "test-token-SECRET123"
OURS = "U" + "0" * 28 + "9c8a"
OTHER_DEST = "U" + "1" * 28 + "b7e2"
OTHER2 = "U" + "0" * 28 + "d4c3"
USER = "U" + "0" * 28 + "a1b2"
USER2 = "U" + "0" * 28 + "e5f6"
USER3 = "U" + "0" * 28 + "c3d4"
BAD_DESTS = ["Uxyz", "U" + "0" * 28 + "9C8A", "U" + "0" * 27 + "9c8a", "C" + "0" * 28 + "9c8a", 12345, None]
NOT_LEARNED = "account: not learned yet (the next webhook with a destination sets it)"
SHOW_KEYS = {"learned", "dest_tail", "updated_at", "rows", "other"}
KEYWORD = "営業時間"
EVENT_LINE = re.compile(r"^inserted (\d+) .* account=(learned|same|other|unknown)(?: dest=(\S+))? user_id=\S+$")
_mods, _ids = {}, itertools.count(1)


def load(name):
    if name not in _mods:
        spec = importlib.util.spec_from_file_location("k_" + name, os.path.join(SCRIPTS, name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _mods[name] = mod
    return _mods[name]


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


class AccountDestinationTest(unittest.TestCase):
    def setUp(self):
        _mods.clear()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir, self.db = tmp.name, os.path.join(tmp.name, "messages.db")
        env = mock.patch.dict(os.environ, {"DESK_DB": self.db, "LINE_CHANNEL_ACCESS_TOKEN": TOKEN})
        env.start()
        self.addCleanup(env.stop)
        self.assertNotEqual(os.path.abspath(os.environ["DESK_DB"]), REAL_DB)
        net = mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("network used"))
        self.urlopen = net.start()
        self.addCleanup(net.stop)
        self.outputs, self.full_outputs = [], []
        code, _, err = self.run_script("init_db")
        self.assertEqual(code, 0, err)

    def tearDown(self):
        self.urlopen.assert_not_called()
        for text in self.outputs + self.full_outputs:
            self.assertNotIn(TOKEN, text)
            self.assertNotIn(OTHER_DEST, text)
            self.assertNotIn(OTHER2, text)
        for text in self.outputs:
            self.assertNotIn(OURS, text)

    def run_script(self, name, *args, stdin="", full=False):
        out, err = io.StringIO(), io.StringIO()
        fake_in = io.TextIOWrapper(io.BytesIO(stdin.encode("utf-8")), encoding="utf-8")
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "stdin", fake_in):
            try:
                code = load(name).main([str(a) for a in args])
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        (self.full_outputs if full else self.outputs).extend([out.getvalue(), err.getvalue()])
        return code or 0, out.getvalue(), err.getvalue()

    def acct(self, *args):
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("CLI used")):
            return self.run_script("account", *args, full="--full" in args)

    def acct_ok(self, *args):
        code, out, err = self.acct(*args)
        self.assertEqual(code, 0, err)
        return out.splitlines()

    def acct_json(self, *args):
        code, out, err = self.acct(*args)
        self.assertEqual(code, 0, err)
        try:
            return json.loads(out)
        except ValueError:
            self.fail("account.py %s printed no JSON: %r" % (" ".join(args), out))

    def acct_error(self, *args):
        code, out, err = self.acct(*args)
        self.assertEqual(code, 1, (args, out, err))
        self.assertTrue(err.startswith("error:"), err)
        return err

    def event(self, user, text="hello", ago=120):
        i = next(_ids)
        return {"type": "message", "mode": "active", "timestamp": int((time.time() - ago) * 1000),
                "webhookEventId": "EVK%d" % i, "deliveryContext": {"isRedelivery": False},
                "replyToken": "rt%d" % i, "source": {"type": "user", "userId": user},
                "message": {"id": "lm%d" % i, "type": "text", "text": text}}

    def wrap(self, dest, *events):
        return json.dumps({"destination": dest, "events": list(events)})

    def log_lines(self, body):
        code, out, err = self.run_script("log_event", stdin=body)
        self.assertEqual(code, 0, err)
        return [l for l in out.splitlines() if l.startswith(("inserted ", "duplicate"))]

    def log_one(self, body):
        lines = self.log_lines(body)
        self.assertEqual(len(lines), 1, lines)
        return lines[0]

    def inbound(self, body):
        line = self.log_one(body)
        m = EVENT_LINE.match(line)
        self.assertIsNotNone(m, line)
        return int(m.group(1)), m.group(2), m.group(3)

    def send(self, dest, user=USER, text="hello", ago=120):
        return self.inbound(self.wrap(dest, self.event(user, text, ago)))

    def single(self, user, text="single event"):
        return self.inbound(json.dumps(self.event(user, text)))

    def q(self, sql, *params):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    def count(self, table):
        try:
            return self.q("SELECT COUNT(*) FROM %s" % table)[0][0]
        except sqlite3.OperationalError:
            return 0

    def columns(self, table):
        return [r[1] for r in self.q("PRAGMA table_info(%s)" % table)]

    def row(self, mid):
        rows = self.q("SELECT status, destination FROM messages WHERE id=?", mid)
        self.assertEqual(len(rows), 1, mid)
        return rows[0]

    def statuses(self, *ids):
        return [self.row(i)[0] for i in ids]

    def learned(self):
        rows = self.q("SELECT value, updated_at FROM desk_settings WHERE key='account_destination'")
        self.assertLessEqual(len(rows), 1)
        return rows[0] if rows else None

    def convs(self):
        return sorted(self.q("SELECT * FROM conversations"), key=repr)

    def conv_of(self, user):
        return [r for r in self.convs() if any(isinstance(v, str) and user in v for v in r)]

    def chats(self):
        code, out, err = self.run_script("list_unreplied", "--json")
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def plain(self):
        code, out, err = self.run_script("list_unreplied")
        self.assertEqual(code, 0, err)
        return out

    def match(self, mid):
        code, out, err = self.run_script("keyword_reply", "match", mid, "--json")
        try:
            return json.loads(out)
        except ValueError:
            self.fail("match --json printed no JSON (exit %s): %r %r" % (code, out, err))

    def assert_now_iso(self, text):
        self.assertRegex(text, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
        at = datetime.datetime.fromisoformat(text)
        now = datetime.datetime.now(datetime.timezone.utc)
        self.assertLess(abs((now - at).total_seconds()), 300, text)

    def make_old_db(self, column=True, table=True):
        con = sqlite3.connect(self.db, isolation_level=None)
        try:
            if table:
                con.execute("DROP TABLE desk_settings")
            if column:
                for name, sql in con.execute("SELECT name, sql FROM sqlite_master WHERE type='index'"
                                             " AND tbl_name='messages' AND sql IS NOT NULL").fetchall():
                    if "destination" in sql:
                        con.execute('DROP INDEX "%s"' % name)
                try:
                    con.execute("ALTER TABLE messages DROP COLUMN destination")
                except sqlite3.OperationalError:
                    self._rebuild_without_destination(con)
            con.execute("PRAGMA user_version=0")
        finally:
            con.close()
        if column:
            self.assertNotIn("destination", self.columns("messages"))
        if table:
            self.assertFalse(self.q("SELECT 1 FROM sqlite_master WHERE name='desk_settings'"))

    def _rebuild_without_destination(self, con):
        sql = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'").fetchone()[0]
        sql = re.sub(r'(?i),\s*"?destination"?\s+TEXT\b[^,)]*', "", re.sub(r"--[^\n]*", "", sql))
        extra = [s for (s,) in con.execute("SELECT sql FROM sqlite_master WHERE tbl_name='messages'"
                                          " AND type IN ('index', 'trigger') AND sql IS NOT NULL")]
        cols = ", ".join('"%s"' % c for c in self.columns("messages") if c != "destination")
        con.execute("CREATE TEMP TABLE old_messages AS SELECT %s FROM messages" % cols)
        con.execute("DROP TABLE messages")
        con.execute(sql)
        con.execute("INSERT INTO messages (%s) SELECT %s FROM old_messages" % (cols, cols))
        con.execute("DROP TABLE old_messages")
        for s in extra:
            con.execute(s)

    def test_init_db_adds_destination_and_desk_settings_to_old_db(self):
        self.make_old_db()
        old, account, tail = self.send(OURS, USER, "before the update")
        self.assertEqual((account, tail), ("unknown", None))
        for _ in range(2):
            code, _, err = self.run_script("init_db")
            self.assertEqual(code, 0, err)
            self.assertIn("destination", self.columns("messages"))
            self.assertEqual(set(self.columns("desk_settings")), {"key", "value", "updated_at"})
            self.assertEqual([r[1] for r in self.q("PRAGMA table_info(desk_settings)") if r[5]], ["key"])
            self.assertTrue(self.q("SELECT 1 FROM sqlite_master WHERE type='index' AND name='messages_destination'"))
            self.assertEqual(self.count("messages"), 1)
            self.assertEqual(self.row(old), ("received", None))
            self.assertIsNone(self.learned())
        new, account, tail = self.send(OURS, USER, "after the update")
        self.assertEqual((account, tail), ("learned", "9c8a"))
        self.assertEqual(self.learned()[0], OURS)
        self.assertEqual(self.row(new), ("received", OURS))
        self.assertEqual(self.row(old), ("received", None))

    def test_first_body_learns_the_account(self):
        self.assertIsNone(self.learned())
        lines = self.log_lines(self.wrap(OURS, self.event(USER, "first"), self.event(USER2, "second")))
        self.assertEqual(len(lines), 2, lines)
        ids = []
        for line in lines:
            m = EVENT_LINE.match(line)
            self.assertIsNotNone(m, line)
            self.assertEqual(m.group(2, 3), ("learned", "9c8a"), line)
            ids.append(int(m.group(1)))
        value, updated_at = self.learned()
        self.assertEqual(value, OURS)
        self.assert_now_iso(updated_at)
        for mid in ids:
            self.assertEqual(self.row(mid), ("received", OURS))
        self.assertTrue(self.conv_of(USER))
        mid, account, tail = self.send(OURS, USER, "again")
        self.assertEqual((account, tail), ("same", "9c8a"))
        self.assertEqual(self.row(mid), ("received", OURS))
        self.assertEqual(self.learned(), (value, updated_at))

    def test_other_destination_is_recorded_but_opens_no_conversation(self):
        self.send(OURS, USER, "ours")
        convs = self.convs()
        line = self.log_one(self.wrap(OTHER_DEST, self.event(USER2, "zz-other")))
        self.assertRegex(line, r" account=other dest=b7e2 user_id=\S+$")
        mid = int(EVENT_LINE.match(line).group(1))
        self.assertEqual(self.row(mid), ("other_account", OTHER_DEST))
        if "raw" in self.columns("messages"):
            self.assertIn("zz-other", self.q("SELECT raw FROM messages WHERE id=?", mid)[0][0] or "")
        self.assertEqual(self.conv_of(USER2), [])
        self.assertEqual(self.convs(), convs)
        mid, account, tail = self.send(OTHER_DEST, USER, "zz-other-2")
        self.assertEqual((account, tail), ("other", "b7e2"))
        self.assertEqual(self.row(mid), ("other_account", OTHER_DEST))
        self.assertEqual(self.convs(), convs)
        self.assertEqual(self.learned()[0], OURS)

    def test_verify_body_and_single_event_learn_nothing(self):
        code, out, err = self.run_script("log_event", stdin=json.dumps({"destination": OURS, "events": []}))
        self.assertEqual(code, 0, err)
        self.assertIn("no events", out + err)
        self.assertIsNone(self.learned())
        self.assertEqual(self.count("messages"), 0)
        mid, account, tail = self.single(USER)
        self.assertEqual((account, tail), ("unknown", None))
        self.assertEqual(self.row(mid), ("received", None))
        self.assertIsNone(self.learned())
        _, account, _ = self.send(OURS, USER2)
        self.assertEqual(account, "learned")
        mid, account, tail = self.single(USER3)
        self.assertEqual((account, tail), ("unknown", None))
        self.assertEqual(self.row(mid), ("received", None))
        self.assertEqual(self.learned()[0], OURS)

    def test_invalid_destination_is_never_learned(self):
        bodies = [self.wrap(dest, self.event(USER, "bad destination")) for dest in BAD_DESTS]
        bodies.append(json.dumps({"events": [self.event(USER, "no destination")]}))
        for body in bodies:
            with self.subTest(body=body):
                mid, account, tail = self.inbound(body)
                self.assertEqual((account, tail), ("unknown", None))
                self.assertEqual(self.row(mid), ("received", None))
                self.assertIsNone(self.learned())
        _, account, _ = self.send(OURS, USER2)
        self.assertEqual(account, "learned")
        for dest in ("Uxyz", "U" + "1" * 28 + "B7E2"):
            mid, account, tail = self.send(dest, USER3)
            self.assertEqual((account, tail), ("unknown", None))
            self.assertEqual(self.row(mid), ("received", None))
        self.assertEqual(self.learned()[0], OURS)

    def test_redelivered_duplicate_prints_the_account_of_its_body(self):
        ours = self.event(USER, "first try")
        self.assertEqual(self.inbound(self.wrap(OURS, ours))[1], "learned")
        other = self.event(USER2, "zz-other")
        self.assertEqual(self.inbound(self.wrap(OTHER_DEST, other))[1], "other")
        n = self.count("messages")
        for ev, dest, expect in ((ours, OURS, "same"), (other, OTHER_DEST, "other")):
            again = json.loads(json.dumps(ev))
            again["deliveryContext"]["isRedelivery"] = True
            line = self.log_one(self.wrap(dest, again))
            self.assertTrue(line.startswith("duplicate"), line)
            self.assertRegex(line, r"\baccount=%s\b" % expect)
        self.assertEqual(self.count("messages"), n)

    def test_learning_happens_inside_the_write_transaction(self):
        ready, done = threading.Event(), {}

        def other_webhook():
            con = sqlite3.connect(self.db, timeout=10, isolation_level=None)
            try:
                con.execute("BEGIN IMMEDIATE")
                con.execute("INSERT INTO desk_settings (key, value, updated_at)"
                            " VALUES ('account_destination', ?, ?)", (OTHER_DEST, now_iso()))
                ready.set()
                time.sleep(0.5)
                con.execute("COMMIT")
                done["ok"] = True
            finally:
                ready.set()
                con.close()

        holder = threading.Thread(target=other_webhook)
        holder.start()
        self.assertTrue(ready.wait(10))
        line = self.log_one(self.wrap(OURS, self.event(USER, "at the same time")))
        holder.join(30)
        self.assertTrue(done.get("ok"), "the other writer could not commit")
        m = EVENT_LINE.match(line)
        self.assertIsNotNone(m, line)
        self.assertEqual(m.group(2, 3), ("other", "9c8a"), line)
        self.assertEqual(self.row(int(m.group(1))), ("other_account", OURS))
        self.assertEqual(self.learned()[0], OTHER_DEST)

    def test_keyword_match_skips_other_account_rows(self):
        code, _, err = self.run_script("keyword_reply", "add", KEYWORD, "--reply", "-",
                                       stdin="営業時間は10時から18時です")
        self.assertEqual(code, 0, err)
        code, _, err = self.run_script("keyword_reply", "setting", "off")
        self.assertEqual(code, 0, err)
        same1, account, _ = self.send(OURS, USER, KEYWORD, ago=5)
        self.assertEqual(account, "learned")
        other, account, _ = self.send(OTHER_DEST, USER2, KEYWORD, ago=5)
        self.assertEqual(account, "other")
        same2, account, _ = self.send(OURS, USER3, KEYWORD, ago=5)
        self.assertEqual(account, "same")
        self.assertEqual(self.match(same1)["action"], "suggest")
        code, _, err = self.run_script("keyword_reply", "setting", "on")
        self.assertEqual(code, 0, err)
        before = self.count("keyword_matches")
        data = self.match(other)
        self.assertEqual((data["action"], data["reason"]), ("none", "other_account"))
        self.assertEqual(self.count("keyword_matches"), before)
        self.assertEqual(self.match(same2)["action"], "auto_send")

    def test_list_unreplied_ignores_other_account_rows(self):
        self.send(OURS, USER, "zz-ours")
        _, account, _ = self.send(OTHER_DEST, USER2, "zz-other-only")
        self.assertEqual(account, "other")
        chats = self.chats()
        self.assertEqual(len(chats), 1, chats)
        text = json.dumps(chats, ensure_ascii=False)
        self.assertIn(USER, text)
        self.assertNotIn(USER2, text)
        self.assertNotIn("zz-other-only", text)
        counts = {k: v for k, v in chats[0].items() if "count" in k}
        _, account, _ = self.send(OTHER_DEST, USER, "zz-other-same-user")
        self.assertEqual(account, "other")
        chats = self.chats()
        self.assertEqual(len(chats), 1, chats)
        text = json.dumps(chats, ensure_ascii=False)
        self.assertIn(USER, text)
        self.assertNotIn("zz-other", text)
        self.assertEqual({k: v for k, v in chats[0].items() if "count" in k}, counts)
        plain = self.plain()
        self.assertNotIn("e5f6", plain)
        self.assertNotIn("zz-other", plain)

    def test_account_show(self):
        lines = self.acct_ok("show")
        self.assertEqual(lines[0], NOT_LEARNED)
        self.assertIn("other: none", lines)
        data = self.acct_json("show", "--json")
        self.assertEqual(set(data), SHOW_KEYS)
        self.assertEqual((data["learned"], data["dest_tail"], data["updated_at"], data["other"]),
                         (False, None, None, []))
        self.assertIsInstance(data["rows"], int)
        self.send(OURS, USER, "one")
        self.send(OURS, USER, "two")
        self.send(OTHER_DEST, USER2, "old other", ago=600)
        time.sleep(1.1)
        self.send(OTHER2, USER3, "new other", ago=60)
        self.send(OTHER2, USER3, "newest other", ago=30)
        value, updated_at = self.learned()
        data = self.acct_json("show", "--json")
        self.assertEqual(set(data), SHOW_KEYS)
        self.assertEqual((data["learned"], data["dest_tail"], data["updated_at"], data["rows"]),
                         (True, "9c8a", updated_at, 2))
        self.assertEqual([(o["dest_tail"], o["rows"]) for o in data["other"]], [("d4c3", 2), ("b7e2", 1)])
        for o in data["other"]:
            self.assertEqual(set(o), {"dest_tail", "rows", "last_at"})
            self.assertIsInstance(o["last_at"], str)
        self.assertEqual(self.acct_ok("show"), [
            "account: 9c8a (learned %s)" % updated_at,
            "rows: 2 inbound rows for this account",
            "other: d4c3 rows=2 last_at=%s" % data["other"][0]["last_at"],
            "other: b7e2 rows=1 last_at=%s" % data["other"][1]["last_at"],
        ])

    def test_account_show_full(self):
        self.send(OURS, USER)
        self.send(OTHER_DEST, USER2)
        self.assertNotIn("destination", self.acct_json("show", "--json"))
        self.assertFalse([l for l in self.acct_ok("show") if l.startswith("destination:")])
        lines = self.acct_ok("show", "--full")
        self.assertIn("destination: " + OURS, lines)
        self.assertIn("account: 9c8a (learned %s)" % self.learned()[1], lines)
        data = self.acct_json("show", "--json", "--full")
        self.assertEqual(set(data), SHOW_KEYS | {"destination"})
        self.assertEqual((data["destination"], data["dest_tail"]), (OURS, "9c8a"))

    def test_account_set(self):
        self.assertEqual(self.acct_ok("set", OURS), ["saved account dest=9c8a"])
        value, updated_at = self.learned()
        self.assertEqual(value, OURS)
        self.assert_now_iso(updated_at)
        for bad in ("Uxyz", "U" + "0" * 28 + "9C8A", "U" + "0" * 27 + "9c8a", "C" + "0" * 28 + "9c8a"):
            with self.subTest(bad=bad):
                self.acct_error("set", bad)
                self.assertEqual(self.learned(), (value, updated_at))
        ours, account, _ = self.send(OURS, USER)
        self.assertEqual(account, "same")
        other, account, _ = self.send(OTHER_DEST, USER2)
        self.assertEqual(account, "other")
        self.assertEqual(self.acct_ok("set", OTHER_DEST), [
            "saved account dest=b7e2 (was 9c8a)",
            "rows: 1 other_account rows now match, 1 received rows now differ (use --relabel to update them)",
        ])
        self.assertEqual(self.learned()[0], OTHER_DEST)
        self.assertEqual(self.statuses(ours, other), ["received", "other_account"])
        self.assertEqual(self.send(OURS, USER3)[1:], ("other", "9c8a"))
        self.assertEqual(self.send(OTHER_DEST, USER3)[1:], ("same", "b7e2"))

    def test_account_set_relabel(self):
        a, _, _ = self.send(OURS, USER, "one")
        b, _, _ = self.send(OURS, USER, "two")
        c, _, _ = self.send(OTHER_DEST, USER2, "three")
        self.assertIn("rows: 1 other_account rows now match, 2 received rows now differ "
                      "(use --relabel to update them)", self.acct_ok("set", OTHER_DEST))
        self.assertEqual(self.statuses(a, b, c), ["received", "received", "other_account"])
        d, account, _ = self.single(USER3)
        self.assertEqual(account, "unknown")
        convs = self.convs()
        self.assertEqual(self.acct_ok("set", OTHER_DEST, "--relabel"),
                         ["saved account dest=b7e2", "relabel: 1 -> received, 2 -> other_account"])
        self.assertEqual(self.statuses(a, b, c, d), ["other_account", "other_account", "received", "received"])
        self.assertEqual(self.learned()[0], OTHER_DEST)
        self.assertEqual(self.acct_ok("set", OURS, "--relabel"),
                         ["saved account dest=9c8a (was b7e2)", "relabel: 2 -> received, 1 -> other_account"])
        self.assertEqual(self.statuses(a, b, c, d), ["received", "received", "other_account", "received"])
        self.assertEqual(self.row(d), ("received", None))
        self.assertEqual(self.acct_ok("set", OURS), ["saved account dest=9c8a"])
        self.assertEqual(self.convs(), convs)

    def test_account_set_from_message(self):
        ours, _, _ = self.send(OURS, USER)
        other, _, _ = self.send(OTHER_DEST, USER2)
        single, _, _ = self.single(USER3)
        for mid in (999999, single):
            with self.subTest(message=mid):
                self.acct_error("set", "--from-message", mid)
                self.assertEqual(self.learned()[0], OURS)
        self.assertIn("saved account dest=b7e2 (was 9c8a)", self.acct_ok("set", "--from-message", other))
        self.assertEqual(self.learned()[0], OTHER_DEST)
        self.assertEqual(self.statuses(ours, other, single), ["received", "other_account", "received"])
        self.assertEqual(self.acct_ok("set", "--from-message", ours, "--relabel"),
                         ["saved account dest=9c8a (was b7e2)", "relabel: 0 -> received, 0 -> other_account"])
        self.assertEqual(self.learned()[0], OURS)
        self.assertEqual(self.statuses(ours, other, single), ["received", "other_account", "received"])

    def test_account_forget_then_next_body_learns_again(self):
        ours, _, _ = self.send(OURS, USER)
        other, _, _ = self.send(OTHER_DEST, USER2)
        self.assertEqual(self.acct_ok("forget"), ["would forget account dest=9c8a (run with --yes)"])
        self.assertEqual(self.learned()[0], OURS)
        self.assertEqual(self.acct_ok("forget", "--yes"),
                         ["forgot account dest=9c8a; the next webhook with a destination sets it again"])
        self.assertIsNone(self.learned())
        self.assertEqual(self.statuses(ours, other), ["received", "other_account"])
        self.assertTrue(self.acct_ok("forget")[0].startswith("account: not learned yet"))
        self.assertEqual(self.acct_ok("show")[0], NOT_LEARNED)
        data = self.acct_json("show", "--json")
        self.assertEqual((data["learned"], data["dest_tail"], data["updated_at"]), (False, None, None))
        self.assertEqual([(o["dest_tail"], o["rows"]) for o in data["other"]], [("b7e2", 1)])
        mid, account, tail = self.send(OTHER_DEST, USER3)
        self.assertEqual((account, tail), ("learned", "b7e2"))
        self.assertEqual(self.learned()[0], OTHER_DEST)
        self.assertEqual(self.row(mid), ("received", OTHER_DEST))
        self.assertEqual(self.statuses(ours, other), ["received", "other_account"])

    def test_old_db_log_event_works_as_before(self):
        self.make_old_db()
        for dest, user in ((OURS, USER), (OTHER_DEST, USER2)):
            line = self.log_one(self.wrap(dest, self.event(user, "old db")))
            m = EVENT_LINE.match(line)
            self.assertIsNotNone(m, line)
            self.assertEqual(m.group(2), "unknown", line)
            self.assertNotIn(" dest=", line)
            self.assertEqual(self.q("SELECT status FROM messages WHERE id=?", int(m.group(1))), [("received",)])
        self.assertEqual(self.count("messages"), 2)
        self.assertTrue(self.conv_of(USER2))
        self.assertNotIn("destination", self.columns("messages"))
        self.assertFalse(self.q("SELECT 1 FROM sqlite_master WHERE name='desk_settings'"))
        for args in (("show",), ("show", "--json", "--full"), ("set", OURS),
                     ("set", "--from-message", "1"), ("forget", "--yes")):
            with self.subTest(args=args):
                self.assertIn("init_db", self.acct_error(*args))

    def test_account_needs_init_db(self):
        missing = os.path.join(self.dir, "none.db")
        with mock.patch.dict(os.environ, {"DESK_DB": missing}):
            self.assertIn("init_db", self.acct_error("show"))
        self.make_old_db(column=False)
        self.assertIn("init_db", self.acct_error("show"))
        self.assertIn("init_db", self.acct_error("set", OURS))
        self.assertEqual(self.send(OURS, USER)[1], "unknown")
        code, _, err = self.run_script("init_db")
        self.assertEqual(code, 0, err)
        self.make_old_db(table=False)
        self.assertIn("init_db", self.acct_error("show", "--json"))
        self.assertIn("init_db", self.acct_error("forget", "--yes"))
        self.assertEqual(self.send(OURS, USER2)[1:], ("unknown", None))
        self.assertIsNone(self.learned())


if __name__ == "__main__":
    unittest.main()
