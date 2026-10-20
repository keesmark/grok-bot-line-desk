#!/usr/bin/env python3
"""Cache LINE display names that the bot read with the line-oa MCP tool get_profile.

Usage: profile_cache.py get <userId> [--json] [--max-age-days N]
       profile_cache.py label <userId> [--max-age-days N]
       profile_cache.py set <userId> (--name-file PATH | --name -)
       profile_cache.py nickname <userId> (--name-file PATH | --name - | --clear)
       profile_cache.py stale [--max-age-days N] [--json]

get    prints the cached display name on one line, or "unknown".
       --json prints {"user_id", "display_name", "nickname", "profile_at",
       "stale", "short_id", "label"}.
label  prints what the owner sees as the sender, on one line: the nickname,
       else the cached name (also when stale), else 名前不明（ID末尾 <short_id>）.
       A cached name that reads the same as another person's nickname gets
       （ID末尾 <short_id>） added. short_id is the last 4 characters of the
       user id; the full id is never shown.
set    saves the name (from a file, or from stdin with --name -) as
       customers.display_name and sets profile_at to now. nickname, note and
       evidence are never changed. The name is trimmed, line breaks inside become
       spaces and it is cut to 100 characters. Prints "saved".
stale  lists user ids of inbound senders whose name is missing, empty or
       older than N days, latest chat first, one per line ("none" if
       nothing). --json prints a JSON list.
nickname  saves the owner's name for this person (customers.nickname), used
       instead of the display name in label, e.g. when two people share one
       display name. Refreshing the display name keeps it. It is cleaned like
       a display name; one already shown for another person (their nickname,
       or their display name when they have none; compared ignoring width,
       case, spaces and invisible characters) is refused. --clear removes it. Prints "saved" or
       "cleared".

A cached name counts as stale after 7 days by default. That is a proposal
the owner can change (--max-age-days, or MAX_AGE_DAYS below).
This script does not call any API.

DB: env DESK_DB (required: this bot's own DB path; no default).
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import unicodedata
import urllib.parse
from datetime import datetime, timedelta

NO_DB = ("DESK_DB is not set: give this bot's own database path, e.g. "
         "DESK_DB=/workspace/line-inbox/<agent id>/messages.db "
         "(see 保存先 in memory.md); not using a shared default")
MAX_AGE_DAYS = 7  # proposal; the owner can change it
MAX_NAME_CHARS = 100
MAX_INPUT_BYTES = 64 * 1024
SHORT_ID_CHARS = 4
UNKNOWN_LABEL = "名前不明"
UNKNOWN_ID_LABEL = "名前不明（ID末尾 %s）"  # never the full user id
# LINE user ids are U + 32 hex chars; accept any short plain token.
USER_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


class Fail(Exception):
    pass


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


# Invisible marks that can make one name look like another (bidi controls,
# zero-width space, word joiner, BOM); zero-width (non-)joiners stay for emoji.
HIDDEN = frozenset("\u061c\u200b\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
                   "\u2060\u2066\u2067\u2068\u2069\ufeff")


def one_line(text):
    """Control characters and line breaks become spaces; hidden marks are dropped."""
    return "".join(" " if unicodedata.category(c) in ("Cc", "Zl", "Zp") else c
                   for c in str(text) if c not in HIDDEN)


def clip(text):
    """One line, at most 200 chars, for echoing untrusted input in errors."""
    return " ".join(one_line(text).split())[:200]


def check_user_id(user_id):
    if not USER_ID_RE.fullmatch(user_id):
        raise Fail("not a LINE user id: %s" % clip(user_id))
    return user_id


def max_age(value):
    try:
        days = int(value)
    except ValueError:
        days = -1
    if days < 0:
        raise argparse.ArgumentTypeError("must be a whole number of days, 0 or more")
    return days


def is_stale(display_name, profile_at, max_age_days):
    if not str(display_name or "").strip() or not profile_at:
        return True
    try:
        fetched = datetime.fromisoformat(profile_at).astimezone()
    except (TypeError, ValueError):
        return True
    return datetime.now().astimezone() - fetched > timedelta(days=max_age_days)


def short_id(user_id):
    """The last SHORT_ID_CHARS characters of the user id ("" for none)."""
    return str(user_id or "")[-SHORT_ID_CHARS:]


def label(name, user_id):
    """What the owner sees: the name, else 名前不明（ID末尾 xxxx）, else 名前不明."""
    name = one_line(name).strip() if name else ""
    if name:
        return name
    sid = short_id(user_id)
    return UNKNOWN_ID_LABEL % sid if sid else UNKNOWN_LABEL


def open_db(path):
    if not os.path.isfile(path):
        raise Fail("database not found: %s (run init_db.py first)" % path)
    uri = "file:%s?mode=rw" % urllib.parse.quote(path)
    return sqlite3.connect(uri, uri=True, timeout=10)


def need(conn, db, table, columns):
    have = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
    if not have:
        raise Fail("table %s not found in %s (run init_db.py first)" % (table, db))
    for col in columns:
        if col not in have:
            raise Fail("column %s.%s is missing (run init_db.py first)"
                       % (table, col))


def read_name(name_file):
    """Read the name from name_file, or from stdin when name_file is None."""
    if name_file is None:
        data = getattr(sys.stdin, "buffer", sys.stdin).read(MAX_INPUT_BYTES + 1)
    else:
        if not os.path.isfile(name_file):
            raise Fail("name file not found: %s" % clip(name_file))
        with open(name_file, "rb") as f:
            data = f.read(MAX_INPUT_BYTES + 1)
    if isinstance(data, str):
        data = data.encode("utf-8")
    if len(data) > MAX_INPUT_BYTES:
        raise Fail("name input is over %d bytes; not a display name"
                   % MAX_INPUT_BYTES)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise Fail("name is not valid UTF-8 text")
    name = one_line(text).strip()[:MAX_NAME_CHARS].rstrip()
    if not name:
        raise Fail("name is empty; nothing saved")
    return name


def has_nickname(conn):
    """Only DBs upgraded by init_db.py have customers.nickname."""
    return "nickname" in {r[1] for r in conn.execute("PRAGMA table_info(customers)")}


def clean(text):
    return one_line(text).strip() if text else ""


def cached(conn, db, user_id):
    """(display name, nickname, profile_at); names as one trimmed line or ""."""
    need(conn, db, "customers", ("display_name", "profile_at"))
    nick = "nickname" if has_nickname(conn) else "NULL"
    row = conn.execute("SELECT display_name, %s, profile_at FROM customers"
                       " WHERE user_id=?" % nick, (user_id,)).fetchone()
    name, nickname, profile_at = row if row else (None, None, None)
    return clean(name), clean(nickname), profile_at


def shown(conn, user_id, name, nickname):
    """The label; a display name that reads like another person's nickname gets the ID tail."""
    if not nickname and name and has_nickname(conn):
        want = same_name(name)
        for (nick,) in conn.execute("SELECT nickname FROM customers WHERE user_id<>?"
                                    " AND nickname IS NOT NULL", (user_id,)):
            if same_name(clean(nick)) == want:
                return "%s（ID末尾 %s）" % (name, short_id(user_id))
    return label(nickname or name, user_id)


