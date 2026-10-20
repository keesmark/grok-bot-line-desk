#!/usr/bin/env python3
"""Mark a LINE chat as read up to one inbound message.

Usage: mark_read.py <messages.id>

Sends that message's markAsReadToken to POST /v2/bot/chat/markAsRead.
LINE marks that message and all earlier ones in the same chat as read.
On success, read_at is set on that chat's inbound messages at or before it.

Env: LINE_CHANNEL_ACCESS_TOKEN  required, never printed
     DESK_DB                    required (this bot's own DB path; no default)
"""
import argparse
import json
import os
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
MARK_READ_URL = "https://api.line.me/v2/bot/chat/markAsRead"
# Chat key: group_id for groups, room_id for rooms, user_id otherwise.
CHAT_KEY_SQL = ("CASE source_type WHEN 'group' THEN group_id "
                "WHEN 'room' THEN room_id ELSE user_id END")


class Fail(Exception):
    pass


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def chat_key(source_type, user_id, group_id, room_id):
    if source_type == "group":
        return group_id
    if source_type == "room":
        return room_id
    return user_id


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


def read_token_of(raw, mid):
    if not raw:
        raise Fail("no raw event stored for message %d" % mid)
    try:
        event = json.loads(raw)
    except ValueError:
        raise Fail("raw event of message %d is not valid JSON" % mid)
    message = event.get("message") if isinstance(event, dict) else None
    token = message.get("markAsReadToken") if isinstance(message, dict) else None
    if not token:
        raise Fail("no markAsReadToken in the event of message %d" % mid)
    return token


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


def call_mark_as_read(read_token, access_token):
    req = urllib.request.Request(
        MARK_READ_URL,
        data=json.dumps({"markAsReadToken": read_token}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + access_token})

    def send():
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status

    try:
        status = retry_once(send)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        raise Fail("LINE API returned HTTP %s: %s"
                   % (e.code, clip(body, access_token)))
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise Fail("could not reach api.line.me (%s)"
                   % clip(reason, access_token))
    if status != 200:
        raise Fail("LINE API returned HTTP %s" % status)


def mark_read(mid):
    db = os.environ.get("DESK_DB", "")
    if not db.strip():
        raise Fail(NO_DB)
    conn = open_db(db)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)")]
        if not cols:
            raise Fail("table messages not found in %s (run init_db.py first)"
                       % db)
        if "read_at" not in cols:
            raise Fail("column messages.read_at is missing "
                       "(run init_db.py first)")
        row = conn.execute(
            "SELECT direction, at, source_type, user_id, group_id, room_id, raw"
            " FROM messages WHERE id=?", (mid,)).fetchone()
        if row is None:
            raise Fail("no message with id %d" % mid)
        direction, at, source_type, user_id, group_id, room_id, raw = row
        if direction != "in":
            raise Fail("message %d is outbound; give the id of an inbound "
                       "message" % mid)
        key = chat_key(source_type, user_id, group_id, room_id)
        if not key:
            raise Fail("message %d has no chat id" % mid)
        read_token = read_token_of(raw, mid)

        access_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
        if not access_token:
            raise Fail("LINE_CHANNEL_ACCESS_TOKEN is not set")
        call_mark_as_read(read_token, access_token)

        with conn:
            cur = conn.execute(
                "UPDATE messages SET read_at=? WHERE direction='in'"
                " AND read_at IS NULL AND at<=? AND " + CHAT_KEY_SQL + "=?",
                (now_iso(), at, key))
        return cur.rowcount
    finally:
        conn.close()


def main(argv):
    parser = argparse.ArgumentParser(
        description="Mark a LINE chat as read up to one inbound message.")
    parser.add_argument("message_id", type=int,
                        help="messages.id of an inbound message")
    args = parser.parse_args(argv)
    try:
        n = mark_read(args.message_id)
    except Fail as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    except sqlite3.Error as e:
        print("error: database problem: %s" % e, file=sys.stderr)
        return 1
    print("ok, marked %d messages read" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
