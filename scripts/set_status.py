#!/usr/bin/env python3
"""Set the handling state (and optionally the memo) of one LINE chat.

Usage: set_status.py <chat_key> <未対応|対応中|完了> [--memo TEXT]
  English aliases: open = 未対応, doing = 対応中, done = 完了
  chat_key: user id for 1:1, group id for groups, room id for rooms
            (as shown by list_unreplied.py).
  --memo TEXT  replace the chat memo ("" clears it). Without it the memo is kept.

Prints "<chat_key>: <old> -> <new>". A new inbound message in a 完了 chat
puts it back to 未対応 automatically (log_event.py does that).
DB: env DESK_DB (required: this bot's own DB path; no default).
"""
import argparse
import os
import sqlite3
import sys
import urllib.parse
from datetime import datetime

NO_DB = ("DESK_DB is not set: give this bot's own database path, e.g. "
         "DESK_DB=/workspace/line-inbox/<agent id>/messages.db "
         "(see 保存先 in memory.md); not using a shared default")
STATES = {"未対応": "未対応", "対応中": "対応中", "完了": "完了",
          "open": "未対応", "doing": "対応中", "done": "完了"}
# Chat key: group_id for groups, room_id for rooms, user_id otherwise.
CHAT_KEY_SQL = ("CASE source_type WHEN 'group' THEN group_id "
                "WHEN 'room' THEN room_id ELSE user_id END")
UPSERT = ("INSERT INTO conversations(chat_key, source_type, state, memo, updated_at)"
          " VALUES(?, ?, ?, ?, ?) ON CONFLICT(chat_key) DO UPDATE SET"
          " state = excluded.state, memo = excluded.memo,"
          " updated_at = excluded.updated_at,"
          " source_type = COALESCE(conversations.source_type, excluded.source_type)")


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def state_arg(s):
    state = STATES.get(s.strip().lower())
    if not state:
        raise argparse.ArgumentTypeError(
            "use 未対応, 対応中 or 完了 (or open, doing, done)")
    return state


def main(argv):
    p = argparse.ArgumentParser(description="Set the handling state of a LINE chat.")
    p.add_argument("chat_key", help="user/group/room id of the chat")
    p.add_argument("state", type=state_arg, help="未対応|対応中|完了 or open|doing|done")
    p.add_argument("--memo", help='replace the memo ("" clears it)')
    args = p.parse_args(argv)
    key = args.chat_key.strip()
    if not key:
        print("error: chat_key is empty", file=sys.stderr)
        return 1

    path = os.environ.get("DESK_DB", "")
    if not path.strip():
        print(f"error: {NO_DB}", file=sys.stderr)
        return 1
    if not os.path.isfile(path):
        print(f"error: database not found: {path} (run init_db.py first)", file=sys.stderr)
        return 1
    con = sqlite3.connect(f"file:{urllib.parse.quote(path)}?mode=rw", uri=True,
                          timeout=10, isolation_level=None)
    try:
        have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in ("messages", "conversations") if t not in have]
        if missing:
            print(f"error: table missing in {path}: {', '.join(missing)} "
                  "(run init_db.py first)", file=sys.stderr)
            return 1
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT state, memo FROM conversations WHERE chat_key = ?",
                          (key,)).fetchone()
        src = con.execute(f"SELECT source_type FROM messages WHERE {CHAT_KEY_SQL} = ?"
                          " ORDER BY at DESC, id DESC LIMIT 1", (key,)).fetchone()
        if row is None and src is None:
            con.execute("ROLLBACK")
            print(f"error: no chat found for {key} "
                  "(copy the chat_key from list_unreplied.py)", file=sys.stderr)
            return 1
        memo = row[1] if row else None
        if args.memo is not None:
            memo = args.memo if args.memo.strip() else None
        con.execute(UPSERT, (key, src[0] if src else None, args.state, memo, now_iso()))
        con.execute("COMMIT")
    except sqlite3.Error as e:
        print(f"error: database problem: {e}", file=sys.stderr)
        return 1
    finally:
        con.close()

    print(f"{key}: {row[0] if row else '(new)'} -> {args.state}")
    if args.memo is not None:
        print("memo: " + ("saved" if memo else "cleared"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