def get(conn, db, user_id, max_age_days, as_json):
    name, nickname, profile_at = cached(conn, db, user_id)
    if as_json:
        print(json.dumps({"user_id": user_id,
                          "display_name": name or None,
                          "nickname": nickname or None,
                          "profile_at": profile_at,
                          "stale": is_stale(name, profile_at, max_age_days),
                          "short_id": short_id(user_id),
                          "label": shown(conn, user_id, name, nickname)},
                         ensure_ascii=False))
    else:
        print(name or "unknown")


def print_label(conn, db, user_id):
    name, nickname, _ = cached(conn, db, user_id)
    print(shown(conn, user_id, name, nickname))


def same_name(text):
    """Compare names ignoring width, case, spaces and invisible format characters."""
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(c for c in text if not c.isspace() and unicodedata.category(c) != "Cf")


def set_nickname(conn, db, user_id, nickname):
    need(conn, db, "customers", ("display_name", "nickname"))
    with conn:
        if nickname:
            want = same_name(nickname)
            for uid, name, nick in conn.execute(
                    "SELECT user_id, display_name, nickname FROM customers"
                    " WHERE user_id<>?", (user_id,)):
                if same_name(clean(nick) or clean(name)) == want:
                    raise Fail("nickname already shown for another person (ID末尾 %s);"
                               " choose another" % short_id(uid))
        conn.execute("INSERT OR IGNORE INTO customers(user_id) VALUES(?)",
                     (user_id,))
        conn.execute("UPDATE customers SET nickname=? WHERE user_id=?",
                     (nickname, user_id))
    print("saved" if nickname else "cleared")


