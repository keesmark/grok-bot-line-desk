#!/usr/bin/env python3
"""Create or upgrade the Desk for LINE database. Safe to run again at any time.

Usage: init_db.py

DB: env DESK_DB (required: this bot's own DB path; no default).
Creates the folder (mode 0700), the DB file (mode 0600), the tables
messages / conversations / customers / canned_replies / customer_tags /
send_recipients / keyword_rules / keyword_matches / desk_settings and their
indexes and the media folder <DB folder>/media (mode 0700), adds missing
tables and columns to an existing DB (existing rows are kept as they are)
and turns on WAL mode so webhook runs at the same time do not collide.
customer_tags is only a container for now; no script writes it yet.
messages.send_kind ('push' | 'segment' | 'broadcast') and
messages.recipient_scope ('one' | 'list' | 'all') describe sent messages;
NULL on older rows means push / one.
send_recipients links one sent message to the friends it was for.
known_at_send=1 there means "a friend known in this DB at send time",
not a confirmed delivery.
keyword_rules holds the keyword auto-reply rules (keyword_reply.py; shop data).
keyword_matches holds one row per inbound message a rule matched: a snapshot
of the reply text, the decision (suggested / auto_pending / auto_sent /
auto_unknown / auto_failed), the reason and the logged outbound row.
messages.destination is the LINE account an inbound webhook was for (NULL on
outbound and older rows); status 'other_account' = not this bot's account.
customers.nickname is the owner's name for a friend (profile_cache.py nickname).
desk_settings holds shop settings; account_destination (this bot's account)
is learned from the first webhook; account.py shows, changes or forgets it.
"""
import argparse
import os
import sqlite3
import sys

NO_DB = ("DESK_DB is not set: give this bot's own database path, e.g. "
         "DESK_DB=/workspace/line-inbox/<agent id>/messages.db "
         "(see 保存先 in memory.md); not using a shared default")
TABLES = {
    "messages": """CREATE TABLE IF NOT EXISTS messages(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 direction TEXT NOT NULL CHECK(direction IN ('in','out')),
 at TEXT NOT NULL,
 source_type TEXT, user_id TEXT, group_id TEXT, room_id TEXT,
 message_type TEXT, text TEXT, line_message_id TEXT,
 webhook_event_id TEXT UNIQUE,
 status TEXT, raw TEXT, read_at TEXT,
 media_path TEXT, preview_path TEXT, media_saved_at TEXT,
 send_kind TEXT, recipient_scope TEXT,
 destination TEXT)""",
    "conversations": """CREATE TABLE IF NOT EXISTS conversations(
 chat_key TEXT PRIMARY KEY, source_type TEXT,
 state TEXT NOT NULL DEFAULT '未対応' CHECK(state IN ('未対応','対応中','完了')),
 memo TEXT, updated_at TEXT)""",
    "customers": """CREATE TABLE IF NOT EXISTS customers(
 user_id TEXT PRIMARY KEY, display_name TEXT, note TEXT, evidence TEXT, updated_at TEXT,
 profile_at TEXT, nickname TEXT)""",
    "canned_replies": """CREATE TABLE IF NOT EXISTS canned_replies(
 name TEXT PRIMARY KEY, body TEXT NOT NULL, updated_at TEXT)""",
    # Per-friend tags, one row per (user_id, tag); kept only here, not on LINE.
    "customer_tags": """CREATE TABLE IF NOT EXISTS customer_tags(
 user_id TEXT NOT NULL, tag TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(user_id, tag))""",
    # Who a sent message was for (push: 1 row, segment: 1 per friend, broadcast: known friends).
    "send_recipients": """CREATE TABLE IF NOT EXISTS send_recipients(
 message_id INTEGER NOT NULL, user_id TEXT NOT NULL,
 known_at_send INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(message_id, user_id))""",
    # Keyword auto-reply rules set by the owner (keyword_reply.py); shop data, purge_old.py keeps them.
    "keyword_rules": """CREATE TABLE IF NOT EXISTS keyword_rules(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 keyword TEXT NOT NULL, keyword_norm TEXT NOT NULL,
 match_type TEXT NOT NULL DEFAULT 'exact' CHECK(match_type IN ('exact','contains')),
 reply_text TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(keyword_norm, match_type))""",
    # One row per inbound message a rule matched; reply_text is the rule's reply at match time.
    "keyword_matches": """CREATE TABLE IF NOT EXISTS keyword_matches(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 message_id INTEGER NOT NULL UNIQUE,
 rule_id INTEGER, user_id TEXT, keyword TEXT, match_type TEXT,
 reply_text TEXT NOT NULL,
 decision TEXT NOT NULL CHECK(decision IN ('suggested','auto_pending','auto_sent','auto_unknown','auto_failed')),
 reason TEXT,
 matched_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 out_message_id INTEGER)""",
    # Shop settings stored in the DB (key/value), e.g. account_destination; purge_old.py never touches it.
    "desk_settings": """CREATE TABLE IF NOT EXISTS desk_settings(
 key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)""",
}
INDEXES = [
    "CREATE INDEX IF NOT EXISTS messages_user_at ON messages(user_id, at)",
    "CREATE INDEX IF NOT EXISTS messages_group_at ON messages(group_id, at)",
    "CREATE INDEX IF NOT EXISTS messages_room_at ON messages(room_id, at)",
    "CREATE INDEX IF NOT EXISTS messages_at ON messages(at)",
    "CREATE INDEX IF NOT EXISTS customer_tags_tag ON customer_tags(tag)",
    "CREATE INDEX IF NOT EXISTS send_recipients_user ON send_recipients(user_id)",
    "CREATE INDEX IF NOT EXISTS keyword_matches_user_rule ON keyword_matches(user_id, rule_id, matched_at)",
    "CREATE INDEX IF NOT EXISTS messages_destination ON messages(destination)",
]
# ALTER TABLE cannot add a UNIQUE column, so redelivery dedupe then needs an index.
EVENT_ID_INDEX = "CREATE UNIQUE INDEX IF NOT EXISTS messages_event_id ON messages(webhook_event_id)"
MEDIA_FOLDER = "media"


