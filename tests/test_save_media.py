import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
spec = importlib.util.spec_from_file_location("save_media", SCRIPTS / "save_media.py")
save_media = importlib.util.module_from_spec(spec)
spec.loader.exec_module(save_media)


class FakeResponse(io.BytesIO):
    def __init__(self, data, status=200, content_type="image/jpeg"):
        io.BytesIO.__init__(self, data)
        self.status = status
        self.headers = {"Content-Type": content_type}

    def __exit__(self, *exc):
        self.close()


class SaveMediaJsonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "messages.db")
        env = dict(os.environ, DESK_DB=self.db)
        subprocess.run([sys.executable, "-I", str(SCRIPTS / "init_db.py")], env=env, check=True,
                       capture_output=True)
        self.env = mock.patch.dict(os.environ, {"DESK_DB": self.db, "LINE_CHANNEL_ACCESS_TOKEN": "t"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def add_image(self, provider="line", media_path=None):
        raw = json.dumps({"message": {"type": "image", "id": "111", "contentProvider": {"type": provider}}})
        with sqlite3.connect(self.db) as con:
            cur = con.execute(
                "INSERT INTO messages(direction, at, message_type, line_message_id, raw, media_path) "
                "VALUES('in', '2026-10-20T00:00:00Z', 'image', '111', ?, ?)", (raw, media_path))
            mid = cur.lastrowid
        con.close()
        return mid

    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = save_media.main(list(argv))
        return code, out.getvalue()

    def test_saved_prints_json(self):
        mid = self.add_image()
        with mock.patch.object(save_media, "open_content", return_value=FakeResponse(b"jpeg")):
            code, out = self.run_main(str(mid), "--json")
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual((result["saved"], result["already"], result["id"], result["bytes"]),
                         (True, False, mid, 4))
        self.assertTrue(os.path.isfile(result["path"]))

    def test_already_saved_prints_json(self):
        path = os.path.join(self.tmp.name, "old.jpg")
        Path(path).write_bytes(b"x")
        mid = self.add_image(media_path=path)
        code, out = self.run_main(str(mid), "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"saved": True, "already": True, "id": mid, "path": path})

    def test_already_saved_prints_text_without_json(self):
        path = os.path.join(self.tmp.name, "old.jpg")
        Path(path).write_bytes(b"x")
        mid = self.add_image(media_path=path)
        code, out = self.run_main(str(mid))
        self.assertEqual(code, 0)
        self.assertEqual(out, "already saved %s\n" % path)

    def test_external_content_prints_json_with_exit_5(self):
        mid = self.add_image(provider="external")
        code, out = self.run_main(str(mid), "--json")
        self.assertEqual(code, 5)
        self.assertEqual(json.loads(out), {"saved": False, "id": mid, "exit": 5,
                                           "reason": "external content, not downloadable"})

    def test_not_ready_prints_json_with_exit_4(self):
        mid = self.add_image()
        with mock.patch.object(save_media, "open_content", return_value=FakeResponse(b"", status=202)):
            code, out = self.run_main(str(mid), "--json")
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(out)["exit"], 4)
        self.assertFalse(json.loads(out)["saved"])

    def test_not_ready_prints_text_without_json(self):
        mid = self.add_image()
        with mock.patch.object(save_media, "open_content", return_value=FakeResponse(b"", status=202)):
            code, out = self.run_main(str(mid))
        self.assertEqual(code, 4)
        self.assertEqual(out, "content not ready yet, try again later\n")


if __name__ == "__main__":
    unittest.main()