def set_name(conn, db, user_id, name):
    need(conn, db, "customers", ("display_name", "profile_at"))
    with conn:
        conn.execute("INSERT OR IGNORE INTO customers(user_id) VALUES(?)",
                     (user_id,))
        conn.execute("UPDATE customers SET display_name=?, profile_at=?"
                     " WHERE user_id=?", (name, now_iso(), user_id))
    print("saved")


def stale(conn, db, max_age_days, as_json):
    need(conn, db, "messages", ("direction", "source_type", "user_id"))
    need(conn, db, "customers", ("display_name", "profile_at"))
    rows = conn.execute(
        "SELECT m.user_id, c.display_name, c.profile_at FROM messages m"
        " LEFT JOIN customers c ON c.user_id = m.user_id"
        " WHERE m.direction='in' AND m.source_type IN ('user','group','room')"
        " AND m.user_id IS NOT NULL AND m.user_id <> ''"
        " GROUP BY m.user_id ORDER BY MAX(m.id) DESC").fetchall()
    ids = [uid for uid, name, profile_at in rows
           if USER_ID_RE.fullmatch(uid) and is_stale(name, profile_at, max_age_days)]
    if as_json:
        print(json.dumps(ids))
    else:
        print("\n".join(ids) if ids else "none")


def run(args, db):
    if args.cmd in ("get", "label", "set", "nickname"):
        check_user_id(args.user_id)
    name = None
    if args.cmd == "set" or (args.cmd == "nickname" and not args.clear):
        name = read_name(args.name_file)
    conn = open_db(db)
    try:
        if args.cmd == "get":
            get(conn, db, args.user_id, args.max_age_days, args.json)
        elif args.cmd == "label":
            print_label(conn, db, args.user_id)
        elif args.cmd == "set":
            set_name(conn, db, args.user_id, name)
        elif args.cmd == "nickname":
            set_nickname(conn, db, args.user_id, name)
        else:
            stale(conn, db, args.max_age_days, args.json)
    finally:
        conn.close()
    return 0


def main(argv):
    parser = argparse.ArgumentParser(
        description="Cache LINE display names read with get_profile (no API calls).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    age_help = "a name older than N days is stale (default %d)" % MAX_AGE_DAYS

    p = sub.add_parser("get", help="print the cached display name, or unknown")
    p.add_argument("user_id", help="LINE user id")
    p.add_argument("--json", action="store_true",
                   help="print user_id, display_name, nickname, profile_at, stale,"
                   " short_id, label as JSON")
    p.add_argument("--max-age-days", type=max_age, default=MAX_AGE_DAYS,
                   metavar="N", help=age_help)

    p = sub.add_parser("label", help="print the sender label the owner sees (the nickname,"
                       " the name, or a fallback with the last 4 characters of the id)")
    p.add_argument("user_id", help="LINE user id")
    p.add_argument("--max-age-days", type=max_age, default=MAX_AGE_DAYS,
                   metavar="N", help=age_help + "; a stale name still shows")

    p = sub.add_parser("set", help="save a display name read with get_profile")
    p.add_argument("user_id", help="LINE user id")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--name-file", metavar="PATH",
                     help="read the name from this file")
    src.add_argument("--name", choices=["-"],
                     help="- reads the name from stdin")

    p = sub.add_parser("nickname", help="save or clear the owner's name for this person")
    p.add_argument("user_id", help="LINE user id")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--name-file", metavar="PATH",
                     help="read the nickname from this file")
    src.add_argument("--name", choices=["-"],
                     help="- reads the nickname from stdin")
    src.add_argument("--clear", action="store_true", help="remove the nickname")

    p = sub.add_parser("stale", help="list user ids whose name needs fetching")
    p.add_argument("--max-age-days", type=max_age, default=MAX_AGE_DAYS,
                   metavar="N", help=age_help)
    p.add_argument("--json", action="store_true", help="print a JSON list")

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
    except OSError as e:
        print("error: could not read the name: %s" % clip(e.strerror or e),
              file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
