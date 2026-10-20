import contextlib
import datetime
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
BOT = "U" + "f" * 32
USER = "U" + "0" * 28 + "a1b2"
OTHER = "U" + "0" * 28 + "e5f6"
MEMBER = "U" + "0" * 28 + "c3d4"
GROUP = "C" + "0" * 28 + "9f3e"
NAME = "田中 花子"
FALLBACK = "名前不明（ID末尾 a1b2）"
PROFILE_KEYS = {"user_id", "display_name", "nickname", "profile_at", "stale", "short_id", "label"}
_mods, _ids = {}, itertools.count(1)


def load(name):
    if name not in _mods:
        spec = importlib.util.spec_from_file_location("i_" + name, os.path.join(SCRIPTS, name + ".py"))
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


class DisplayNameTest(unittest.TestCase):
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

    def event(self, etype, user, source="user", ago=120):
        i = next(_ids)
        src = {"type": source, "userId": user}
        if source == "group":
            src["groupId"] = GROUP
        return {"type": etype, "mode": "active", "timestamp": int((time.time() - ago) * 1000),
                "webhookEventId": "EV%d" % i, "deliveryContext": {"isRedelivery": False},
                "replyToken": "rt%d" % i, "source": src}

    def wrap(self, *events):
        return json.dumps({"destination": BOT, "events": list(events)})

    def body(self, user, text=None, mtype="text", source="user", ago=120):
        ev = self.event("message", user, source, ago)
        msg = {"id": "lm%d" % next(_ids), "type": mtype}
        msg.update({"text": text} if mtype == "text" else {"packageId": "1", "stickerId": "2"})
        ev["message"] = msg
        return self.wrap(ev)

    def inbound(self, user, text=None, mtype="text", source="user", ago=120):
        code, out, err = self.run_script("log_event", stdin=self.body(user, text, mtype, source, ago))
        self.assertEqual(code, 0, err)
        return int(re.search(r"inserted (\d+)", out).group(1))

    def event_line(self, body):
        code, out, err = self.run_script("log_event", stdin=body)
        self.assertEqual(code, 0, err)
        lines = [l for l in out.splitlines() if l.startswith(("inserted ", "duplicate"))]
        self.assertEqual(len(lines), 1, out)
        return lines[0]

    def log_out(self, *args, stdin="hello"):
        return self.run_script("log_event", "--out", "--text", "-", *args, stdin=stdin)

    def pc(self, *args, stdin=""):
        return self.run_script("profile_cache", *args, stdin=stdin)

    def profile(self, user):
        code, out, err = self.pc("get", user, "--json")
        try:
            data = json.loads(out)
        except ValueError:
            self.fail("get --json printed no JSON (exit %s): %r %r" % (code, out, err))
        self.assertLessEqual(PROFILE_KEYS, set(data))
        self.assertEqual(data["user_id"], user)
        return data

    def label(self, user):
        code, out, err = self.pc("label", user)
        self.assertEqual(code, 0, err)
        self.assertTrue(out.endswith("\n"), repr(out))
        text = out[:-1]
        self.assertNotIn("\n", text)
        self.assertNotIn("\r", text)
        self.assertEqual(text, text.strip())
        return text

    def set_name(self, user, name):
        code, out, err = self.pc("set", user, "--name", "-", stdin=name)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "saved")

    def stale_listed(self, user):
        code, out, err = self.pc("stale")
        self.assertEqual(code, 0, err)
        code, out_json, err = self.pc("stale", "--json")
        self.assertEqual(code, 0, err)
        listed = user in json.dumps(json.loads(out_json))
        self.assertEqual(user in out, listed, (out, out_json))
        return listed

    def chats(self, *args):
        code, out, err = self.run_script("list_unreplied", "--json", *args)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def chat(self, *args):
        found = self.chats(*args)
        self.assertEqual(len(found), 1, found)
        return found[0]

    def plain(self, *args):
        code, out, err = self.run_script("list_unreplied", *args)
        self.assertEqual(code, 0, err)
        return out

    def move_profile_back(self, user, days):
        rows = self.q("SELECT profile_at FROM customers WHERE user_id=?", user)
        self.assertEqual(len(rows), 1)
        stored = rows[0]["profile_at"]
        self.assertRegex(stored or "", r"^\d{4}-\d\d-\d\d", "profile_at is not an ISO date")
        day = datetime.date.fromisoformat(stored[:10]) - datetime.timedelta(days=days)
        self.execute("UPDATE customers SET profile_at=? WHERE user_id=?", day.isoformat() + stored[10:], user)

    def drop_profile_at(self):
        keep = [c for c in self.q("PRAGMA table_info(customers)") if c["name"] != "profile_at"]
        names = ", ".join('"%s"' % c["name"] for c in keep)
        cols = ", ".join('"%s" %s%s%s%s' % (
            c["name"], c["type"], " PRIMARY KEY" if c["pk"] else "", " NOT NULL" if c["notnull"] else "",
            "" if c["dflt_value"] is None else " DEFAULT %s" % c["dflt_value"]) for c in keep)
        con = sqlite3.connect(self.db)
        try:
            with con:
                con.execute("CREATE TABLE customers2(%s)" % cols)
                con.execute("INSERT INTO customers2(%s) SELECT %s FROM customers" % (names, names))
                con.execute("DROP TABLE customers")
                con.execute("ALTER TABLE customers2 RENAME TO customers")
        finally:
            con.close()
        have = [c["name"] for c in self.q("PRAGMA table_info(customers)")]
        self.assertNotIn("profile_at", have)
        self.assertLessEqual({"user_id", "display_name", "note", "evidence", "updated_at"}, set(have))

    def q(self, sql, *params):
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql, params)]
        finally:
            con.close()

    def execute(self, sql, *params):
        con = sqlite3.connect(self.db)
        try:
            with con:
                con.execute(sql, params)
        finally:
            con.close()

    def count(self, table, where="1", *params):
        return self.q("SELECT COUNT(*) AS n FROM %s WHERE %s" % (table, where), *params)[0]["n"]

    def test_unknown_user_gets_short_id_label(self):
        p = self.profile(USER)
        self.assertIsNone(p["display_name"])
        self.assertIsNone(p["profile_at"])
        self.assertIs(p["stale"], True)
        self.assertEqual(p["short_id"], "a1b2")
        self.assertEqual(p["label"], FALLBACK)
        self.assertNotIn(USER, p["label"])
        self.assertEqual(self.pc("get", USER)[1].strip(), "unknown")
        label = self.label(USER)
        self.assertEqual(label, FALLBACK)
        self.assertNotIn(USER, label)
        self.assertEqual(self.label("U1"), "名前不明（ID末尾 U1）")
        self.assertEqual(self.count("customers", "COALESCE(display_name, '') != ''"), 0)

    def test_set_name_from_stdin(self):
        self.set_name(USER, NAME + "\n")
        p = self.profile(USER)
        self.assertEqual(p["display_name"], NAME)
        self.assertEqual(p["label"], NAME)
        self.assertIs(p["stale"], False)
        self.assertEqual(p["short_id"], "a1b2")
        self.assertTrue(p["profile_at"])
        self.assertEqual(self.label(USER), NAME)
        self.assertEqual(self.pc("get", USER)[1].strip(), NAME)
        rows = self.q("SELECT display_name, profile_at FROM customers WHERE user_id=?", USER)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["display_name"], NAME)
        self.assertTrue(rows[0]["profile_at"])

    def test_name_with_line_break_and_tab_is_one_line(self):
        path = os.path.join(self.dir, "name.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("山田\n太郎\t様\n")
        code, out, err = self.pc("set", USER, "--name-file", path)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "saved")
        label = self.label(USER)
        self.assertRegex(label, r"^山田\s+太郎\s*様$")
        p = self.profile(USER)
        self.assertEqual(p["label"], label)
        self.assertNotIn("\n", p["display_name"])
        self.inbound(USER, "よろしくお願いします")
        self.assertEqual(self.chat()["label"], label)
        self.set_name(OTHER, "長" * 150)
        name = self.profile(OTHER)["display_name"]
        self.assertLessEqual(len(name), 100)
        self.assertTrue(name.startswith("長" * 90), name)
        self.assertEqual(self.label(OTHER), name)

    def test_unreplied_json_has_sender_and_label(self):
        self.inbound(USER, "予約したいです")
        c = self.chat()
        self.assertEqual(c["user_id"], USER)
        self.assertEqual(c["short_id"], "a1b2")
        self.assertEqual(c["label"], FALLBACK)
        self.assertNotIn(USER, c["label"])
        self.assertIs(c["name_stale"], True)
        self.assertEqual(c["name"], "")
        self.set_name(USER, NAME + "\n")
        c = self.chat()
        self.assertEqual(c["user_id"], USER)
        self.assertEqual(c["short_id"], "a1b2")
        self.assertEqual(c["label"], NAME)
        self.assertIs(c["name_stale"], False)
        self.assertEqual(c["name"], NAME)

    def test_name_older_than_7_days_is_stale_but_shown(self):
        self.assertEqual(load("profile_cache").MAX_AGE_DAYS, 7)
        self.inbound(USER, "まだですか")
        self.assertTrue(self.stale_listed(USER))
        self.set_name(USER, NAME)
        self.assertFalse(self.stale_listed(USER))
        self.move_profile_back(USER, 6)
        self.assertIs(self.profile(USER)["stale"], False)
        self.assertIs(self.chat()["name_stale"], False)
        self.assertFalse(self.stale_listed(USER))
        self.move_profile_back(USER, 2)
        p = self.profile(USER)
        self.assertIs(p["stale"], True)
        self.assertEqual(p["display_name"], NAME)
        self.assertEqual(p["label"], NAME)
        c = self.chat()
        self.assertIs(c["name_stale"], True)
        self.assertEqual(c["label"], NAME)
        self.assertEqual(c["name"], NAME)
        self.assertTrue(self.stale_listed(USER))
        self.execute("UPDATE customers SET profile_at='not a date' WHERE user_id=?", USER)
        c = self.chat()
        self.assertIs(c["name_stale"], True)
        self.assertEqual(c["label"], NAME)
        self.set_name(USER, NAME)
        self.assertIs(self.chat()["name_stale"], False)
        self.assertFalse(self.stale_listed(USER))

    def test_group_chat_shows_the_latest_sender(self):
        self.inbound(MEMBER, "グループから失礼します", source="group", ago=300)
        self.assertEqual(self.chats(), [])
        c = self.chat("--include-groups")
        self.assertEqual(c["user_id"], MEMBER)
        self.assertEqual(c["short_id"], "c3d4")
        self.assertEqual(c["label"], "名前不明（ID末尾 c3d4）")
        self.assertIs(c["name_stale"], True)
        self.assertEqual(c["name"], "")
        self.assertNotIn(MEMBER, c["label"])
        self.assertNotIn(GROUP, c["label"])
        self.set_name(MEMBER, "佐藤 一郎")
        c = self.chat("--include-groups")
        self.assertEqual(c["user_id"], MEMBER)
        self.assertEqual(c["label"], "佐藤 一郎")
        self.assertIs(c["name_stale"], False)
        self.inbound(OTHER, "私も質問です", source="group", ago=60)
        c = self.chat("--include-groups")
        self.assertEqual(c["user_id"], OTHER)
        self.assertEqual(c["short_id"], "e5f6")
        self.assertEqual(c["label"], "名前不明（ID末尾 e5f6）")
        self.assertIs(c["name_stale"], True)

    def test_plain_unreplied_has_who_label(self):
        self.inbound(USER, "こんにちは")
        self.inbound(OTHER, "質問です")
        quoted = 'ボブ "B"'
        self.set_name(OTHER, quoted)
        out = self.plain()
        self.assertIn('who="名前不明（ID末尾 a1b2）"', out)
        self.assertIn("who=" + json.dumps(quoted, ensure_ascii=False), out)
        self.assertEqual(out.count("who="), 2, out)
        self.set_name(USER, NAME)
        out = self.plain()
        self.assertIn('who="田中 花子"', out)
        self.assertNotIn("名前不明", out)
        self.assertEqual(out.count("who="), 2, out)

    def test_log_event_lines_end_with_user_id(self):
        first = self.body(USER, "こんにちは")
        line = self.event_line(first)
        self.assertTrue(line.startswith("inserted "), line)
        self.assertTrue(line.endswith(" user_id=" + USER), line)
        line = self.event_line(self.body(MEMBER, "グループです", source="group"))
        self.assertTrue(line.startswith("inserted "), line)
        self.assertTrue(line.endswith(" user_id=" + MEMBER), line)
        follow = self.event("follow", OTHER)
        follow["follow"] = {"isUnblocked": False}
        line = self.event_line(self.wrap(follow))
        self.assertTrue(line.startswith("inserted "), line)
        self.assertTrue(line.endswith(" user_id=" + OTHER), line)
        self.assertEqual(self.count("messages", "user_id=?", OTHER), 1)
        again = json.loads(first)
        again["events"][0]["deliveryContext"]["isRedelivery"] = True
        before = self.count("messages")
        line = self.event_line(json.dumps(again))
        self.assertTrue(line.startswith("duplicate"), line)
        self.assertTrue(line.endswith(" user_id=" + USER), line)
        self.assertEqual(self.count("messages"), before)

    def test_old_db_without_profile_at(self):
        self.inbound(USER, "予約できますか")
        self.inbound(OTHER, "営業時間は？")
        self.drop_profile_at()
        self.execute("INSERT OR IGNORE INTO customers(user_id) VALUES(?)", USER)
        self.execute("UPDATE customers SET display_name=?, updated_at=? WHERE user_id=?",
                     NAME, "2026-01-05T10:00:00+09:00", USER)
        found = {c["user_id"]: c for c in self.chats()}
        self.assertEqual(set(found), {USER, OTHER})
        self.assertEqual(found[USER]["label"], NAME)
        self.assertEqual(found[USER]["name"], NAME)
        self.assertIs(found[USER]["name_stale"], True)
        self.assertEqual(found[OTHER]["label"], "名前不明（ID末尾 e5f6）")
        self.assertEqual(found[OTHER]["name"], "")
        self.assertIs(found[OTHER]["name_stale"], True)
        out = self.plain()
        self.assertIn('who="田中 花子"', out)
        self.assertIn('who="名前不明（ID末尾 e5f6）"', out)
        code, out, err = self.run_script("init_db")
        self.assertEqual(code, 0, err)
        self.assertIn("table customers: added column profile_at", out)
        self.assertIn("profile_at", [c["name"] for c in self.q("PRAGMA table_info(customers)")])
        found = {c["user_id"]: c for c in self.chats()}
        self.assertEqual(found[USER]["label"], NAME)
        self.assertIs(found[USER]["name_stale"], True)
        self.set_name(USER, NAME)
        found = {c["user_id"]: c for c in self.chats()}
        self.assertIs(found[USER]["name_stale"], False)
        self.assertEqual(found[OTHER]["label"], "名前不明（ID末尾 e5f6）")


    def nick(self, user, *args, stdin=""):
        return self.pc("nickname", user, *args, stdin=stdin)

    def test_nickname_is_the_label_and_survives_a_name_refresh(self):
        self.inbound(USER, "予約できますか")
        self.set_name(USER, NAME)
        code, out, err = self.nick(USER, "--name", "-", stdin=" 田中さん（美容室）\n")
        self.assertEqual((code, out.strip()), (0, "saved"), err)
        p = self.profile(USER)
        self.assertEqual(p["nickname"], "田中さん（美容室）")
        self.assertEqual(p["display_name"], NAME)
        self.assertEqual(p["label"], "田中さん（美容室）")
        self.assertEqual(self.label(USER), "田中さん（美容室）")
        self.assertEqual(self.pc("get", USER)[1].strip(), NAME)
        self.set_name(USER, "はなこ")
        p = self.profile(USER)
        self.assertEqual((p["display_name"], p["nickname"]), ("はなこ", "田中さん（美容室）"))
        c = self.chat()
        self.assertEqual((c["label"], c["name"], c["nickname"]), ("田中さん（美容室）", "はなこ", "田中さん（美容室）"))
        self.assertIn('who="田中さん（美容室）"', self.plain())

    def test_nickname_from_file_and_clear(self):
        path = os.path.join(self.dir, "nick.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("田中A")
        code, out, err = self.nick(USER, "--name-file", path)
        self.assertEqual((code, out.strip()), (0, "saved"), err)
        self.assertEqual(self.label(USER), "田中A")
        self.assertIsNone(self.profile(USER)["display_name"])
        code, out, err = self.nick(USER, "--clear")
        self.assertEqual((code, out.strip()), (0, "cleared"), err)
        self.assertIsNone(self.profile(USER)["nickname"])
        self.assertEqual(self.label(USER), FALLBACK)
        code, out, err = self.nick(USER, "--name", "-", stdin=" \n")
        self.assertEqual(code, 1)
        self.assertIn("name is empty", err)

    def test_nickname_shown_for_another_person_is_refused(self):
        self.set_name(USER, NAME)
        self.set_name(OTHER, "鈴木")
        code, out, err = self.nick(OTHER, "--name", "-", stdin="田中　花子")
        self.assertEqual(code, 1)
        self.assertIn("ID末尾 a1b2", err)
        self.assertNotIn(USER, err)
        self.assertIsNone(self.profile(OTHER)["nickname"])
        self.assertEqual(self.nick(USER, "--name", "-", stdin="TANAKA")[0], 0)
        self.assertEqual(self.nick(OTHER, "--name", "-", stdin=NAME)[0], 0)
        code, out, err = self.nick(OTHER, "--name", "-", stdin="ｔａｎａｋａ")
        self.assertEqual(code, 1)
        self.assertEqual(self.label(OTHER), NAME)
        self.assertEqual(self.nick(USER, "--name", "-", stdin="tanaka")[0], 0)
        self.assertEqual(self.nick(MEMBER, "--name", "-", stdin="鈴木")[0], 0)

    def test_nickname_needs_init_db_on_an_old_db(self):
        self.set_name(USER, NAME)
        self.execute("ALTER TABLE customers DROP COLUMN nickname")
        p = self.profile(USER)
        self.assertIsNone(p["nickname"])
        self.assertEqual(p["label"], NAME)
        self.inbound(USER, "こんにちは")
        self.assertEqual(self.chat()["nickname"], "")
        code, out, err = self.nick(USER, "--name", "-", stdin="田中A")
        self.assertEqual(code, 1)
        self.assertIn("customers.nickname is missing", err)
        code, out, err = self.run_script("init_db")
        self.assertEqual(code, 0, err)
        self.assertIn("table customers: added column nickname", out)
        self.assertEqual(self.nick(USER, "--name", "-", stdin="田中A")[0], 0)
        self.assertEqual(self.label(USER), "田中A")

def main():
    before = real_db_stat()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DisplayNameTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    untouched = real_db_stat() == before
    print("summary: %d run, %d failures, %d errors; %s %s" % (
        result.testsRun, len(result.failures), len(result.errors), REAL_DB,
        "unchanged" if untouched else "CHANGED"))
    return 0 if result.wasSuccessful() and untouched else 1


if __name__ == "__main__":
    sys.exit(main())
