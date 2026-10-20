#!/usr/bin/env python3
"""Show, set or forget the LINE account this bot answers for.

Usage: account.py show [--json] [--full]
       account.py set <destination> [--relabel]
       account.py set --from-message <messages.id> [--relabel]
       account.py forget [--yes]

1 bot = 1 LINE account. Every LINE webhook body has a top-level
"destination": the bot user id of the LINE account the webhook was sent
for (U + 32 lowercase hex chars). The webhook relay adds nothing else, so
it is the only way to tell accounts apart. The bot cannot read its own bot
user id with get_profile, so log_event.py learns the account from the
first webhook with a valid destination (desk_settings key
account_destination). Inbound rows for any other destination get status
other_account: they do not open a conversation, list_unreplied.py skips
them and keyword_reply.py never answers them.

show    prints the account ("account: <tail> (learned <updated_at>)" or
        "account: not learned yet ..."), the number of inbound rows for
        it, and one line per other destination seen on other_account rows,
        newest first ("other: none" if there is none). --json prints one
        object {"learned", "dest_tail", "updated_at", "rows", "other":
        [{"dest_tail", "rows", "last_at"}]}. --full also prints the full
        destination ("destination" in JSON).
set     saves the account: a destination given on the command line, or
        with --from-message the destination of that inbound messages row.
        Use it when the wrong account was learned or the account changed.
        Existing rows are not changed; it prints how many now match or
        differ. --relabel updates them in the same transaction:
        other_account rows for the new account become received, received
        rows for any other destination become other_account. Conversations
        are not touched; check the unreplied list after relabelling.
forget  deletes the learned account, so the next webhook with a
        destination sets it again. Dry run unless --yes. Rows are not
        changed.

Only the last 4 characters of a destination are printed unless --full is
given (it is not a secret; the owner only needs the tail).
This script never sends anything and does not call LINE or any API.

DB: env DESK_DB (required: this bot's own DB path; no default).
"""
import argparse
import contextlib
import json
import os
import re
import sqlite3
import sys
import unicodedata
import urllib.parse
from datetime import datetime

NO_DB = ("DESK_DB is not set: give this bot's own database path, e.g. "
         "DESK_DB=/workspace/line-inbox/<agent id>/messages.db "
         "(see 保存先 in memory.md); not using a shared default")
ACCOUNT_KEY = "account_destination"
TAIL_CHARS = 4
MAX_ROW_ID = 2 ** 63 - 1
# What LINE sends as destination: U + 32 lowercase hex chars.
DEST_RE = re.compile(r"U[0-9a-f]{32}")
# messages time column shown as last_at; the first one present is used.
TIME_COLUMNS = ("at", "created_at", "received_at", "logged_at")
# Inbound rows that relabelling to the account (?) would change.
NOW_MATCH = ("direction='in' AND destination IS NOT NULL AND destination=?"
             " AND status='other_account'")
NOW_DIFFER = ("direction='in' AND destination IS NOT NULL AND destination<>?"
              " AND status='received'")


class Fail(Exception):
    pass


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def one_line(text):
    """Control characters and line breaks become spaces."""
    return "".join(" " if unicodedata.category(c) in ("Cc", "Zl", "Zp") else c
                   for c in str(text))


def is_dest(value):
    return isinstance(value, str) and DEST_RE.fullmatch(value) is not None


def check_dest(value):
    if not is_dest(value):
        raise Fail("not a LINE destination: it must be U + 32 lowercase hex"
                   " characters")
    return value


def dest_tail(dest):
    """The last TAIL_CHARS characters of a destination (None for none)."""
    return one_line(dest)[-TAIL_CHARS:] if dest is not None else None


def message_id(value):
    try:
        n = int(value)
    except ValueError:
        n = 0
    if not 1 <= n <= MAX_ROW_ID:
        raise argparse.ArgumentTypeError("must be a messages.id (a whole number,"
                                         " 1 or more)")
    return n


def open_db(path):
    if not os.path.isfile(path):
        raise Fail("database not found: %s (run init_db.py first)" % path)
    uri = "file:%s?mode=rw" % urllib.parse.quote(path)
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.isolation_level = None  # write transactions are opened explicitly
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def need(conn, db, table, columns):
    have = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
    if not have:
        raise Fail("table %s not found in %s (run init_db.py first)" % (table, db))
    for col in columns:
        if col not in have:
            raise Fail("column %s.%s is missing (run init_db.py first)"
                       % (table, col))


@contextlib.contextmanager
def immediate(conn):
    """One write transaction; BEGIN IMMEDIATE takes the write lock first."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    conn.commit()


def account(conn):
    """(account destination, updated_at), or (None, None) if not learned."""
    row = conn.execute("SELECT value, updated_at FROM desk_settings WHERE key=?",
                       (ACCOUNT_KEY,)).fetchone()
    return tuple(row) if row else (None, None)


def count(conn, where, dest):
    return conn.execute("SELECT COUNT(*) FROM messages WHERE " + where,
                        (dest,)).fetchone()[0]


def others(conn, dest):
    """Other destinations on other_account rows, newest first."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    col = next((c for c in TIME_COLUMNS if c in have), None)
    # With a single MAX() SQLite takes the bare column from that row.
    rows = conn.execute(
        "SELECT destination, COUNT(*), MAX(id) AS last_id, %s FROM messages"
        " WHERE direction='in' AND status='other_account'"
        " AND destination IS NOT NULL AND destination<>?"
        " GROUP BY destination ORDER BY last_id DESC"
        % ('"%s"' % col if col else "NULL"), (dest or "",)).fetchall()
    return [{"dest_tail": dest_tail(d), "rows": n,
             "last_at": None if at is None else str(at)}
            for d, n, _, at in rows]


