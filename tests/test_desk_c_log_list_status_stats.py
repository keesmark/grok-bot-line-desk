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
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from unittest import mock

sys.dont_write_bytecode = True
SCRIPTS = os.environ.get("SCRIPTS") or os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts"))
REAL_DB = "/workspace/line-inbox/messages.db"
TOKEN = "test-token-SECRET123"
DEAD_PROXY = "http://127.0.0.1:9"
PROXY_VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")
_ids = itertools.count(1)


def real_db_stamp():
    try:
        st = os.stat(REAL_DB)
    except FileNotFoundError:
        return None
    return (st.st_mtime_ns, st.st_size)


def is_real_db(path):
    return bool(path) and os.path.realpath(path) == os.path.realpath(REAL_DB)


def ev(eid, uid, mtype="text", text="hello", minutes_ago=30, group=None, redelivery=False):
    e = {"type": "message", "mode": "active", "webhookEventId": eid, "replyToken": "rt-" + eid,
         "timestamp": int(time.time() * 1000) - minutes_ago * 60000,
         "source": {"type": "group", "groupId": group, "userId": uid} if group
         else {"type": "user", "userId": uid},
         "deliveryContext": {"isRedelivery": redelivery}}
    if mtype == "follow":
        e["type"] = "follow"
    else:
        e["message"] = {"type": mtype, "id": "m-" + eid}
        if mtype == "text":
            e["message"]["text"] = text
    return e


def body(*events):
    return json.dumps({"destination": "Ubot0", "events": list(events)}, ensure_ascii=False)


class DeskTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="desk-test-")
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.db = os.path.join(self.tmp, "messages.db")
        env = mock.patch.dict(os.environ, {"DESK_DB": self.db, "LINE_CHANNEL_ACCESS_TOKEN": TOKEN,
                                           **{k: DEAD_PROXY for k in PROXY_VARS}})
        env.start()
        self.addCleanup(env.stop)
        for k in ("no_proxy", "NO_PROXY"):
            os.environ.pop(k, None)
        self.assertFalse(is_real_db(os.environ["DESK_DB"]), "DESK_DB must never be the real inbox DB")
        self.net_calls = []
        self.init_db()

    def tearDown(self):
        self.assertEqual(self.net_calls, [], "scripts must not touch the network")

    def fake_urlopen(self, *args, **kwargs):
        self.net_calls.append(args)
        raise urllib.error.URLError("network disabled in tests")

    def init_db(self):
        self.assertFalse(is_real_db(os.environ.get("DESK_DB")))
        r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "init_db.py")], env=dict(os.environ),
                           cwd=self.tmp, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        self.assertNotIn(TOKEN, r.stdout + r.stderr)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(self.db), "init_db.py did not create DESK_DB")

    def run_script(self, name, *argv, stdin=""):
        self.assertFalse(is_real_db(os.environ.get("DESK_DB")))
        path = os.path.join(SCRIPTS, name + ".py")
        spec = importlib.util.spec_from_file_location(f"desk_{name}_{next(_ids)}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        net = getattr(getattr(mod, "urllib", None), "request", urllib.request)
        fake_in = io.TextIOWrapper(io.BytesIO(stdin.encode("utf-8")), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", fake_in), mock.patch.object(net, "urlopen", self.fake_urlopen), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = mod.main(list(argv))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        code = 0 if code is None else code
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue(), "token leaked into output")
        return code, out.getvalue(), err.getvalue()

    def log(self, *events):
        code, out, err = self.run_script("log_event", stdin=body(*events))
        self.assertEqual(code, 0, err)
        return out

    def rows(self, sql, args=()):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def count(self, where="1"):
        return self.rows(f"SELECT COUNT(*) FROM messages WHERE {where}")[0][0]

    def conv(self, key):
        r = self.rows("SELECT state, memo FROM conversations WHERE chat_key = ?", (key,))
        return r[0] if r else None

    def listed(self, *flags):
        code, out, err = self.run_script("list_unreplied", "--json", *flags)
        self.assertEqual(code, 0, err)
        return {c["chat_key"]: c for c in json.loads(out)}

    def text_file(self, text):
        path = os.path.join(self.tmp, f"text{next(_ids)}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path


class LogEventTest(DeskTest):
    def test_sandbox_and_init_idempotent(self):
        self.assertTrue(os.path.realpath(self.db).startswith(os.path.realpath(self.tmp) + os.sep))
        self.assertFalse(stat.S_IMODE(os.stat(self.db).st_mode) & 0o002, "DB is world-writable")
        tables = {r[0] for r in self.rows("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertLessEqual({"messages", "conversations", "customers"}, tables)
        self.assertIn("read_at", {r[1] for r in self.rows("PRAGMA table_info(messages)")})
        self.log(ev("E1", "U1"))
        self.init_db()
        self.assertEqual(self.count(), 1)

    def test_webhook_mixed_events(self):
        out = self.log(ev("E1", "U1", text="こんにちは"), ev("E2", "U2", "image"),
                       ev("E3", "U3", text="group hi", group="Cg1"), ev("E4", "U4", "follow"))
        self.assertEqual(out.count("inserted"), 4, out)
        self.assertIn("chat_key=Cg1", out)
        self.assertIn("message_type=follow", out)
        got = {r[0]: r[1:] for r in self.rows(
            "SELECT webhook_event_id, direction, source_type, message_type, text, line_message_id, status"
            " FROM messages")}
        self.assertEqual(got["E1"], ("in", "user", "text", "こんにちは", "m-E1", "received"))
        self.assertEqual(got["E2"], ("in", "user", "image", None, "m-E2", "received"))
        self.assertEqual(got["E3"], ("in", "group", "text", "group hi", "m-E3", "received"))
        self.assertEqual(got["E4"], ("in", "user", "follow", None, None, "received"))
        self.assertEqual(self.rows("SELECT group_id, user_id FROM messages WHERE webhook_event_id = 'E3'"),
                         [("Cg1", "U3")])
        raw = self.rows("SELECT raw FROM messages WHERE webhook_event_id = 'E1'")[0][0]
        self.assertEqual(json.loads(raw)["webhookEventId"], "E1")
        for key in ("U1", "U2", "Cg1"):
            self.assertEqual(self.conv(key)[0], "未対応", key)
        self.assertIsNone(self.conv("U4"), "a follow event must not open a chat")

    def test_redelivery_is_duplicate(self):
        self.log(ev("E1", "U1"))
        out = self.log(ev("E1", "U1", redelivery=True))
        self.assertIn("duplicate", out)
        self.assertNotIn("inserted", out)
        self.assertIn("isRedelivery=true", out)
        self.assertEqual(self.count("webhook_event_id = 'E1'"), 1)
        out = self.log(ev("E1", "U1"), ev("E5", "U1"))
        self.assertEqual((out.count("duplicate"), out.count("inserted")), (1, 1), out)
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.listed()["U1"]["count"], 2)

    def test_bad_input_logs_nothing(self):
        for data in ("", "  \n", "{not json", "42", "[1, 2]", '{"events": "x"}'):
            with self.subTest(data=data):
                code, _, err = self.run_script("log_event", stdin=data)
                self.assertEqual(code, 1)
                self.assertIn("error", err.lower())
        code, _, err = self.run_script("log_event", stdin='{"destination": "U0", "events": []}')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.count(), 0)

    def test_out_logging(self):
        self.log(ev("E1", "U1"))
        path = self.text_file("ご連絡ありがとうございます。\n")
        code, out, err = self.run_script("log_event", "--out", "--to", "U1", "--text-file", path,
                                         "--line-message-id", "LM1")
        self.assertEqual(code, 0, err)
        self.assertIn("inserted", out)
        self.assertEqual(self.rows("SELECT source_type, user_id, message_type, text, line_message_id, status"
                                   " FROM messages WHERE direction = 'out'"),
                         [("user", "U1", "text", "ご連絡ありがとうございます。", "LM1", "sent")])
        self.assertNotIn("U1", self.listed())
        code, _, err = self.run_script("log_event", "--out", "--to", "U2", "--text", "-", stdin="from stdin\n")
        self.assertEqual(code, 0, err)
        code, _, err = self.run_script("log_event", "--out", "--to", "U3", "--status", "app_reply")
        self.assertEqual(code, 0, err)
        got = dict((r[0], r[1:]) for r in self.rows(
            "SELECT user_id, text, status FROM messages WHERE direction = 'out'"))
        self.assertEqual(got["U2"], ("from stdin", "sent"))
        self.assertEqual(got["U3"], (None, "app_reply"))

    def test_out_errors(self):
        missing = os.path.join(self.tmp, "nope.txt")
        for argv in (["--out"], ["--out", "--to", "  "], ["--out", "--to", "U1"],
                     ["--out", "--to", "U1", "--text", "hello"],
                     ["--out", "--to", "U1", "--text", "-"],
                     ["--out", "--to", "U1", "--text-file", missing],
                     ["--to", "U1", "--text", "-"],
                     ["--out", "--to", "U1", "--status", "bogus"]):
            with self.subTest(argv=argv):
                code, _, err = self.run_script("log_event", *argv)
                self.assertNotEqual(code, 0)
                self.assertTrue(err.strip())
        self.assertEqual(self.count(), 0)


class ListUnrepliedTest(DeskTest):
    def test_waiting_user_chat_listed(self):
        self.log(ev("E1", "U1", text="first", minutes_ago=50), ev("E2", "U1", text="second\nline", minutes_ago=40),
                 ev("E3", "U4", "follow"), ev("E4", "U3", group="Cg1"))
        code, out, err = self.run_script("list_unreplied")
        self.assertEqual(code, 0, err)
        for part in ("chat_key=U1", "source_type=user", "state=未対応", "count=2", "second line"):
            self.assertIn(part, out)
        self.assertNotIn("U4", out)
        self.assertNotIn("chat_key=Cg1", out)
        self.assertIn("--include-groups", out)

    def test_include_groups(self):
        self.log(ev("E1", "U1"), ev("E2", "U3", group="Cg1"))
        code, out, err = self.run_script("list_unreplied", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual([c["chat_key"] for c in json.loads(out)], ["U1"])
        self.assertIn("--include-groups", err)
        chats = self.listed("--include-groups")
        self.assertEqual(set(chats), {"U1", "Cg1"})
        self.assertEqual(chats["Cg1"]["source_type"], "group")
        code, out, err = self.run_script("list_unreplied", "--include-groups")
        self.assertEqual(code, 0, err)
        self.assertIn("chat_key=Cg1", out)

    def test_json_fields_and_order(self):
        self.assertEqual(self.listed(), {})
        self.log(ev("E1", "U1", text="hi", minutes_ago=10), ev("E2", "U2", "image", minutes_ago=90))
        code, out, err = self.run_script("list_unreplied", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual([c["chat_key"] for c in data], ["U2", "U1"])
        first, second = data
        self.assertEqual((first["n"], first["message_type"], first["count"], first["read"]), (1, "image", 1, False))
        self.assertTrue(85 <= first["waiting_minutes"] <= 95, first["waiting_minutes"])
        self.assertEqual((second["text"], second["state"], second["source_type"]), ("hi", "未対応", "user"))
        self.assertEqual(second["id"], self.rows("SELECT id FROM messages WHERE webhook_event_id = 'E1'")[0][0])

    def test_failed_send_is_not_a_reply(self):
        self.log(ev("E1", "U1"))
        path = self.text_file("sorry for the wait")
        code, _, err = self.run_script("log_event", "--out", "--to", "U1", "--text-file", path, "--status", "failed")
        self.assertEqual(code, 0, err)
        self.assertIn("U1", self.listed())
        code, _, err = self.run_script("log_event", "--out", "--to", "U1", "--text-file", path)
        self.assertEqual(code, 0, err)
        self.assertNotIn("U1", self.listed())


class SetStatusTest(DeskTest):
    def test_done_hides_and_new_inbound_reopens(self):
        self.log(ev("E1", "U1"))
        code, out, err = self.run_script("set_status", "U1", "完了")
        self.assertEqual(code, 0, err)
        self.assertIn("U1", out)
        self.assertIn("完了", out)
        self.assertEqual(self.conv("U1")[0], "完了")
        self.assertNotIn("U1", self.listed())
        self.assertIn("duplicate", self.log(ev("E1", "U1", redelivery=True)))
        self.assertEqual(self.conv("U1")[0], "完了", "a redelivered old event must not reopen the chat")
        self.log(ev("E2", "U1", text="still there?", minutes_ago=1))
        self.assertEqual(self.conv("U1")[0], "未対応")
        chat = self.listed()["U1"]
        self.assertEqual((chat["state"], chat["text"]), ("未対応", "still there?"))

    def test_aliases_memo_and_errors(self):
        self.log(ev("E1", "U1"))
        code, out, err = self.run_script("set_status", "U1", "doing", "--memo", "VIP 顧客")
        self.assertEqual(code, 0, err)
        self.assertIn("対応中", out)
        self.assertEqual(self.conv("U1"), ("対応中", "VIP 顧客"))
        self.assertEqual((self.listed()["U1"]["state"], self.listed()["U1"]["memo"]), ("対応中", "VIP 顧客"))
        self.assertEqual(self.run_script("set_status", "U1", "open")[0], 0)
        self.assertEqual(self.conv("U1"), ("未対応", "VIP 顧客"), "memo must be kept without --memo")
        self.assertEqual(self.run_script("set_status", "U1", "done", "--memo", "")[0], 0)
        self.assertEqual(self.conv("U1"), ("完了", None))
        for argv in (["Unone", "完了"], ["U1", "bogus"], ["  ", "完了"]):
            with self.subTest(argv=argv):
                code, _, err = self.run_script("set_status", *argv)
                self.assertNotEqual(code, 0)
                self.assertTrue(err.strip())
        self.assertIsNone(self.conv("Unone"))
        self.assertEqual(self.conv("U1")[0], "完了")


class StatsTest(DeskTest):
    def stats(self, *argv):
        code, out, err = self.run_script("stats", "--json", *argv)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_stats_json(self):
        r = self.stats()
        self.assertEqual((r["inbound_total"], r["unreplied_user"], r["latest_inbound_at"]), (0, 0, None))
        self.log(ev("E1", "U1"), ev("E2", "U2", "image"), ev("E3", "U3", group="Cg1"), ev("E4", "U4", "follow"))
        code, _, err = self.run_script("log_event", "--out", "--to", "U1", "--text-file", self.text_file("ok"))
        self.assertEqual(code, 0, err)
        r = self.stats()
        self.assertEqual((r["inbound_total"], r["inbound_text"], r["inbound_other"], r["inbound_chats"]), (3, 2, 1, 3))
        self.assertEqual((r["inbound_by_source_type"]["user"], r["inbound_by_source_type"]["group"]), (2, 1))
        self.assertEqual(r["other_events"], {"follow": 1})
        self.assertEqual((r["outbound_total"], r["outbound_by_status"]), (1, {"sent": 1}))
        self.assertEqual((r["unreplied_user"], r["unreplied_group_room"]), (1, 1))
        self.assertIsNotNone(r["latest_inbound_at"])
        self.assertGreaterEqual(r["latest_inbound_minutes_ago"], 0)
        self.assertEqual(self.run_script("set_status", "U2", "完了")[0], 0)
        self.assertEqual(self.stats()["unreplied_user"], 0)
        today = datetime.now().date()
        yesterday = (today - timedelta(days=1)).isoformat()
        self.assertEqual(self.stats("--since", yesterday, "--until", today.isoformat())["inbound_total"], 3)
        code, out, err = self.run_script("stats")
        self.assertEqual(code, 0, err)
        self.assertIn("inbound", out)
        for argv in (["--days", "0"], ["--until", yesterday], ["--since", "2026-13-01"],
                     ["--since", today.isoformat(), "--until", yesterday], ["--since", yesterday, "--days", "3"]):
            with self.subTest(argv=argv):
                self.assertNotEqual(self.run_script("stats", *argv)[0], 0)


def main():
    if not os.path.isdir(SCRIPTS):
        print(f"error: scripts folder not found: {SCRIPTS} (set env SCRIPTS)", file=sys.stderr)
        return 2
    before = real_db_stamp()
    old_umask = os.umask(0o022)
    try:
        suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    finally:
        os.umask(old_umask)
    real_ok = real_db_stamp() == before
    if not real_ok:
        print(f"FAIL: {REAL_DB} changed during the test run", file=sys.stderr)
    ok = result.wasSuccessful() and real_ok
    print(f"summary: ran={result.testsRun} failures={len(result.failures)} errors={len(result.errors)}"
          f" skipped={len(result.skipped)} real_db_unchanged={real_ok} -> {'OK' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
