import contextlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest

sys.dont_write_bytecode = True
SCRIPTS = os.environ.get("SCRIPTS") or os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts"))
REAL_DIR = "/workspace/line-inbox"
REAL_DB = REAL_DIR + "/messages.db"
TOKEN = "test-token-L"
PROXY = "http://127.0.0.1:9"
NOT_SET = "DESK_DB is not set"
ZERO = "U" + "0" * 32
USER = "U" + "0" * 28 + "a1b2"
DEST = "U" + "0" * 28 + "9c8a"
BOT1, BOT2 = "agent-1234", "agent-5678"
EMPTY_BODY = '{"destination":"' + ZERO + '","events":[]}'
CALLS = (
    ("init_db.py", (), ""),
    ("log_event.py", (), EMPTY_BODY),
    ("list_unreplied.py", (), ""),
    ("set_status.py", (ZERO, "完了"), ""),
    ("mark_read.py", ("1",), ""),
    ("quota_check.py", ("--peek",), ""),
    ("stats.py", (), ""),
    ("purge_old.py", ("--before", "2000-01-01"), ""),
    ("profile_cache.py", ("get", ZERO), ""),
    ("canned.py", ("list",), ""),
    ("save_media.py", ("1",), ""),
    ("keyword_reply.py", ("list",), ""),
    ("account.py", ("show",), ""),
)
_suite_before = None


def real_state():
    try:
        st = os.stat(REAL_DB)
        db = (st.st_mtime_ns, st.st_size)
    except OSError:
        db = None
    try:
        entries = frozenset(os.listdir(REAL_DIR))
    except OSError:
        entries = None
    return db, entries


def real_state_problems(before, after):
    problems = []
    if after[0] != before[0]:
        problems.append(f"{REAL_DB} changed: {before[0]} -> {after[0]}")
    if before[1] is None:
        if after[1] is not None:
            problems.append(f"{REAL_DIR} was created: {sorted(after[1])}")
    else:
        new = sorted((after[1] or frozenset()) - before[1])
        if new:
            problems.append(f"new entries in {REAL_DIR}: {new}")
    return problems


def file_stamp(path):
    st = os.lstat(path)
    return st.st_mtime_ns, st.st_size


def folder_state(path):
    return {name: file_stamp(os.path.join(path, name)) for name in os.listdir(path)}


def mode(path):
    return oct(stat.S_IMODE(os.stat(path).st_mode))


def count_messages(db):
    with contextlib.closing(sqlite3.connect(db)) as con:
        return con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


def webhook_body():
    return json.dumps({
        "destination": DEST,
        "events": [{
            "type": "message",
            "mode": "active",
            "timestamp": int(time.time() * 1000),
            "source": {"type": "user", "userId": USER},
            "webhookEventId": "01JDESKL" + "0" * 18,
            "deliveryContext": {"isRedelivery": False},
            "replyToken": "0" * 31 + "1",
            "message": {"type": "text", "id": "500000000000000001",
                        "quoteToken": "q-test-L", "text": "明日は営業していますか？"},
        }],
    }, ensure_ascii=False)


def script_env(desk_db):
    env = dict(os.environ)
    for key in ("DESK_DB", "no_proxy", "NO_PROXY"):
        env.pop(key, None)
    env["LINE_CHANNEL_ACCESS_TOKEN"] = TOKEN
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        env[key] = PROXY
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if desk_db is not None:
        env["DESK_DB"] = desk_db
    return env


def run_script(name, args, stdin, desk_db, cwd):
    return subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, name), *args],
        input=stdin, capture_output=True, encoding="utf-8", errors="replace",
        env=script_env(desk_db), cwd=cwd, timeout=60,
    )


def describe(proc):
    return f"exit={proc.returncode}\nstdout={proc.stdout[-1500:]!r}\nstderr={proc.stderr[-1500:]!r}"


def setUpModule():
    global _suite_before
    _suite_before = real_state()


def tearDownModule():
    problems = real_state_problems(_suite_before, real_state())
    if problems:
        raise AssertionError("whole suite: " + "; ".join(problems))


