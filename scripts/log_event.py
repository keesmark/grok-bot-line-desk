#!/usr/bin/env python3
"""Log LINE webhook events and sent messages into the Desk for LINE database.

Usage:
  log_event.py < body.json
      Log every event of a webhook body {"destination":..., "events":[...]}
      (or a single event object). Redelivered events print "duplicate".
      Each event prints one line ("inserted <id> ..." or "duplicate ...") that
      ends with user_id=<sender> ("-" if none): whose name to look up.
      The body's destination (the LINE account it was sent for, "U" + 32 hex)
      is stored on each row; the first body with events teaches this bot's
      account (desk_settings account_destination, see account.py). Before
      user_id= each line shows account=learned|same|other|unknown (unknown: no
      valid destination, e.g. a single event object, or init_db.py not run
      since this update) and, with a valid destination, dest=<last 4 chars>.
      account=other -> status other_account: recorded only: no reopen, not in
      the unreplied list, no keyword reply; tell the owner.
  log_event.py --out --to <userId> [--to <userId> ...] (--text-file <path> | --text -)
               [--line-message-id <id>]
               [--status sent|send_unknown|failed|app_reply|auto_reply|auto_reply_unknown|auto_reply_failed]
               [--error TEXT]
      Log a message the bot sent. One --to: a 1:1 push (send_kind push,
      prints "inserted <id>"). Two or more different --to: one message sent
      to that list of friends (send_kind segment, user_id empty, one
      send_recipients row per friend). --status app_reply means the owner
      replied from the LINE app (single --to only; text and message id are
      then optional). --status auto_reply / auto_reply_unknown /
      auto_reply_failed mark a keyword auto-reply (sent / result unknown /
      failed; logged by keyword.py sent, single --to only).
  log_event.py --out --broadcast (--text-file <path> | --text -) [--link-known]
               [--line-message-id <id>] [--status sent|send_unknown|failed]
               [--error TEXT]
      Log a broadcast to all friends (send_kind broadcast, user_id empty).
      --link-known also adds send_recipients rows (known_at_send=1) for the
      friends known in this DB now: users seen on inbound 1:1 rows whose
      latest follow/unfollow event is not unfollow. That is not a delivery
      confirmation.
  --error TEXT (with --out and --status failed, send_unknown, auto_reply_failed
      or auto_reply_unknown only; use --error=TEXT if it starts with "-"):
      why the send did not go through, e.g. the tool error for a text over
      5000 characters. Stored on the outbound row as raw {"send_error": ...}
      (one line, access token and "Bearer ..." redacted, at most 300
      characters + "..."). A failed row is not a reply and is never counted
      as sent. This script only records: it never sends or retries anything.

DB: env DESK_DB (required: this bot's own DB path; no default). Run init_db.py
first (several --to and --broadcast need the send_recipients table it adds).
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

NO_DB = ("DESK_DB is not set: give this bot's own database path, e.g. "
         "DESK_DB=/workspace/line-inbox/<agent id>/messages.db "
         "(see 保存先 in memory.md); not using a shared default")
OUT_STATUSES = ("sent", "send_unknown", "failed", "app_reply",
                "auto_reply", "auto_reply_unknown", "auto_reply_failed")
# Statuses of a reply to one friend: only with a single --to.
SINGLE_TO_STATUSES = ("app_reply", "auto_reply", "auto_reply_unknown", "auto_reply_failed")
# Statuses that may carry --error (the send did not go through, or its result is unknown).
ERROR_STATUSES = ("failed", "send_unknown", "auto_reply_failed", "auto_reply_unknown")
ERROR_MAX = 300
SEND_SCOPES = {"push": "one", "segment": "list", "broadcast": "all"}
INSERT_IN = ("INSERT OR IGNORE INTO messages(direction, at, source_type, user_id, group_id, room_id,"
             " message_type, text, line_message_id, webhook_event_id, status, raw)"
             " VALUES('in', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'received', ?)")
# Same with the webhook body's destination; status 'received' or 'other_account'.
INSERT_IN_DEST = ("INSERT OR IGNORE INTO messages(direction, at, source_type, user_id, group_id, room_id,"
                  " message_type, text, line_message_id, webhook_event_id, status, raw, destination)"
                  " VALUES('in', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
# This bot's LINE account (the destination of its webhooks), learned from the first webhook.
ACCOUNT_KEY = "account_destination"
GET_SETTING = "SELECT value FROM desk_settings WHERE key = ?"
SET_SETTING = "INSERT INTO desk_settings(key, value, updated_at) VALUES(?, ?, ?)"
DEST_RE = re.compile(r"U[0-9a-f]{32}")  # used with fullmatch: ^U[0-9a-f]{32}$
# New chat -> 未対応, 完了 -> 未対応, 未対応/対応中 kept as is.
REOPEN = ("INSERT INTO conversations(chat_key, source_type, state, updated_at) VALUES(?, ?, '未対応', ?)"
          " ON CONFLICT(chat_key) DO UPDATE SET"
          " state = CASE WHEN conversations.state = '完了' THEN '未対応' ELSE conversations.state END,"
          " source_type = COALESCE(excluded.source_type, conversations.source_type),"
          " updated_at = excluded.updated_at")
# Friends known now: seen on an inbound 1:1 row, latest follow/unfollow event (if any) is not unfollow.
LINK_KNOWN = ("INSERT OR IGNORE INTO send_recipients(message_id, user_id, known_at_send)"
              " SELECT ?, k.user_id, 1 FROM (SELECT DISTINCT user_id FROM messages"
              "  WHERE direction = 'in' AND source_type = 'user' AND IFNULL(user_id, '') <> '') k"
              " WHERE IFNULL((SELECT f.message_type FROM messages f"
              "  WHERE f.direction = 'in' AND f.source_type = 'user' AND f.user_id = k.user_id"
              "  AND f.message_type IN ('follow', 'unfollow')"
              "  ORDER BY f.at DESC, f.id DESC LIMIT 1), '') <> 'unfollow'")


class Fail(Exception):
    pass


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ms_to_iso(ms):
    """LINE event timestamp (ms epoch) -> local ISO 8601 with offset; now if missing or bad."""
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return now_iso()
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return now_iso()


def obj(v):
    return v if isinstance(v, dict) else {}


def s(v):
    """Value safe to store: strings as is, other JSON values as JSON text."""
    return v if v is None or isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def show(v):
    """One printable token, so input data can never break the output lines."""
    return "".join(c if c.isprintable() and not c.isspace() else "?" for c in v) if v else "-"


def chat_key(source_type, user_id, group_id, room_id):
    if source_type == "group":
        return group_id
    if source_type == "room":
        return room_id
    return user_id


def read_stdin():
    buf = getattr(sys.stdin, "buffer", None)
    try:
        return buf.read().decode("utf-8-sig") if buf else sys.stdin.read()
    except UnicodeDecodeError:
        raise Fail("input is not valid UTF-8")


def open_db():
    path = os.environ.get("DESK_DB", "")
    if not path.strip():
        raise Fail(NO_DB)
    if not os.path.isfile(path):
        raise Fail(f"database not found: {path} (run init_db.py first)")
    con = sqlite3.connect(path, timeout=10)
    con.execute("PRAGMA busy_timeout=10000")
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [t for t in ("messages", "conversations") if t not in have]
    if missing:
        con.close()
        raise Fail(f"table missing in {path}: {', '.join(missing)} (run init_db.py first)")
    return con


def send_schema(con):
    """(messages has send_kind/recipient_scope, table send_recipients exists); init_db.py adds both."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
    table = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='send_recipients'").fetchone()
    return {"send_kind", "recipient_scope"} <= cols, table is not None