class Fail(Exception):
    pass


def add_missing_columns(con, table):
    """Add the columns of the current schema that an older table lacks."""
    mem = sqlite3.connect(":memory:")
    mem.execute(TABLES[table])
    want = mem.execute(f"PRAGMA table_info({table})").fetchall()
    mem.close()
    have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    added = []
    for _, name, typ, notnull, default, pk in want:
        if name in have:
            continue
        if pk or (notnull and default is None):
            raise Fail(f"table {table} has an unknown layout (no column {name}); not changing it")
        extra = (" NOT NULL" if notnull else "") + (f" DEFAULT {default}" if default is not None else "")
        con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}{extra}")
        added.append(name)
    return added


def make_media_folder(folder):
    """Create <DB folder>/media (mode 0700) for saved images, videos and files."""
    media = os.path.join(folder, MEDIA_FOLDER)
    if os.path.islink(media) or (os.path.exists(media) and not os.path.isdir(media)):
        raise Fail(f"{media} exists but is not a folder; move it away and run again")
    if os.path.isdir(media):
        os.chmod(media, 0o700)
        print(f"media folder exists: {media} (mode 0700)")
    else:
        os.mkdir(media, 0o700)
        os.chmod(media, 0o700)  # mkdir mode is reduced by umask
        print(f"created media folder {media} (mode 0700)")


def init(path):
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        os.makedirs(folder, mode=0o700)
        os.chmod(folder, 0o700)  # makedirs mode is reduced by umask
        print(f"created folder {folder} (mode 0700)")
    if os.path.isdir(path):
        raise Fail(f"{path} is a folder, not a database file")
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        print(f"created database {path}")
    except FileExistsError:
        print(f"database exists: {path}")
    for p in (path, path + "-wal", path + "-shm"):
        if os.path.exists(p):
            os.chmod(p, 0o600)
    print("file mode 0600")

    con = sqlite3.connect(path, timeout=10, isolation_level=None)
    try:
        con.execute("PRAGMA busy_timeout=10000")
        print("journal_mode " + con.execute("PRAGMA journal_mode=WAL").fetchone()[0])
        con.execute("BEGIN IMMEDIATE")
        have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, sql in TABLES.items():
            if table not in have:
                con.execute(sql)
                print(f"table {table}: created")
                continue
            added = add_missing_columns(con, table)
            print(f"table {table}: " + ("added column " + ", ".join(added) if added else "ok"))
            if "webhook_event_id" in added:
                con.execute(EVENT_ID_INDEX)
        for sql in INDEXES:
            con.execute(sql)
        print(f"indexes: ok ({len(INDEXES)})")
        con.execute("COMMIT")
    finally:
        con.close()
    make_media_folder(folder)
    print(f"ok, database ready: {path}")
    return 0


def main(argv):
    argparse.ArgumentParser(
        description="Create or upgrade the Desk for LINE database (safe to re-run).").parse_args(argv)
    db = os.environ.get("DESK_DB", "")
    if not db.strip():
        print(f"error: {NO_DB}", file=sys.stderr)
        return 1
    path = os.path.abspath(db)
    try:
        return init(path)
    except Fail as e:
        print(f"error: {e}", file=sys.stderr)
    except sqlite3.Error as e:
        print(f"error: database problem: {e} ({path})", file=sys.stderr)
    except OSError as e:
        print(f"error: {e.strerror or e}: {e.filename or path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
