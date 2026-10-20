#!/usr/bin/env python3
"""Save the content of a customer's image, video, audio or file message.

Usage: save_media.py <messages.id> [--preview] [--force] [--json]

Downloads GET https://api-data.line.me/v2/bot/message/{id}/content
(or .../content/preview with --preview, image/video only) into
<DB dir>/media/ and records the path in messages.media_path (or
preview_path) and media_saved_at. LINE deletes such content after a
while, so save it early. Never sends LINE messages.

Exit: 0 saved or already saved, 1 error, 4 not ready yet, 5 external content

Env: LINE_CHANNEL_ACCESS_TOKEN  required, never printed
     DESK_DB                    required (this bot's own DB path; no default)
"""
import argparse
import http.client
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

NO_DB = ("DESK_DB is not set: give this bot's own database path, e.g. "
         "DESK_DB=/workspace/line-inbox/<agent id>/messages.db "
         "(see 保存先 in memory.md); not using a shared default")
CONTENT_URL = "https://api-data.line.me/v2/bot/message/%s/content"
MEDIA_TYPES = ("image", "video", "audio", "file")
PREVIEW_TYPES = ("image", "video")
NEEDED_COLUMNS = ("direction", "message_type", "line_message_id", "raw",
                  "media_path", "preview_path", "media_saved_at")
EXTENSIONS = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "video/mp4": ".mp4", "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a", "audio/mp4": ".m4a", "audio/mpeg": ".mp3",
    "application/pdf": ".pdf",
}
NOT_AVAILABLE = ("content not available (it may have been deleted by LINE "
                 "after its storage period)")
CHUNK = 64 * 1024


class Fail(Exception):
    pass


class NotSaved(Exception):
    """Not an error, but nothing was saved; ends with its own exit code."""

    def __init__(self, message, code):
        Exception.__init__(self, message)
        self.code = code


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def clip(text, secret):
    """One line, secret removed, at most 200 chars."""
    text = str(text)
    if secret:
        text = text.replace(secret, "***")
    return " ".join(text.split())[:200]


def open_db(path):
    if not os.path.isfile(path):
        raise Fail("database not found: %s (run init_db.py first)" % path)
    uri = "file:%s?mode=rw" % urllib.parse.quote(path)
    return sqlite3.connect(uri, uri=True, timeout=10)


def message_of(raw):
    """The message object of the stored raw event, or {} if unreadable."""
    try:
        event = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        event = {}
    message = event.get("message") if isinstance(event, dict) else None
    return message if isinstance(message, dict) else {}


def is_external(message):
    """True if contentProvider.type is given and is not "line"."""
    provider = message.get("contentProvider")
    return isinstance(provider, dict) and provider.get("type", "line") != "line"


def extension(content_type, message_type, message):
    if content_type in EXTENSIONS:
        return EXTENSIONS[content_type]
    name = message.get("fileName")
    if message_type == "file" and isinstance(name, str) and "." in name:
        ext = name.rsplit(".", 1)[1]
        if re.fullmatch(r"[A-Za-z0-9]{1,8}", ext):
            return "." + ext.lower()
    return ".bin"


def retry_once(fn):
    """Run fn(); on a connection error, wait 2 seconds and run it once more.

    HTTP errors (any status) are never retried.
    """
    try:
        return fn()
    except urllib.error.HTTPError:
        raise
    except OSError:  # URLError, socket timeout, ConnectionError
        time.sleep(2)
        return fn()


def open_content(url, access_token):
    req = urllib.request.Request(url, method="GET")
    # Unredirected: the token is not passed on if LINE redirects elsewhere.
    req.add_unredirected_header("Authorization", "Bearer " + access_token)
    try:
        return retry_once(lambda: urllib.request.urlopen(req, timeout=10))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise Fail(NOT_AVAILABLE)
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        raise Fail("LINE API returned HTTP %s: %s"
                   % (e.code, clip(body, access_token)))
    except (OSError, http.client.HTTPException) as e:
        reason = getattr(e, "reason", e)
        raise Fail("could not reach api-data.line.me (%s)"
                   % clip(reason, access_token))