def show(conn, as_json, full):
    dest, updated_at = account(conn)
    rows = 0
    if dest is not None:
        rows = conn.execute("SELECT COUNT(*) FROM messages"
                            " WHERE direction='in' AND destination=?",
                            (dest,)).fetchone()[0]
    other = others(conn, dest)
    if as_json:
        out = {"learned": dest is not None, "dest_tail": dest_tail(dest),
               "updated_at": updated_at, "rows": rows, "other": other}
        if full:
            out["destination"] = dest
        print(json.dumps(out, ensure_ascii=False))
        return
    if dest is None:
        print("account: not learned yet (the next webhook with a destination"
              " sets it)")
    else:
        print("account: %s (learned %s)" % (dest_tail(dest), updated_at))
        if full:
            print("destination: %s" % one_line(dest))
    print("rows: %d inbound rows for this account" % rows)
    for o in other:
        print("other: %s rows=%d last_at=%s"
              % (o["dest_tail"], o["rows"], o["last_at"] or "-"))
    if not other:
        print("other: none")


def message_destination(conn, msg_id):
    row = conn.execute("SELECT direction, destination FROM messages WHERE id=?",
                       (msg_id,)).fetchone()
    if row is None:
        raise Fail("message %d not found" % msg_id)
    direction, dest = row
    if direction != "in":
        raise Fail("message %d is not an inbound row" % msg_id)
    if not dest:
        raise Fail("message %d has no destination (old row, or the webhook"
                   " had none)" % msg_id)
    return check_dest(dest)


def set_account(conn, dest, msg_id, relabel):
    with immediate(conn):
        if msg_id is not None:
            dest = message_destination(conn, msg_id)
        old = account(conn)[0]
        if dest != old:
            conn.execute("INSERT OR REPLACE INTO desk_settings(key, value,"
                         " updated_at) VALUES(?, ?, ?)",
                         (ACCOUNT_KEY, dest, now_iso()))
        if relabel:
            match = conn.execute("UPDATE messages SET status='received' WHERE "
                                 + NOW_MATCH, (dest,)).rowcount
            differ = conn.execute("UPDATE messages SET status='other_account'"
                                  " WHERE " + NOW_DIFFER, (dest,)).rowcount
        else:
            match = count(conn, NOW_MATCH, dest)
            differ = count(conn, NOW_DIFFER, dest)
    saved = "saved account dest=%s" % dest_tail(dest)
    if old is not None and old != dest:
        saved += " (was %s)" % dest_tail(old)
    print(saved)
    if relabel:
        print("relabel: %d -> received, %d -> other_account" % (match, differ))
    elif match or differ:
        print("rows: %d other_account rows now match, %d received rows now differ"
              " (use --relabel to update them)" % (match, differ))


def forget(conn, yes):
    if yes:
        with immediate(conn):
            dest = account(conn)[0]
            conn.execute("DELETE FROM desk_settings WHERE key=?", (ACCOUNT_KEY,))
    else:
        dest = account(conn)[0]
    if dest is None:
        print("account: not learned yet")
    elif yes:
        print("forgot account dest=%s; the next webhook with a destination sets"
              " it again" % dest_tail(dest))
    else:
        print("would forget account dest=%s (run with --yes)" % dest_tail(dest))


def run(args, db):
    if args.cmd == "set":
        if (args.destination is None) == (args.from_message is None):
            raise Fail("give either a destination or --from-message ID")
        if args.destination is not None:
            check_dest(args.destination)
    conn = open_db(db)
    try:
        need(conn, db, "desk_settings", ("key", "value", "updated_at"))
        need(conn, db, "messages", ("id", "direction", "status", "destination"))
        if args.cmd == "show":
            show(conn, args.json, args.full)
        elif args.cmd == "set":
            set_account(conn, args.destination, args.from_message, args.relabel)
        else:
            forget(conn, args.yes)
    finally:
        conn.close()
    return 0


def main(argv):
    parser = argparse.ArgumentParser(
        description="Show, set or forget the LINE account this bot answers for"
        " (no API calls).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("show", help="print the learned account and the other"
                       " accounts seen")
    p.add_argument("--json", action="store_true",
                   help="print learned, dest_tail, updated_at, rows, other as JSON")
    p.add_argument("--full", action="store_true",
                   help="also print the full destination")

    p = sub.add_parser("set", help="save the account (the wrong account was"
                       " learned, or the account changed)")
    p.add_argument("destination", nargs="?",
                   help="U + 32 lowercase hex characters")
    p.add_argument("--from-message", type=message_id, metavar="ID",
                   help="use the destination of this inbound messages row")
    p.add_argument("--relabel", action="store_true",
                   help="also update the status of existing inbound rows")

    p = sub.add_parser("forget", help="delete the learned account"
                       " (dry run unless --yes)")
    p.add_argument("--yes", action="store_true", help="really delete it")

    args = parser.parse_args(argv)
    db = os.environ.get("DESK_DB", "")
    if not db.strip():
        print("error: %s" % NO_DB, file=sys.stderr)
        return 1
    try:
        return run(args, db)
    except Fail as e:
        print("error: %s" % e, file=sys.stderr)
    except sqlite3.Error as e:
        print("error: database problem: %s" % e, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
