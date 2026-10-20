import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import stat
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.dont_write_bytecode = True
SCRIPTS = os.environ.get("SCRIPTS") or os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts"))
REAL_DB = "/workspace/line-inbox/messages.db"
TOKEN = "test-token-SECRET123"
LINE_ID = "500000000000000001"
URL = "https://api-data.line.me/v2/bot/message/%s/content" % LINE_ID
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 600
EXTERNAL = {"type": "external", "originalContentUrl": "https://example.com/a.jpg",
            "previewImageUrl": "https://example.com/a-small.jpg"}
CAPTURED = []
_MODULES = {}


def real_db_state():
    try:
        st = os.stat(REAL_DB)
    except FileNotFoundError:
        return None
    return st.st_mtime_ns, st.st_size


REAL_BEFORE = real_db_state()


def load(name):
    if name not in _MODULES:
        spec = importlib.util.spec_from_file_location(
            "desk_" + name, os.path.join(SCRIPTS, name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MODULES[name] = mod
    return _MODULES[name]


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def refused():
    return urllib.error.URLError("connection refused " + TOKEN)


def http_error(code, body):
    return urllib.error.HTTPError(URL, code, "error", {}, io.BytesIO(body))


class FakeResp:

    def __init__(self, body=JPEG, status=200, ctype="image/jpeg", cut=False):
        self.body, self.status, self.cut = io.BytesIO(body), status, cut
        self.headers = {"Content-Type": ctype}

    def read(self, n=-1):
        data = self.body.read(n)
        if not data and self.cut:
            raise ConnectionResetError("connection reset " + TOKEN)
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.body.close()


class FakeNet:

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, req, timeout=None):
        self.calls.append(req)
        if not self.replies:
            raise AssertionError("unexpected network request")
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


class SaveMediaTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = os.path.join(tmp.name, "messages.db")
        self.media = os.path.join(tmp.name, "media")
        env = mock.patch.dict(os.environ, {"DESK_DB": self.db,
                                           "LINE_CHANNEL_ACCESS_TOKEN": TOKEN})
        env.start()
        self.addCleanup(env.stop)
        self.assertNotEqual(os.path.realpath(os.environ["DESK_DB"]),
                            os.path.realpath(REAL_DB))
        rc, out, err = self.call("init_db")
        self.assertEqual(rc, 0, out + err)

    def call(self, script, *args, net=None):
        mod = load(script)
        self.net, self.sleep = net or FakeNet(), mock.Mock()
        out, err = io.StringIO(), io.StringIO()
        fake_net = mock.patch.object(getattr(mod, "urllib", urllib).request,
                                     "urlopen", self.net)
        no_sleep = mock.patch.object(time, "sleep", self.sleep)
        with fake_net, no_sleep, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = mod.main(list(args))
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else int(e.code is not None)
        out, err = out.getvalue(), err.getvalue()
        CAPTURED.extend((out, err))
        self.assertNotIn(TOKEN, out + err)
        return rc, out, err

    def save(self, *args, net=None):
        return self.call("save_media", *map(str, args), net=net)

    def add(self, mtype="image", provider=None):
        message = {"id": LINE_ID, "type": mtype}
        if mtype == "text":
            message["text"] = "hello"
        else:
            message["contentProvider"] = provider or {"type": "line"}
        user = "U" + "1" * 32
        raw = {"type": "message", "webhookEventId": "01TESTEVENT", "message": message,
               "source": {"type": "user", "userId": user}}
        con = sqlite3.connect(self.db)
        try:
            with con:
                return con.execute(
                    "INSERT INTO messages(direction, at, source_type, user_id, message_type,"
                    " text, line_message_id, raw)"
                    " VALUES('in', datetime('now'), 'user', ?, ?, ?, ?, ?)",
                    (user, mtype, message.get("text"), LINE_ID, json.dumps(raw))).lastrowid
        finally:
            con.close()

    def row(self, mid):
        con = sqlite3.connect(self.db)
        try:
            return con.execute("SELECT media_path, media_saved_at FROM messages WHERE id=?",
                               (mid,)).fetchone()
        finally:
            con.close()

    def files(self):
        return sorted(os.listdir(self.media)) if os.path.isdir(self.media) else []

    def assertNothingSaved(self, mid):
        self.assertEqual(self.files(), [])
        self.assertEqual(self.row(mid), (None, None))

    def test_saves_image(self):
        mid = self.add()
        rc, out, err = self.save(mid, net=FakeNet(FakeResp()))
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len(self.net.calls), 1)
        req = self.net.calls[0]
        self.assertEqual((req.get_method(), req.full_url), ("GET", URL))
        self.assertEqual(req.unredirected_hdrs.get("Authorization"), "Bearer " + TOKEN)
        self.assertNotIn("Authorization", req.headers)
        path, saved_at = self.row(mid)
        self.assertTrue(saved_at)
        self.assertEqual(os.path.realpath(os.path.dirname(path)), os.path.realpath(self.media))
        self.assertTrue(path.endswith(".jpg"), path)
        self.assertIn(path, out)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), JPEG)
        self.assertEqual(mode(path), 0o600)
        self.assertEqual(mode(self.media), 0o700)
        self.assertEqual(self.files(), [os.path.basename(path)])

    def test_second_run_skips_and_force_downloads_again(self):
        mid = self.add()
        self.assertEqual(self.save(mid, net=FakeNet(FakeResp()))[0], 0)
        path = self.row(mid)[0]
        rc, out, err = self.save(mid)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("already", out)
        self.assertIn(path, out)
        self.assertEqual(self.net.calls, [])
        rc, out, err = self.save(mid, "--force", "--json", net=FakeNet(FakeResp(b"new picture")))
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len(self.net.calls), 1)
        result = json.loads(out)
        self.assertEqual((result["path"], result["bytes"]), (path, 11))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"new picture")
        self.assertEqual(mode(path), 0o600)
        self.assertEqual(self.files(), [os.path.basename(path)])

    def test_not_ready_and_cut_download_save_nothing(self):
        mid = self.add()
        rc, out, err = self.save(mid, net=FakeNet(FakeResp(b"", status=202)))
        self.assertEqual(rc, 4, out + err)
        self.assertIn("ready", (out + err).lower())
        self.assertNothingSaved(mid)
        rc, out, err = self.save(mid, net=FakeNet(FakeResp(cut=True)))
        self.assertEqual(rc, 1, out + err)
        self.assertIn("error", err)
        self.assertNothingSaved(mid)

    def test_external_content_is_not_requested(self):
        mid = self.add(provider=EXTERNAL)
        rc, out, err = self.save(mid)
        self.assertEqual(rc, 5, out + err)
        self.assertIn("external", (out + err).lower())
        self.assertEqual(self.net.calls, [])
        self.assertNothingSaved(mid)

    def test_http_errors_are_not_retried(self):
        mid = self.add()
        for code, body in ((404, b'{"message":"Not found"}'),
                           (401, ('{"message":"invalid token %s"}' % TOKEN).encode())):
            with self.subTest(code=code):
                rc, out, err = self.save(mid, net=FakeNet(http_error(code, body)))
                self.assertEqual(rc, 1, out + err)
                self.assertIn("error", err)
                self.assertEqual(len(self.net.calls), 1)
                self.sleep.assert_not_called()
                self.assertNothingSaved(mid)

    def test_connection_error_is_retried_once(self):
        mid = self.add()
        rc, out, err = self.save(mid, net=FakeNet(refused(), FakeResp()))
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len(self.net.calls), 2)
        self.sleep.assert_called_once()
        self.assertTrue(self.row(mid)[0])
        rc, out, err = self.save(mid, "--force", net=FakeNet(refused(), refused()))
        self.assertEqual(rc, 1, out + err)
        self.assertIn("error", err)
        self.assertEqual(len(self.net.calls), 2)

    def test_rejects_text_message_and_bad_ids(self):
        mid = self.add("text")
        for arg in (mid, mid + 100, "abc"):
            with self.subTest(arg=arg):
                rc, out, err = self.save(arg)
                self.assertNotEqual(rc, 0, out + err)
                self.assertIn("error", err)
                self.assertEqual(self.net.calls, [])
        self.assertNothingSaved(mid)

    def test_missing_token(self):
        mid = self.add()
        os.environ["LINE_CHANNEL_ACCESS_TOKEN"] = "  "
        blank = self.save(mid)
        del os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
        for rc, out, err in (blank, self.save(mid)):
            self.assertEqual(rc, 1, out + err)
            self.assertIn("LINE_CHANNEL_ACCESS_TOKEN", err)
        self.assertEqual(self.net.calls, [])
        self.assertNothingSaved(mid)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    real_ok = real_db_state() == REAL_BEFORE
    leaked = any(TOKEN in s for s in CAPTURED)
    ok = result.wasSuccessful() and real_ok and not leaked
    print("summary: %d run, %d failed, %d errors, %d skipped; real DB unchanged: %s;"
          " token leaked: %s -> %s"
          % (result.testsRun, len(result.failures), len(result.errors),
             len(result.skipped), "yes" if real_ok else "NO",
             "YES" if leaked else "no", "OK" if ok else "FAIL"))
    sys.exit(0 if ok else 1)