def dest_schema(con):
    """True when messages has destination and table desk_settings exists; init_db.py adds both."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
    table = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desk_settings'").fetchone()
    return "destination" in cols and table is not None


def valid_dest(v):
    """A destination as LINE sends it: "U" + 32 lowercase hex characters."""
    return isinstance(v, str) and DEST_RE.fullmatch(v) is not None


def dest_tail(d):
    """Last 4 characters of a destination; the full value is never printed."""
    return d[-4:]


def is_event(ev):
    return isinstance(ev, dict) and isinstance(ev.get("type"), str)


def account_of(con, dest, now):
    """learned / same / other for a valid destination; stores it as this bot's account if none yet.
    Call inside BEGIN IMMEDIATE so two webhooks at the same time cannot learn two different values."""
    row = con.execute(GET_SETTING, (ACCOUNT_KEY,)).fetchone()
    if row:
        return "same" if row[0] == dest else "other"
    con.execute(SET_SETTING, (ACCOUNT_KEY, dest, now))
    return "learned"


def log_in(con):
    data = read_stdin()
    if not data.strip():
        raise Fail("no input on stdin (expected LINE webhook JSON)")
    try:
        body = json.loads(data)
    except ValueError as e:
        raise Fail(f"invalid JSON on stdin: {e}")
    is_body = isinstance(body, dict) and "events" in body
    events = body.get("events") if is_body else body
    if isinstance(events, dict):
        events = [events]
    if not isinstance(events, list):
        raise Fail("input is not a LINE webhook body or event object")
    if not events:
        print("no events")
        return 0
    has_dest = dest_schema(con)  # False on a DB not upgraded by init_db.py: old behaviour, account=unknown
    dest = body.get("destination") if has_dest and is_body else None  # a single event object has none
    dest = dest if valid_dest(dest) else None
    lines, bad, now, account = [], 0, now_iso(), "unknown"
    with con:  # one transaction; lines are printed only after commit
        if has_dest:
            con.execute("BEGIN IMMEDIATE")  # write lock before reading the account (see account_of)
            if dest and any(is_event(ev) for ev in events):  # a body without event objects never learns
                account = account_of(con, dest, now)
        status = "other_account" if account == "other" else "received"
        tail = f" account={account}" + (f" dest={dest_tail(dest)}" if dest else "")
        for ev in events:
            if not is_event(ev):
                lines.append("skipped (not a LINE event object)")
                bad += 1
                continue
            src = obj(ev.get("source"))
            msg = obj(ev.get("message")) if ev["type"] == "message" else {}
            stype, uid, gid, rid = (s(src.get(k)) for k in ("type", "userId", "groupId", "roomId"))
            key = chat_key(stype, uid, gid, rid)
            mtype = s(msg.get("type")) or ev["type"]
            text = s(msg.get("text")) if mtype == "text" else None  # stored as data only
            vals = (ms_to_iso(ev.get("timestamp")), stype, uid, gid, rid, mtype, text,
                    s(msg.get("id")), s(ev.get("webhookEventId")))
            raw = json.dumps(ev, ensure_ascii=False)
            if has_dest:
                cur = con.execute(INSERT_IN_DEST, vals + (status, raw, dest))
            else:
                cur = con.execute(INSERT_IN, vals + (raw,))
            inserted = cur.rowcount == 1
            # Another account's message is recorded only: a reply from this account would be wrong.
            if inserted and ev["type"] == "message" and key and account != "other":
                con.execute(REOPEN, (key, stype, now))
            redelivery = obj(ev.get("deliveryContext")).get("isRedelivery") is True
            head = f"inserted {cur.lastrowid}" if inserted else "duplicate"
            lines.append(f"{head} chat_key={show(key)} source_type={show(stype)}"
                         f" message_type={show(mtype)} isRedelivery={'true' if redelivery else 'false'}"
                         f"{tail} user_id={show(uid)}")
    print("\n".join(lines))
    if bad:
        print(f"error: {bad} item(s) were not LINE event objects and were skipped", file=sys.stderr)
        return 1
    return 0


def clean_error(v):
    """--error text -> one line, access token and "Bearer ..." redacted, cut to ERROR_MAX characters."""
    t = " ".join(v.split())
    token = " ".join(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").split())
    if token:
        t = t.replace(token, "***")
    t = re.sub(r"Bearer \S+", "Bearer ***", t, flags=re.IGNORECASE)
    if not t:
        raise Fail("--error is empty")
    return t if len(t) <= ERROR_MAX else t[:ERROR_MAX] + "..."


def log_out(con, a):
    tos = list(dict.fromkeys(t.strip() for t in a.to or []))  # de-dup, keep order
    if a.broadcast and tos:
        raise Fail("use either --to or --broadcast, not both")
    if not a.broadcast and not any(tos):
        raise Fail("--out needs --to <userId> (or --broadcast)")
    if "" in tos:
        raise Fail("empty --to value (each --to needs a userId)")
    if a.link_known and not a.broadcast:
        raise Fail("--link-known is only for --broadcast")
    kind = "broadcast" if a.broadcast else "segment" if len(tos) > 1 else "push"
    status = a.status or "sent"
    if status in SINGLE_TO_STATUSES and kind != "push":
        raise Fail(f"--status {status} needs a single --to (not several --to or --broadcast)")
    if a.error is not None and status not in ERROR_STATUSES:
        raise Fail(f"--error is only for --status {', '.join(ERROR_STATUSES)} (status is {status});"
                   " a send with an error is never logged as sent")
    send_error = clean_error(a.error) if a.error is not None else None
    has_cols, has_table = send_schema(con)
    if kind != "push" and not (has_cols and has_table):
        raise Fail(f"this database has no send_recipients table yet, needed for {kind} logging"
                   " (run init_db.py first)")
    if a.text is not None and a.text != "-":
        raise Fail("--text only accepts '-' (stdin); put the text in a file and use --text-file")
    if a.text is not None and a.text_file:
        raise Fail("use either --text-file or --text -, not both")
    text = None
    if a.text_file:
        try:
            with open(a.text_file, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            raise Fail(f"cannot read text file {a.text_file}: {e.strerror or type(e).__name__}")
        except UnicodeDecodeError:
            raise Fail(f"text file is not valid UTF-8: {a.text_file}")
    elif a.text == "-":
        text = read_stdin()
    text = (text or "").rstrip("\r\n") or None
    if text is None and status != "app_reply":
        raise Fail("message text is empty or missing (use --text-file <path> or --text -)")
    uid = tos[0] if kind == "push" else None  # segment / broadcast rows belong to no chat
    cols = ["direction", "at", "source_type", "user_id", "message_type", "text", "line_message_id", "status",
            "raw"]
    vals = ["out", now_iso(), "user" if uid else None, uid, "text" if text else None, text,
            a.line_message_id, status,
            json.dumps({"send_error": send_error}, ensure_ascii=False) if send_error else None]
    if has_cols:  # older DBs without these columns still log a single --to (NULL = push / one)
        cols += ["send_kind", "recipient_scope"]
        vals += [kind, SEND_SCOPES[kind]]
    linked = 0
    with con:
        cur = con.execute(f"INSERT INTO messages({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)})",
                          vals)
        mid = cur.lastrowid
        if has_table and kind != "broadcast":
            linked = con.executemany("INSERT OR IGNORE INTO send_recipients(message_id, user_id) VALUES(?, ?)",
                                     [(mid, u) for u in tos]).rowcount
        elif has_table and a.link_known:
            linked = con.execute(LINK_KNOWN, (mid,)).rowcount
    if kind == "push":
        print(f"inserted {mid}")
    else:
        print(f"inserted {mid} send_kind={kind} scope={SEND_SCOPES[kind]} recipients_linked={linked}")
    return 0


def main(argv):
    p = argparse.ArgumentParser(description="Log LINE webhook events from stdin, or a sent message with --out.")
    p.add_argument("--out", action="store_true", help="log an outbound message instead of webhook JSON")
    p.add_argument("--to", action="append",
                   help="userId the message was sent to; repeat for a list of friends (2+ = segment)")
    p.add_argument("--broadcast", action="store_true", help="log a broadcast to all friends (not with --to)")
    p.add_argument("--link-known", action="store_true",
                   help="with --broadcast: also link the friends known in this DB now (known_at_send=1)")
    p.add_argument("--text-file", help="file with the message text")
    p.add_argument("--text", help="'-' to read the message text from stdin")
    p.add_argument("--line-message-id", help="message id returned by LINE, if any")
    p.add_argument("--status", choices=OUT_STATUSES, help="default: sent")
    p.add_argument("--error", help="why the send failed (only with --status failed, send_unknown,"
                   " auto_reply_failed or auto_reply_unknown); stored as raw {\"send_error\": ...}")
    a = p.parse_args(argv)
    try:
        if not a.out and (a.broadcast or a.link_known or any(
                v is not None for v in (a.to, a.text_file, a.text, a.line_message_id, a.status, a.error))):
            raise Fail("--to, --broadcast, --link-known, --text-file, --text, --line-message-id,"
                       " --status and --error are only for --out")
        con = open_db()
        try:
            return log_out(con, a) if a.out else log_in(con)
        finally:
            con.close()
    except Fail as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except sqlite3.Error as e:
        print(f"error: database problem: {e} (if a table or column is missing, run init_db.py)", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
