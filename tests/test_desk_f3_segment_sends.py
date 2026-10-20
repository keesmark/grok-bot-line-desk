import contextlib
import importlib.util
import io
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
import time
import unittest
import urllib.request
from unittest import mock

sys.dont_write_bytecode = True
SCRIPTS = os.environ.get("SCRIPTS") or os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts"))
REAL_DB = "/workspace/line-inbox/messages.db"
TOKEN = "test-token-SECRET123"
FUTURE, PAST = "2999-01-01", "2000-01-01"
_modules = {}


def load(name):
    if name not in _modules:
        spec = importlib.util.spec_from_file_location("f3_" + name[:-3], os.path.join(SCRIPTS, name))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _modules[name] = mod
    return _modules[name]


def real_db_state():
    try:
        st = os.stat(REAL_DB)
        return st.st_mtime_ns, st.st_size
    except FileNotFoundError:
        return None


def unsafe_db(path):
    return not path or os.path.realpath(path) == os.path.realpath(REAL_DB)


class F3(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.db = os.path.join(self.dir, "messages.db")
        env = mock.patch.dict(os.environ, {"DESK_DB": self.db, "LINE_CHANNEL_ACCESS_TOKEN": TOKEN})
        env.start()
        self.addCleanup(env.stop)
        self.assertFalse(unsafe_db(os.environ.get("DESK_DB")), "DESK_DB must never be the real DB")
        self.events = 0
        self.run_ok("init_db.py")
        self.assertEqual(stat.S_IMODE(os.stat(self.db).st_mode), 0o600)

    def call(self, script, *argv, stdin=b""):
        self.assertFalse(unsafe_db(os.environ.get("DESK_DB")), "DESK_DB must be a temp DB")
        mod = load(script)
        fake = mock.Mock(side_effect=AssertionError("network access in a test"))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(getattr(mod, "urllib", urllib).request, "urlopen", fake), \
                mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin), encoding="utf-8")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = mod.main(list(argv))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        fake.assert_not_called()
        for text in (out.getvalue(), err.getvalue()):
            self.assertNotIn(TOKEN, text)
        return code or 0, out.getvalue(), err.getvalue()

    def run_ok(self, script, *argv, stdin=b""):
        code, out, err = self.call(script, *argv, stdin=stdin)
        self.assertEqual(code, 0, f"{script} {' '.join(argv)} failed: {err}")
        return out

    def q(self, sql, *params):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    def count(self, table, where="1", *params):
        return self.q(f"SELECT COUNT(*) FROM {table} WHERE {where}", *params)[0][0]

    def inbound(self, uid, minutes_ago=60):
        self.events += 1
        ev = {"type": "message", "mode": "active", "timestamp": int((time.time() - minutes_ago * 60) * 1000),
              "source": {"type": "user", "userId": uid}, "webhookEventId": f"ev{self.events}",
              "deliveryContext": {"isRedelivery": False},
              "message": {"id": f"m{self.events}", "type": "text", "text": f"hello {self.events}"}}
        body = json.dumps({"destination": "Ubot", "events": [ev]}).encode()
        self.assertIn("inserted", self.run_ok("log_event.py", stdin=body))

    def sent(self, *args):
        path = os.path.join(self.dir, "text.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("お知らせです\n")
        out = self.run_ok("log_event.py", "--out", *args, "--text-file", path)
        m = re.search(r"inserted (\d+)", out)
        self.assertTrue(m, out)
        return int(m.group(1))

    def push(self, uid):
        return self.sent("--to", uid)

    def segment(self, *uids):
        return self.sent(*[a for u in uids for a in ("--to", u)])

    def broadcast(self):
        return self.sent("--broadcast", "--link-known")

    def recipients(self, mid):
        return sorted(r[0] for r in self.q("SELECT user_id FROM send_recipients WHERE message_id = ?", mid))

    def waiting(self):
        return {c["chat_key"] for c in json.loads(self.run_ok("list_unreplied.py", "--json"))}

    def purge(self, *argv):
        out = self.run_ok("purge_old.py", *argv)
        return {t: int(n) for t, n in re.findall(r"^(\w+): (\d+)$", out, re.M)}

    def test_broadcast_is_not_a_reply_but_push_is(self):
        self.inbound("U1")
        self.assertEqual(self.waiting(), {"U1"})
        b = self.broadcast()
        self.assertEqual(self.q("SELECT send_kind, user_id FROM messages WHERE id = ?", b), [("broadcast", None)])
        self.assertEqual(self.q("SELECT user_id, known_at_send FROM send_recipients WHERE message_id = ?", b),
                         [("U1", 1)])
        self.assertEqual(self.waiting(), {"U1"})
        self.push("U1")
        self.assertEqual(self.waiting(), set())

    def test_segment_is_not_a_reply_but_push_is(self):
        self.inbound("U1")
        self.inbound("U2")
        s = self.segment("U1", "U2")
        self.assertEqual(self.q("SELECT send_kind, user_id FROM messages WHERE id = ?", s), [("segment", None)])
        self.assertEqual(self.recipients(s), ["U1", "U2"])
        self.assertEqual(self.waiting(), {"U1", "U2"})
        self.push("U1")
        self.assertEqual(self.waiting(), {"U2"})

    def test_stats_outbound_by_kind(self):
        self.inbound("U1")
        self.inbound("U2")
        self.push("U1")
        self.push("U1")
        self.segment("U1", "U2")
        self.broadcast()
        r = json.loads(self.run_ok("stats.py", "--json"))
        kinds = r["outbound_by_kind"]
        self.assertEqual({k: kinds.get(k) for k in ("push", "segment", "broadcast")},
                         {"push": 2, "segment": 1, "broadcast": 1})
        self.assertEqual(r["outbound_total"], 4)
        self.assertEqual(r["unreplied_user"], 1)

    def test_purge_user_keeps_bulk_rows_and_other_recipients(self):
        self.inbound("U1")
        self.inbound("U2")
        b, s = self.broadcast(), self.segment("U1", "U2")
        p1, p2 = self.push("U1"), self.push("U2")
        u1_rows = self.count("send_recipients", "user_id = 'U1'")
        self.assertEqual(u1_rows, 3)
        self.assertEqual(self.purge("--user", "U1").get("send_recipients"), u1_rows)
        self.assertEqual(self.count("send_recipients"), 6)
        self.assertEqual(self.purge("--user", "U1", "--yes").get("send_recipients"), u1_rows)
        self.assertEqual(self.count("send_recipients", "user_id = 'U1'"), 0)
        self.assertEqual(sorted(r[0] for r in self.q("SELECT message_id FROM send_recipients WHERE user_id = 'U2'")),
                         sorted([b, s, p2]))
        self.assertEqual(self.count("messages", "id IN (?, ?)", b, s), 2)
        self.assertEqual(self.count("messages", "id = ? OR user_id = 'U1'", p1), 0)
        self.assertEqual(self.count("messages", "user_id = 'U2'"), 2)
        self.assertEqual(self.count("conversations", "chat_key = 'U1'"), 0)

    def test_purge_before_deletes_recipients_of_deleted_messages(self):
        self.inbound("U1")
        self.inbound("U2")
        self.broadcast()
        self.segment("U1", "U2")
        self.push("U1")
        total = self.count("send_recipients")
        self.assertEqual(total, 5)
        dry = self.purge("--before", FUTURE)
        self.assertEqual(dry.get("send_recipients"), total)
        self.assertEqual(dry.get("messages"), 5)
        self.assertEqual(self.purge("--before", PAST).get("send_recipients"), 0)
        self.assertEqual(self.count("send_recipients"), total)
        done = self.purge("--before", FUTURE, "--yes")
        self.assertEqual(done.get("send_recipients"), total)
        self.assertEqual(self.count("send_recipients"), 0)
        self.assertEqual(self.count("messages"), 0)
        self.assertEqual(stat.S_IMODE(os.stat(self.db).st_mode), 0o600)


def main():
    before = real_db_state()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(F3))
    untouched = real_db_state() == before
    if not untouched:
        print(f"error: {REAL_DB} changed during the run", file=sys.stderr)
    print(f"test_f3: {result.testsRun} run, {len(result.failures)} failed, {len(result.errors)} errors, "
          f"real DB {'untouched' if untouched else 'CHANGED'}")
    return 0 if result.wasSuccessful() and untouched else 1


if __name__ == "__main__":
    sys.exit(main())