class DeskDbRequiredTest(unittest.TestCase):
    def setUp(self):
        self.real_before = real_state()
        tmp = tempfile.TemporaryDirectory(prefix="desk-l-")
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.inbox = os.path.join(self.tmp, "line-inbox")

    def tearDown(self):
        self.assertEqual(real_state_problems(self.real_before, real_state()), [],
                         "the shared DB or its folder changed during this test")

    def new_cwd(self):
        return tempfile.mkdtemp(prefix="cwd-", dir=self.tmp)

    def bot_db(self, agent):
        return os.path.join(self.inbox, agent, "messages.db")

    def assert_quiet(self, proc, cwd, what):
        self.assertNotIn(TOKEN, proc.stdout + proc.stderr, f"{what}: token printed")
        self.assertEqual(os.listdir(cwd), [], f"{what}: wrote into cwd\n{describe(proc)}")

    def assert_stops(self, name, args, stdin, desk_db):
        what = f"{name} {' '.join(args)} (DESK_DB={'unset' if desk_db is None else repr(desk_db)})"
        before = real_state()
        cwd = self.new_cwd()
        proc = run_script(name, args, stdin, desk_db, cwd)
        self.assertNotEqual(proc.returncode, 0, f"{what}: did not stop\n{describe(proc)}")
        self.assertIn(NOT_SET, proc.stderr, f"{what}\n{describe(proc)}")
        self.assert_quiet(proc, cwd, what)
        self.assertEqual(real_state_problems(before, real_state()), [], what)

    def init_bot(self, agent):
        db = self.bot_db(agent)
        what = f"init_db.py for {agent}"
        cwd = self.new_cwd()
        proc = run_script("init_db.py", (), "", db, cwd)
        self.assertEqual(proc.returncode, 0, f"{what}\n{describe(proc)}")
        self.assert_quiet(proc, cwd, what)
        folder = os.path.dirname(db)
        media = os.path.join(folder, "media")
        self.assertTrue(os.path.isfile(db), f"{what}: no DB at {db}")
        self.assertTrue(os.path.isdir(media), f"{what}: no media folder")
        self.assertEqual(mode(folder), "0o700", f"{what}: mode of the DB folder")
        self.assertEqual(mode(db), "0o600", f"{what}: mode of the DB")
        self.assertEqual(mode(media), "0o700", f"{what}: mode of media")
        return db

    def test_all_scripts_present(self):
        for name, _args, _stdin in CALLS:
            with self.subTest(script=name):
                self.assertTrue(os.path.isfile(os.path.join(SCRIPTS, name)), f"{name} missing in {SCRIPTS}")

    def test_no_default_db_in_sources(self):
        pattern = re.compile(r"DEFAULT_DB\s*=")
        names = sorted(n for n in os.listdir(SCRIPTS) if n.endswith(".py"))
        self.assertTrue(names, f"no scripts in {SCRIPTS}")
        for name in names:
            with self.subTest(script=name):
                with open(os.path.join(SCRIPTS, name), encoding="utf-8") as fh:
                    hits = [f"{name}:{no}: {line.strip()}" for no, line in enumerate(fh, 1) if pattern.search(line)]
                self.assertEqual(hits, [], "a shared default DB path is still defined")

    def test_missing_desk_db_stops_every_script(self):
        for name, args, stdin in CALLS:
            with self.subTest(script=name):
                self.assert_stops(name, args, stdin, None)

    def test_blank_desk_db_stops_every_script(self):
        for blank in ("  ", ""):
            for name, args, stdin in CALLS:
                with self.subTest(script=name, desk_db=blank):
                    self.assert_stops(name, args, stdin, blank)

    def test_quota_check_with_state_needs_no_desk_db(self):
        state_dir = os.path.join(self.tmp, "state")
        os.mkdir(state_dir, 0o700)
        cwd = self.new_cwd()
        proc = run_script("quota_check.py", ("--peek", "--state", os.path.join(state_dir, "quota.json")),
                          "", None, cwd)
        self.assertNotIn(NOT_SET, proc.stderr, describe(proc))
        self.assert_quiet(proc, cwd, "quota_check.py --peek --state")

    def test_init_db_creates_private_folder_per_bot(self):
        self.assertFalse(os.path.exists(self.inbox))
        db1 = self.init_bot(BOT1)
        before1 = folder_state(os.path.dirname(db1))
        db2 = self.init_bot(BOT2)
        self.assertEqual(folder_state(os.path.dirname(db1)), before1, "init_db.py for bot 2 changed bot 1's folder")
        self.assertEqual(sorted(os.listdir(self.inbox)), [BOT1, BOT2])
        self.assertFalse(os.path.samefile(db1, db2))
        for db in (db1, db2):
            with self.subTest(db=db):
                self.assertEqual(count_messages(db), 0)

    def test_log_event_writes_only_to_own_bot(self):
        db1, db2 = self.init_bot(BOT1), self.init_bot(BOT2)
        folder2 = os.path.dirname(db2)
        before2 = folder_state(folder2)
        cwd = self.new_cwd()
        proc = run_script("log_event.py", (), webhook_body(), db1, cwd)
        self.assertEqual(proc.returncode, 0, describe(proc))
        self.assert_quiet(proc, cwd, "log_event.py for bot 1")
        self.assertEqual(folder_state(folder2), before2, "log_event.py for bot 1 changed bot 2's folder")
        self.assertEqual(sorted(os.listdir(self.inbox)), [BOT1, BOT2])
        self.assertEqual(count_messages(db1), 1)
        self.assertEqual(count_messages(db2), 0)

    def test_keyword_setting_next_to_own_db(self):
        db1, db2 = self.init_bot(BOT1), self.init_bot(BOT2)
        folder1, folder2 = os.path.dirname(db1), os.path.dirname(db2)
        before2 = folder_state(folder2)
        cwd = self.new_cwd()
        proc = run_script("keyword_reply.py", ("setting", "off", "--json"), "", db1, cwd)
        self.assertEqual(proc.returncode, 0, describe(proc))
        self.assert_quiet(proc, cwd, "keyword_reply.py setting --json for bot 1")
        path = os.path.join(folder1, "keyword.json")
        self.assertTrue(os.path.isfile(path), f"no keyword.json next to {db1}")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        try:
            json.loads(text)
        except ValueError as exc:
            self.fail(f"keyword.json is not valid JSON: {exc}")
        self.assertFalse(os.path.exists(os.path.join(folder2, "keyword.json")), "keyword.json in bot 2's folder")
        self.assertEqual(folder_state(folder2), before2, "keyword_reply.py for bot 1 changed bot 2's folder")
        self.assertEqual(sorted(os.listdir(self.inbox)), [BOT1, BOT2], "keyword.json in the shared parent folder")


if __name__ == "__main__":
    unittest.main()