def write_file(resp, path, access_token):
    """Stream resp into path.part, then rename it to path. Return bytes."""
    part = path + ".part"
    flags = (os.O_WRONLY | os.O_CREAT | os.O_TRUNC
             | getattr(os, "O_NOFOLLOW", 0))
    size = 0
    try:
        with os.fdopen(os.open(part, flags, 0o600), "wb") as f:
            while True:
                try:
                    chunk = resp.read(CHUNK)
                except (OSError, http.client.HTTPException) as e:
                    raise Fail("download interrupted (%s)"
                               % clip(e, access_token))
                if not chunk:
                    break
                f.write(chunk)
                size += len(chunk)
        os.replace(part, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    return size


def save_media(mid, preview, force):
    db = os.environ.get("DESK_DB", "")
    if not db.strip():
        raise Fail(NO_DB)
    conn = open_db(db)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)")]
        if not cols:
            raise Fail("table messages not found in %s (run init_db.py first)"
                       % db)
        for col in NEEDED_COLUMNS:
            if col not in cols:
                raise Fail("column messages.%s is missing "
                           "(run init_db.py first)" % col)
        kind = "preview" if preview else "content"
        column = "preview_path" if preview else "media_path"
        row = conn.execute(
            "SELECT direction, message_type, line_message_id, raw, " + column
            + " FROM messages WHERE id=?", (mid,)).fetchone()
        if row is None:
            raise Fail("no message with id %d" % mid)
        direction, message_type, line_id, raw, old_path = row
        if direction != "in":
            raise Fail("message %d is outbound; give the id of an inbound "
                       "message" % mid)
        if message_type not in MEDIA_TYPES:
            raise Fail("message %d is a %s message; only image, video, audio "
                       "and file messages have content"
                       % (mid, clip(message_type, "")))
        if preview and message_type not in PREVIEW_TYPES:
            raise Fail("previews exist only for image and video messages")
        if not line_id:
            raise Fail("message %d has no LINE message id" % mid)
        if old_path and os.path.isfile(old_path) and not force:
            return {"already": old_path}

        message = message_of(raw)
        if is_external(message):
            raise NotSaved("external content, not downloadable", 5)

        access_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
        if not access_token:
            raise Fail("LINE_CHANNEL_ACCESS_TOKEN is not set")

        media_dir = os.path.join(os.path.dirname(os.path.abspath(db)), "media")
        os.makedirs(media_dir, mode=0o700, exist_ok=True)
        os.chmod(media_dir, 0o700)

        url = CONTENT_URL % urllib.parse.quote(str(line_id), safe="")
        if preview:
            url += "/preview"
        with open_content(url, access_token) as resp:
            if resp.status != 200:
                if 200 <= resp.status < 300:
                    raise NotSaved("content not ready yet, try again later", 4)
                raise Fail("LINE API returned HTTP %s" % resp.status)
            content_type = clip(resp.headers.get("Content-Type") or "", "")
            content_type = content_type.split(";")[0].strip().lower()
            ext = extension(content_type, message_type, message)
            path = os.path.join(media_dir, "%d-%s%s" % (mid, kind, ext))
            size = write_file(resp, path, access_token)

        with conn:
            conn.execute("UPDATE messages SET " + column
                         + "=?, media_saved_at=? WHERE id=?",
                         (path, now_iso(), mid))
        return {"id": mid, "path": path, "bytes": size,
                "content_type": content_type or "unknown", "kind": kind}
    finally:
        conn.close()


def main(argv):
    parser = argparse.ArgumentParser(
        description="Save the content of a customer's image, video, audio "
                    "or file message.")
    parser.add_argument("message_id", type=int,
                        help="messages.id of an inbound media message")
    parser.add_argument("--preview", action="store_true",
                        help="save the preview image (image/video only)")
    parser.add_argument("--force", action="store_true",
                        help="download again even if already saved")
    parser.add_argument("--json", action="store_true",
                        help="print the result as JSON")
    args = parser.parse_args(argv)
    try:
        result = save_media(args.message_id, args.preview, args.force)
    except NotSaved as e:
        print(e)
        return e.code
    except Fail as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    except sqlite3.Error as e:
        print("error: database problem: %s" % e, file=sys.stderr)
        return 1
    except OSError as e:
        print("error: file problem: %s" % e, file=sys.stderr)
        return 1
    if "already" in result:
        print("already saved %s" % result["already"])
    elif args.json:
        print(json.dumps(result))
    else:
        print("saved %s (%d bytes, %s)"
              % (result["path"], result["bytes"], result["content_type"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
