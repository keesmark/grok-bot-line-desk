#!/usr/bin/env python3
"""Delete old data or one customer's data from the Desk for LINE log.

Usage:
  purge_old.py --before YYYY-MM-DD [--yes]
      messages with `at` before that local date (and the send_recipients and
      keyword_matches rows of those messages), customer notes last updated
      before it, customer tags added before it, and conversation states of
      chats with no newer messages.
  purge_old.py --user <userId> [--yes]
      everything about one customer: their messages (1:1 and in groups, in
      and out), their conversation row, their customer note, their tags,
      their keyword_matches rows and their send_recipients rows (as a
      recipient of a segment/broadcast send, and those of their own
      messages). Segment/broadcast message rows themselves are kept: they are
      not about one person.
keyword_rules (the shop's keyword auto-reply rules) are never deleted.
An older DB without the customer_tags, send_recipients or keyword_matches
table is fine; those are then skipped.

Media files recorded for the deleted messages (messages.media_path and
messages.preview_path; relative paths count from the DB folder) are deleted
too, but only when their real path is inside <DB folder>/media/. Nothing else
is ever deleted; files outside it are listed as skipped.

Without --yes this is a dry run: it only prints how many rows would be deleted
per table and how many of those media files exist. With --yes it deletes the
rows, then the media files, then runs VACUUM so the data is gone from the
file. Retention is unlimited by default; use this only when asked.
DB: env DESK_DB (required: this bot's own DB path; no default).
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime

NO_DB = ("DESK_DB is not set: give this bot's own database path, e.g. "
         "DESK_DB=/workspace/line-inbox/<agent id>/messages.db "
         "(see 保存先 in memory.md); not using a shared default")
TABLES = ("messages", "conversations", "customers")
MEDIA_COLUMNS = ("media_path", "preview_path")
CHAT_KEY = ("CASE m.source_type WHEN 'group' THEN m.group_id "
            "WHEN 'room' THEN m.room_id ELSE m.user_id END")


def day(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError("use YYYY-MM-DD, e.g. 2026-01-01")


def user_id(s):
    if not s.strip():
        raise argparse.ArgumentTypeError("userId is empty")
    return s.strip()


def one_line(s):
    """Untrusted text, safe to print on one line."""
    return "".join(c if c.isprintable() else "?" for c in s)


def plan(args):
    """Return [(table, where, params)] for the chosen mode, in delete order.
    send_recipients and keyword_matches go first, while the messages they
    point to still exist."""
    if args.before:
        d = args.before
        # Drop a chat's state only when it is old and the chat has no newer messages.
        conv = ("updated_at < ? AND NOT EXISTS (SELECT 1 FROM messages m "
                f"WHERE m.at >= ? AND {CHAT_KEY} = conversations.chat_key)")
        return [("send_recipients", "message_id IN (SELECT id FROM messages WHERE at < ?)", (d,)),
                ("keyword_matches", "message_id IN (SELECT id FROM messages WHERE at < ?)", (d,)),
                ("messages", "at < ?", (d,)),
                ("conversations", conv, (d, d)),
                ("customers", "updated_at < ?", (d,)),
                ("customer_tags", "created_at < ?", (d,))]
    u = args.user
    # Segment/broadcast rows (user_id NULL) are kept; only this user's recipient rows go.
    recipients = "user_id = ? OR message_id IN (SELECT id FROM messages WHERE user_id = ?)"
    return [("send_recipients", recipients, (u, u)),
            ("keyword_matches", "user_id = ?", (u,)),
            ("messages", "user_id = ?", (u,)),
            ("conversations", "chat_key = ?", (u,)),
            ("customers", "user_id = ?", (u,)),
            ("customer_tags", "user_id = ?", (u,))]


def media_paths(con, steps):
    """Media paths recorded on the messages the plan deletes (none if no such columns)."""
    have = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
    cols = [c for c in MEDIA_COLUMNS if c in have]
    if not cols:
        return set()
    _, where, params = next(s for s in steps if s[0] == "messages")
    rows = con.execute(f"SELECT {', '.join(cols)} FROM messages WHERE {where}", params)
    return {v for row in rows for v in row if isinstance(v, str) and v.strip()}


def existing_media(paths, db_dir):
    """Return (inside, outside): real paths of the files that exist, split by
    whether they are inside <db_dir>/media/."""
    root = os.path.realpath(os.path.join(db_dir, "media"))
    inside, outside = set(), set()
    for p in paths:
        try:
            real = os.path.realpath(os.path.join(db_dir, p))
        except ValueError:  # e.g. a NUL byte in the path
            continue
        if not os.path.isfile(real):
            continue
        if real.startswith(root + os.sep):
            inside.add(real)
        else:
            outside.add(real)
    return sorted(inside), sorted(outside)


def remove_files(files):
    """Delete the files; return (deleted, failed)."""
    deleted = failed = 0
    for f in files:
        try:
            os.remove(f)
            deleted += 1
        except FileNotFoundError:
            pass  # already gone
        except OSError as e:
            print(f"could not delete media file: {one_line(f)} ({e.strerror or e}); "
                  "delete it by hand", file=sys.stderr)
            failed += 1
    return deleted, failed


def print_skipped(files, media_dir, what):
    for f in files:
        print(f"media file {what} (outside {media_dir}{os.sep}): {one_line(f)}")


def main(argv):
    p = argparse.ArgumentParser(description="Delete old or per-customer data (dry run unless --yes).")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--before", type=day, metavar="YYYY-MM-DD", help="delete data older than this local date")
    g.add_argument("--user", type=user_id, metavar="USER_ID", help="delete all data about this LINE user")
    p.add_argument("--yes", action="store_true", help="really delete (default: dry run)")
    args = p.parse_args(argv)

    path = os.environ.get("DESK_DB", "")
    if not path.strip():
        print(f"error: {NO_DB}", file=sys.stderr)
        return 1
    if not os.path.isfile(path):
        print(f"database not found: {path} (run init_db.py first)", file=sys.stderr)
        return 1
    db_dir = os.path.dirname(os.path.abspath(path))
    media_dir = os.path.join(db_dir, "media")
    con = sqlite3.connect(path, timeout=10, isolation_level=None)
    try:
        have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = ", ".join(t for t in TABLES if t not in have)
        if missing:
            print(f"table missing: {missing} in {path} (run init_db.py first)", file=sys.stderr)
            return 1
        what = f"before {args.before}" if args.before else f"user {args.user}"
        # customer_tags, send_recipients and keyword_matches may be missing on a DB
        # not yet upgraded; skip them then.
        steps = [s for s in plan(args) if s[0] in have]
        if not args.yes:
            print(f"dry run, nothing deleted (add --yes to delete): {what}")
            for table, where, params in steps:
                n = con.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]
                print(f"{table}: {n}")
            inside, outside = existing_media(media_paths(con, steps), db_dir)
            print(f"media files: {len(inside) + len(outside)}")
            print_skipped(outside, media_dir, "would be skipped")
            return 0
        con.execute("BEGIN IMMEDIATE")
        files = media_paths(con, steps)
        counts = [(table, con.execute(f"DELETE FROM {table} WHERE {where}", params).rowcount)
                  for table, where, params in steps]
        con.execute("COMMIT")
        print(f"deleted: {what}")
        for table, n in counts:
            print(f"{table}: {n}")
        # Files go right after the commit, so a failed VACUUM cannot leave them behind.
        inside, outside = existing_media(files, db_dir)
        deleted, failed = remove_files(inside)
        print(f"media files deleted: {deleted}")
        print_skipped(outside, media_dir, "skipped")
        try:
            con.execute("VACUUM")
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError as e:
            print(f"rows deleted, but VACUUM failed ({e}); old data may stay in the file "
                  "until it succeeds. Run the same command again later.", file=sys.stderr)
            return 1
        print("vacuum: done")
        return 1 if failed else 0
    except sqlite3.Error as e:
        print(f"database error: {e}", file=sys.stderr)
        return 1
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
