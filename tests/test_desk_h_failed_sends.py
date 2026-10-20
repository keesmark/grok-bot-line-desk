import contextlib
import importlib.util
import io
import itertools
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import unittest
import urllib.request
from unittest import mock

sys.dont_write_bytecode = True
SCRIPTS = os.environ.get("SCRIPTS") or os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts"))
REAL_DB = "/workspace/line-inbox/messages.db"
TOKEN = "test-token-SECRET123"
LIMIT_ERROR = ("MCP error -32602: Input validation error: Invalid arguments for tool push_text_message: "
               "String must contain at most 5000 character(s) at message.text")
_mods, _ids = {}, itertools.count(1)


def load(name):
    if name not in _mods:
        spec = importlib.util.spec_from_file_location("h_" + name, os.path.join(SCRIPTS, name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _mods[name] = mod
    return _mods[name]


def real_db_stat():
    try:
        st = os.stat(REAL_DB)
    except FileNotFoundError:
        return None
    return st.st_mtime_ns, st.st_size


class NoMarkRead(types.ModuleType):

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        raise AssertionError("mark_read used: " + name)


class SendErrorTest(unittest.TestCase):
    def setUp(self):
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
        self.outputs = []
        code, _, err = self.run_script("init_db")
        self.assertEqual(code, 0, err)

    def tearDown(self):
        self.urlopen.assert_not_called()
        for text in self.outputs:
            self.assertNotIn(TOKEN, text)

    def run_script(self, name, *args, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        fake_in = io.TextIOWrapper(io.BytesIO(stdin.encode("utf-8")), encoding="utf-8")
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "stdin", fake_in):
            try:
                code = load(name).main([str(a) for a in args])
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        self.outputs += [out.getvalue(), err.getvalue()]
        return code or 0, out.getvalue(), err.getvalue()

    def kw(self, *args, stdin=""):
        return self.run_script("keyword_reply", *args, stdin=stdin)

    def add_rule(self, keyword, reply, match="exact"):
        code, out, err = self.kw("add", keyword, "--reply", "-", "--match", match, "--json", stdin=reply)
        self.assertEqual(code, 0, err)
        return json.loads(out)["id"]

    def body(self, user, text=None, mtype="text", source="user"):
        i = next(_ids)
        msg = {"id": "lm%d" % i, "type": mtype}
        msg.update({"text": text} if mtype == "text" else {"packageId": "1", "stickerId": "2"})
        src = {"type": source, "userId": user}
        if source == "group":
            src["groupId"] = "Cgroup1"
        ev = {"type": "message", "mode": "active", "timestamp": int((time.time() - 120) * 1000),
              "webhookEventId": "EV%d" % i, "deliveryContext": {"isRedelivery": False},
              "replyToken": "rt%d" % i, "source": src, "message": msg}
        return json.dumps({"destination": "Ubot", "events": [ev]})

    def inbound(self, user, text=None, mtype="text", source="user"):
        code, out, err = self.run_script("log_event", stdin=self.body(user, text, mtype, source))
        self.assertEqual(code, 0, err)
        return int(re.search(r"inserted (\d+)", out).group(1))

    def log_out(self, *args, stdin="hello"):
        return self.run_script("log_event", "--out", "--text", "-", *args, stdin=stdin)

    def logged(self, out):
        rows = self.q("SELECT * FROM messages WHERE id=?", int(re.search(r"inserted (\d+)", out).group(1)))
        self.assertEqual(len(rows), 1)
        return rows[0]

    def send_error(self, row):
        raw = json.loads(row["raw"])
        self.assertEqual(list(raw), ["send_error"])
        return raw["send_error"]

    def match(self, mid, *args):
        code, out, err = self.kw("match", mid, "--json", *args)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def decisions(self, mid):
        for t in self.q("SELECT name FROM sqlite_master WHERE type='table'"):
            cols = {c["name"] for c in self.q('PRAGMA table_info("%s")' % t["name"])}
            if {"decision", "message_id"} <= cols:
                return [r["decision"] for r in self.q(
                    'SELECT decision FROM "%s" WHERE message_id=? ORDER BY rowid' % t["name"], mid)]
        self.fail("no table with decision and message_id columns")

    def pending(self):
        code, out, err = self.kw("pending", "--json")
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def pending_ids(self):
        return [int(p["message_id"]) for p in self.pending()]

    def unreplied(self):
        code, out, err = self.run_script("list_unreplied", "--json")
        self.assertEqual(code, 0, err)
        return [c["chat_key"] for c in json.loads(out)]

    def q(self, sql, *params):
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql, params)]
        finally:
            con.close()

    def count(self, table, where="1", *params):
        return self.q("SELECT COUNT(*) AS n FROM %s WHERE %s" % (table, where), *params)[0]["n"]

    def set_on(self):
        self.assertEqual(self.kw("setting", "on")[0], 0)
        self.assertEqual(os.stat(os.path.join(self.dir, "keyword.json")).st_mode & 0o777, 0o600)

    def read(self, path):
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with open(path, encoding="utf-8", newline="") as f:
            return f.read()

    def test_error_is_cleaned_and_failed_is_not_a_reply(self):
        self.inbound("U1", "こんにちは")
        error = "Error:\n  line two\tBearer abc123 token=%s\n" % TOKEN
        code, out, err = self.log_out("--to", "U1", "--status", "failed", "--error", error, stdin="長い本文")
        self.assertEqual(code, 0, err)
        row = self.logged(out)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["user_id"], "U1")
        self.assertNotIn(TOKEN, row["raw"])
        sent_error = self.send_error(row)
        self.assertNotIn("\n", sent_error)
        self.assertNotIn("abc123", sent_error)
        self.assertIn("***", sent_error)
        self.assertIn("Bearer ***", sent_error)
        self.assertEqual(sent_error, "Error: line two Bearer *** token=***")
        self.assertIn("U1", self.unreplied())
        code, out, err = self.run_script("stats", "--days", "1", "--json")
        self.assertEqual(code, 0, err)
        by_status = json.loads(out)["outbound_by_status"]
        self.assertEqual(by_status.get("failed"), 1)
        self.assertFalse(by_status.get("sent"))

    def test_error_is_cut_and_must_not_be_empty(self):
        code, out, err = self.log_out("--to", "U1", "--status", "failed", "--error", "E" * 500)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.send_error(self.logged(out)), "E" * 300 + "...")
        code, out, err = self.log_out("--to", "U1", "--status", "failed", "--error", "F" * 300)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.send_error(self.logged(out)), "F" * 300)
        code, out, err = self.log_out("--to", "U1", "--status", "failed", "--error", "a" + "\n" * 1000 + "b")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.send_error(self.logged(out)), "a b")
        n = self.count("messages")
        code, out, err = self.log_out("--to", "U1", "--status", "failed", "--error", " \n\t ")
        self.assertNotEqual(code, 0, out)
        self.assertIn("--error", err)
        self.assertEqual(self.count("messages"), n)
        with mock.patch.dict(os.environ, {"LINE_CHANNEL_ACCESS_TOKEN": ""}):
            code, out, err = self.log_out("--to", "U1", "--status", "failed", "--error", "plain  error")
            self.assertEqual(code, 0, err)
            self.assertEqual(self.send_error(self.logged(out)), "plain error")
            del os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
            code, out, err = self.log_out("--to", "U1", "--status", "failed", "--error", "plain error")
            self.assertEqual(code, 0, err)
            self.assertEqual(self.send_error(self.logged(out)), "plain error")

    def test_error_rejected_for_other_statuses_and_without_out(self):
        for extra in (["--status", "sent"], [], ["--status", "app_reply"], ["--status", "auto_reply"]):
            with self.subTest(status=extra):
                code, out, err = self.log_out("--to", "U1", *extra, "--error", "x")
                self.assertNotEqual(code, 0, out)
                self.assertIn("error", err)
                self.assertEqual(self.count("messages"), 0)
        body = self.body("U1", "こんにちは")
        code, out, err = self.run_script("log_event", "--error", "x", stdin=body)
        self.assertNotEqual(code, 0, out)
        self.assertEqual(self.count("messages"), 0)
        code, out, err = self.run_script("log_event", stdin=body)
        self.assertEqual(code, 0, err)
        code, out, err = self.log_out("--to", "U1")
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.logged(out)["raw"])
        self.assertEqual(self.count("messages"), 2)

    def test_broadcast_failed_and_send_unknown(self):
        code, out, err = self.log_out("--broadcast", "--status", "failed", "--error", "x", stdin="お知らせ")
        self.assertEqual(code, 0, err)
        row = self.logged(out)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(self.send_error(row), "x")
        if "send_kind" in row:
            self.assertEqual(row["send_kind"], "broadcast")
        code, out, err = self.log_out("--to", "U2", "--status", "send_unknown", "--error", "タイムアウト\n(応答なし)")
        self.assertEqual(code, 0, err)
        row = self.logged(out)
        self.assertEqual(row["status"], "send_unknown")
        self.assertEqual(self.send_error(row), "タイムアウト (応答なし)")
        self.assertIn("タイムアウト", row["raw"])

    def test_keyword_auto_send_refused(self):
        reply = "営業時間は10時から19時です。\n定休日は水曜です。"
        self.set_on()
        self.add_rule("営業時間", reply)
        mid = self.inbound("U1", "営業時間")
        self.assertEqual(self.match(mid)["action"], "auto_send")
        code, _, err = self.kw("sent", mid, "--result", "failed", "--error", LIMIT_ERROR)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.decisions(mid), ["auto_failed"])
        rows = self.q("SELECT * FROM messages WHERE status='auto_reply_failed'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user_id"], "U1")
        self.assertEqual(rows[0]["text"], reply)
        self.assertEqual(self.send_error(rows[0]), LIMIT_ERROR)
        self.assertEqual(self.count("messages", "status IN ('sent', 'auto_reply')"), 0)
        self.assertIn("U1", self.unreplied())
        found = [p for p in self.pending() if int(p["message_id"]) == mid]
        self.assertEqual(len(found), 1)
        self.assertTrue({"match_id", "message_id", "user_id", "rule_id", "keyword", "reason",
                         "reply_text", "matched_at"} <= set(found[0]))
        self.assertEqual(found[0]["reason"], "auto_failed")
        self.assertEqual(found[0]["user_id"], "U1")
        self.assertEqual(found[0]["keyword"], "営業時間")
        self.assertEqual(found[0]["reply_text"], reply)
        path = os.path.join(self.dir, "reply.txt")
        code, _, err = self.kw("text", mid, "--out", path)
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertEqual(self.read(path), reply)

    def test_keyword_sent_error_rules(self):
        self.set_on()
        self.add_rule("住所", "東京都千代田区1-1")
        mid = self.inbound("U1", "住所")
        self.assertEqual(self.match(mid)["action"], "auto_send")
        before = self.count("messages")
        code, _, err = self.kw("sent", mid, "--result", "sent", "--error", "x")
        self.assertEqual(code, 2, err)
        self.assertEqual(self.count("messages"), before)
        self.assertEqual(self.decisions(mid), ["auto_pending"])
        code, _, err = self.kw("sent", mid, "--result", "unknown", "--error", "timeout")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.decisions(mid), ["auto_unknown"])
        rows = self.q("SELECT * FROM messages WHERE status='auto_reply_unknown'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.send_error(rows[0]), "timeout")
        self.assertEqual(self.count("messages"), before + 1)
        self.assertNotIn(mid, self.pending_ids())
        _, _, err = self.kw("text", mid, "--out", os.path.join(self.dir, "reply.txt"))
        self.assertIn("warning", err)

    def test_keyword_error_starting_with_dash(self):
        self.set_on()
        self.add_rule("dash", "Reply")
        mid = self.inbound("U9", "dash")
        self.assertEqual(self.match(mid)["action"], "auto_send")
        code, _, err = self.kw("sent", mid, "--result", "failed", "--error=-32602 Input validation error")
        self.assertEqual(code, 0, err)
        raw = self.q("SELECT raw FROM messages WHERE direction='out'")[0]["raw"]
        self.assertEqual(json.loads(raw)["send_error"], "-32602 Input validation error")

    def test_pending_auto_failed_cleared_by_later_push(self):
        self.set_on()
        self.add_rule("予約", "予約はこちらからどうぞ")
        mid = self.inbound("U1", "予約")
        self.assertEqual(self.match(mid)["action"], "auto_send")
        code, _, err = self.kw("sent", mid, "--result", "failed", "--error", LIMIT_ERROR)
        self.assertEqual(code, 0, err)
        self.assertIn(mid, self.pending_ids())
        code, _, err = self.log_out("--to", "U1", "--status", "failed", "--error", "too long again")
        self.assertEqual(code, 0, err)
        code, _, err = self.log_out("--to", "U2")
        self.assertEqual(code, 0, err)
        self.assertIn(mid, self.pending_ids())
        code, _, err = self.log_out("--to", "U1", "--status", "sent", stdin="手動でお返事しました")
        self.assertEqual(code, 0, err)
        self.assertNotIn(mid, self.pending_ids())

    def test_no_network_no_mark_read_no_resend(self):
        calls, real_popen = [], subprocess.Popen

        def popen(args, *a, **k):
            calls.append(args if isinstance(args, str) else " ".join(map(str, args)))
            return real_popen(args, *a, **k)

        with mock.patch.object(socket, "create_connection", side_effect=AssertionError("network used")) as conn, \
                mock.patch.object(subprocess, "Popen", side_effect=popen), \
                mock.patch.dict(sys.modules, {"mark_read": NoMarkRead("mark_read")}):
            self.set_on()
            self.add_rule("料金", "料金表はこちらです")
            mid = self.inbound("U1", "料金")
            self.assertEqual(self.match(mid)["action"], "auto_send")
            self.assertEqual(self.kw("sent", mid, "--result", "failed", "--error", LIMIT_ERROR)[0], 0)
            self.assertEqual(self.log_out("--to", "U1", "--status", "failed", "--error", "too long")[0], 0)
            self.assertEqual(self.log_out("--broadcast", "--status", "failed", "--error", "too long")[0], 0)
            self.assertIn(mid, self.pending_ids())
            self.assertEqual(self.kw("text", mid, "--out", os.path.join(self.dir, "reply.txt"))[0], 0)
            self.assertIn("U1", self.unreplied())
            self.assertEqual(self.run_script("stats", "--days", "1", "--json")[0], 0)
            self.assertEqual(self.log_out("--to", "U1", "--status", "send_unknown", "--error", "timeout")[0], 0)
        conn.assert_not_called()
        self.urlopen.assert_not_called()
        for cmd in calls:
            self.assertNotRegex(cmd, r"(?i)mark.?(as.?)?read|curl|wget|https?://")
        self.assertEqual(self.decisions(mid), ["auto_failed"])
        self.assertEqual(self.count("messages"), 5)
        self.assertEqual(self.count("messages", "status IN ('sent', 'auto_reply', 'app_reply')"), 0)


def main():
    before = real_db_stat()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(SendErrorTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    untouched = real_db_stat() == before
    print("summary: %d run, %d failures, %d errors; %s %s" % (
        result.testsRun, len(result.failures), len(result.errors), REAL_DB,
        "unchanged" if untouched else "CHANGED"))
    return 0 if result.wasSuccessful() and untouched else 1


if __name__ == "__main__":
    sys.exit(main())
