import contextlib
import importlib.util
import io
import itertools
import json
import os
import re
import sqlite3
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
_mods, _ids = {}, itertools.count(1)


def load(name):
    if name not in _mods:
        spec = importlib.util.spec_from_file_location("g2_" + name, os.path.join(SCRIPTS, name + ".py"))
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


class KeywordReplyTest(unittest.TestCase):
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

    def inbound(self, user, text=None, mtype="text", source="user"):
        i = next(_ids)
        msg = {"id": "lm%d" % i, "type": mtype}
        msg.update({"text": text} if mtype == "text" else {"packageId": "1", "stickerId": "2"})
        src = {"type": source, "userId": user}
        if source == "group":
            src["groupId"] = "Cgroup1"
        ev = {"type": "message", "mode": "active", "timestamp": int((time.time() - 120) * 1000),
              "webhookEventId": "EV%d" % i, "deliveryContext": {"isRedelivery": False},
              "replyToken": "rt%d" % i, "source": src, "message": msg}
        code, out, err = self.run_script("log_event", stdin=json.dumps({"destination": "Ubot", "events": [ev]}))
        self.assertEqual(code, 0, err)
        return int(re.search(r"inserted (\d+)", out).group(1))

    def match(self, mid, *args):
        code, out, err = self.kw("match", mid, "--json", *args)
        self.assertEqual(code, 0, err)
        return json.loads(out)

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

    def test_off_suggest_pending_and_text_snapshot(self):
        rid = self.add_rule("abc", "Hello {{name}}\nSee you", "contains")
        mid = self.inbound("U1", "abc PRIVATE-7781")
        r = self.match(mid)
        self.assertEqual((r["action"], r["reason"], r["rule_id"]), ("suggest", "off", rid))
        self.assertEqual(self.q("SELECT decision FROM keyword_matches WHERE message_id=?", mid),
                         [{"decision": "suggested"}])
        code, out, _ = self.kw("pending", "--json")
        self.assertEqual((code, [p["message_id"] for p in json.loads(out)]), (0, [mid]))
        self.assertEqual(self.kw("edit", rid, "--reply", "-", stdin="Changed")[0], 0)
        path = os.path.join(self.dir, "reply.txt")
        self.assertEqual(self.kw("text", mid, "--out", path)[0], 0)
        self.assertEqual(self.read(path), "Hello {{name}}\nSee you")
        self.assertFalse(any("PRIVATE-7781" in o for o in self.outputs), "customer text was printed")
        self.assertEqual(self.run_script("log_event", "--out", "--to", "U1", "--text", "-", stdin="ok")[0], 0)
        self.assertEqual(json.loads(self.kw("pending", "--json")[1]), [])

    def test_none_for_non_text_group_or_no_rule(self):
        self.set_on()
        self.add_rule("abc", "Reply", "contains")
        for mid, reason in ((self.inbound("U1", mtype="sticker"), "not_1to1_text"),
                            (self.inbound("U1", "abc", source="group"), "not_1to1_text"),
                            (self.inbound("U1", "xyz"), "no_match")):
            r = self.match(mid, "--remaining", "unlimited")
            self.assertEqual((r["action"], r["reason"]), ("none", reason))
        self.assertEqual(self.count("keyword_matches"), 0)

    def test_quota_decisions_auto_send_and_already(self):
        self.set_on()
        self.assertEqual(self.kw("setting", "--min-remaining", "20")[0], 0)
        rid = self.add_rule("price", "Price list: 1000 yen", "contains")
        m1 = self.inbound("U1", "price?")
        self.assertEqual(self.match(m1)["action"], "need_quota")
        self.assertEqual(self.count("keyword_matches"), 0)
        r = self.match(m1, "--remaining", "5")
        self.assertEqual((r["action"], r["reason"]), ("suggest", "quota_low"))
        r = self.match(self.inbound("U2", "price"), "--remaining", "unknown")
        self.assertEqual((r["action"], r["reason"]), ("suggest", "quota_unknown"))
        out = os.path.join(self.dir, "auto.txt")
        m3 = self.inbound("U3", "PRICE")
        r = self.match(m3, "--remaining", "100", "--out", out)
        self.assertEqual((r["action"], r["decision"], r["rule_id"]), ("auto_send", "auto_pending", rid))
        self.assertEqual(self.read(out), "Price list: 1000 yen")
        r = self.match(m3, "--remaining", "100")
        self.assertEqual((r["action"], r["decision"]), ("already", "auto_pending"))
        self.assertEqual(self.count("keyword_matches", "message_id=?", m3), 1)
        self.assertEqual(self.match(self.inbound("U4", "price"), "--remaining", "unlimited")["action"],
                         "auto_send")
        rows = self.q("SELECT user_id, decision FROM keyword_matches ORDER BY id")
        self.assertEqual([(x["user_id"], x["decision"]) for x in rows],
                         [("U1", "suggested"), ("U2", "suggested"),
                          ("U3", "auto_pending"), ("U4", "auto_pending")])

    def test_default_min_remaining_zero_skips_quota(self):
        self.set_on()
        rid = self.add_rule("price", "Price list: 1000 yen", "contains")
        out = os.path.join(self.dir, "auto.txt")
        r = self.match(self.inbound("U1", "price?"), "--out", out)
        self.assertEqual((r["action"], r["reason"], r["decision"], r["rule_id"]),
                         ("auto_send", "auto", "auto_pending", rid))
        self.assertEqual(self.read(out), "Price list: 1000 yen")
        for user, remaining in (("U2", "unknown"), ("U3", "0")):
            r = self.match(self.inbound(user, "price"), "--remaining", remaining)
            self.assertEqual((r["action"], r["reason"], r["decision"]),
                             ("auto_send", "auto", "auto_pending"), remaining)
        self.assertEqual(self.kw("setting", "--min-remaining", "5")[0], 0)
        m4 = self.inbound("U4", "price")
        self.assertEqual(self.match(m4)["action"], "need_quota")
        self.assertEqual(self.count("keyword_matches", "user_id=?", "U4"), 0)
        r = self.match(m4, "--remaining", "3")
        self.assertEqual((r["action"], r["reason"]), ("suggest", "quota_low"))
        rows = self.q("SELECT user_id, decision, reason FROM keyword_matches ORDER BY id")
        self.assertEqual([(x["user_id"], x["decision"], x["reason"]) for x in rows],
                         [("U1", "auto_pending", "auto"), ("U2", "auto_pending", "auto"),
                          ("U3", "auto_pending", "auto"), ("U4", "suggested", "quota_low")])

    def test_window_is_per_user_and_rule(self):
        self.set_on()
        self.add_rule("hours", "Open 10-18", "contains")
        self.add_rule("menu", "Menu: A, B", "contains")
        unl = ("--remaining", "unlimited")
        self.assertEqual(self.match(self.inbound("U1", "hours?"), *unl)["action"], "auto_send")
        r = self.match(self.inbound("U1", "hours again"), *unl)
        self.assertEqual((r["action"], r["reason"]), ("suggest", "window"))
        self.assertEqual(self.match(self.inbound("U2", "hours"), *unl)["action"], "auto_send")
        self.assertEqual(self.match(self.inbound("U1", "menu"), *unl)["action"], "auto_send")

    def test_priority_exact_then_longest_and_nfkc(self):
        ab = self.add_rule("ab", "R-ab", "contains")
        long_contains = self.add_rule("abcdx", "R-contains", "contains")
        exact = self.add_rule("abcdx", "R-exact", "exact")
        abc = self.add_rule("abc", "R-abc", "exact")
        for text, want in (("ABCDX", exact), ("zz abcdx zz", long_contains), ("zab", ab),
                           ("ＡＢＣ", abc), ("Ａ\u3000Ｂ Ｃ", abc)):
            self.assertEqual(self.match(self.inbound("U1", text))["rule_id"], want, text)

    def test_sent_results_and_unreplied(self):
        self.set_on()
        rid = self.add_rule("hours", "Open 10-18\nClosed Mon", "contains")
        mids = {u: self.inbound(u, "hours?") for u in ("U1", "U2", "U3")}
        for mid in mids.values():
            self.assertEqual(self.match(mid, "--remaining", "unlimited")["action"], "auto_send")
        self.assertEqual(self.kw("edit", rid, "--reply", "-", stdin="Edited")[0], 0)
        code, out, err = self.kw("sent", mids["U1"], "--result", "sent", "--line-message-id", "LM-1")
        self.assertEqual(code, 0, err)
        self.assertIn("auto_sent", out)
        self.assertEqual(self.kw("sent", mids["U1"], "--result", "sent")[0], 2)
        self.assertEqual(self.kw("sent", mids["U2"], "--result", "unknown")[0], 0)
        self.assertEqual(self.kw("sent", mids["U3"], "--result", "failed")[0], 0)
        outs = self.q("SELECT id, user_id, status, text, line_message_id FROM messages"
                      " WHERE direction='out' ORDER BY id")
        self.assertEqual([(r["user_id"], r["status"]) for r in outs],
                         [("U1", "auto_reply"), ("U2", "auto_reply_unknown"), ("U3", "auto_reply_failed")])
        self.assertEqual((outs[0]["text"], outs[0]["line_message_id"]), ("Open 10-18\nClosed Mon", "LM-1"))
        km = self.q("SELECT decision, out_message_id FROM keyword_matches ORDER BY id")
        self.assertEqual([(k["decision"], k["out_message_id"]) for k in km],
                         [("auto_sent", outs[0]["id"]), ("auto_unknown", outs[1]["id"]),
                          ("auto_failed", outs[2]["id"])])
        self.assertEqual([f for f in os.listdir(self.dir) if f.startswith(".keyword-sent.")], [])
        code, out, err = self.run_script("list_unreplied", "--json")
        self.assertEqual(code, 0, err)
        waiting = {c["chat_key"] for c in json.loads(out)}
        self.assertNotIn("U1", waiting)
        self.assertNotIn("U2", waiting)
        self.assertIn("U3", waiting)

    def test_log_event_auto_reply_needs_single_to(self):
        code, _, err = self.run_script("log_event", "--out", "--to", "U1", "--to", "U2", "--text", "-",
                                       "--status", "auto_reply", stdin="hi")
        self.assertNotEqual(code, 0)
        self.assertIn("error", err)
        self.assertEqual(self.count("messages"), 0)

    def test_purge_user_keeps_rules(self):
        self.add_rule("abc", "Reply")
        for u in ("U1", "U2"):
            self.assertEqual(self.match(self.inbound(u, "abc"))["action"], "suggest")
        self.assertEqual(self.run_script("purge_old", "--user", "U1")[0], 0)
        self.assertEqual(self.count("keyword_matches"), 2)
        code, _, err = self.run_script("purge_old", "--user", "U1", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual([r["user_id"] for r in self.q("SELECT user_id FROM keyword_matches")], ["U2"])
        self.assertEqual(self.count("keyword_rules"), 1)
        self.assertEqual(self.count("messages", "user_id=?", "U1"), 0)


def main():
    before = real_db_stat()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(KeywordReplyTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    untouched = real_db_stat() == before
    print("summary: %d run, %d failures, %d errors; %s %s" % (
        result.testsRun, len(result.failures), len(result.errors), REAL_DB,
        "unchanged" if untouched else "CHANGED"))
    return 0 if result.wasSuccessful() and untouched else 1


if __name__ == "__main__":
    sys.exit(main())
