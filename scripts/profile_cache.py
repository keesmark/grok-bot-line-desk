#!/usr/bin/env python3
"""Cache LINE display names that the bot read with the line-oa MCP tool get_profile.

Usage: profile_cache.py get <userId> [--json] [--max-age-days N]
       profile_cache.py label <userId> [--max-age-days N]
       profile_cache.py set <userId> (--name-file PATH | --name -)
       profile_cache.py nickname <userId> (--name-file PATH | --name - | --clear)
       profile_cache.py stale [--max-age-days N] [--json]

get    prints the cached display name on one line, or "unknown".
       --json prints {"user_id", "display_name", "nickname", "profile_at",
       "stale", "short_id", "label", "name_clash", "like_nickname"}.
label  prints what the owner sees as the sender, on one line: the nickname,
       else the cached name (also when stale), else 名前不明（ID末尾 <short_id>）.
       A cached name of someone without a nickname that clashes gets
       （ID末尾 <short_id>） added (get --json: name_clash true): it reads the
       same as another person's nickname or display name (ignoring width,
       case, spaces, invisible marks, ID notes and a trailing 様/さま/さん/殿),
       or it holds "ID末尾". like_nickname is true when it reads like another
       person's nickname or holds "ID末尾": not the person with that nickname. short_id is the last 4 characters of the
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
       a display name but never cut: over 100 characters is refused. One that
       is another person's nickname or display name (compared as for
       name_clash), starts with 名前不明 or holds ID末尾 is refused. --clear removes it. Prints "saved" or
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


def read_name(name_file, whole=False):
    """Read the name from name_file, or from stdin when name_file is None.
    whole=True refuses a name over MAX_NAME_CHARS instead of cutting it."""
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
    name = one_line(text).strip()
    if whole and len(name) > MAX_NAME_CHARS:
        raise Fail("name is over %d characters; nothing saved" % MAX_NAME_CHARS)
    name = name[:MAX_NAME_CHARS].rstrip()
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


def name_clash(conn, user_id, name, nickname):
    """(name_clash, like_nickname) for a person without a nickname: their display
    name reads like another person's display name or nickname, or holds an ID
    note; like_nickname when it reads like a nickname or holds an ID note."""
    if nickname or not name:
        return False, False
    if has_id_note(name):
        return True, True
    want = name_key(name)
    if not want:
        return False, False
    nick = "nickname" if has_nickname(conn) else "NULL"
    clash = like = False
    for other, other_nick in conn.execute(
            "SELECT display_name, %s FROM customers WHERE user_id<>?" % nick, (user_id,)):
        like = like or want == name_key(clean(other_nick))
        clash = clash or like or want == name_key(clean(other))
    return clash, like


def shown(conn, user_id, name, nickname):
    """The label; a clashing display name gets the ID tail."""
    if name_clash(conn, user_id, name, nickname)[0]:
        return "%s（ID末尾 %s）" % (name, short_id(user_id))
    return label(nickname or name, user_id)


def get(conn, db, user_id, max_age_days, as_json):
    name, nickname, profile_at = cached(conn, db, user_id)
    clash, like = name_clash(conn, user_id, name, nickname)
    if as_json:
        print(json.dumps({"user_id": user_id,
                          "display_name": name or None,
                          "nickname": nickname or None,
                          "profile_at": profile_at,
                          "stale": is_stale(name, profile_at, max_age_days),
                          "short_id": short_id(user_id),
                          "label": shown(conn, user_id, name, nickname),
                          "name_clash": clash,
                          "like_nickname": like},
                         ensure_ascii=False))
    else:
        print(name or "unknown")


def print_label(conn, db, user_id):
    name, nickname, _ = cached(conn, db, user_id)
    print(shown(conn, user_id, name, nickname))


# Blank-looking letters (Hangul fillers, braille blank) left out when comparing names.
BLANKS = frozenset("\u115f\u1160\u2800\u3164\uffa0")


def same_name(text):
    """Compare names ignoring width, case, spaces, format characters, combining
    marks (variation selectors and the like) and blank-looking letters."""
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(c for c in text if not c.isspace() and c not in BLANKS
                   and unicodedata.category(c) not in ("Cf", "Mn", "Me"))


# A note like （ID末尾 1a2b） or a trailing 様/さま/さん/殿 does not make a name different.
ID_NOTE_RE = re.compile(r"\(?id末尾[^)]*\)?")
HONORIFIC_RE = re.compile(r"(?:様|さま|さん|殿)+$")


def name_key(text):
    """same_name without ID notes and trailing honorifics."""
    return HONORIFIC_RE.sub("", ID_NOTE_RE.sub("", same_name(text)))


def has_id_note(text):
    return "id末尾" in same_name(text)


def set_nickname(conn, db, user_id, nickname):
    need(conn, db, "customers", ("display_name", "nickname"))
    with conn:
        if nickname:
            if same_name(nickname).startswith(same_name(UNKNOWN_LABEL)) or has_id_note(nickname):
                raise Fail("a nickname cannot start with %s or hold ID末尾; choose another"
                           % UNKNOWN_LABEL)
            want = name_key(nickname)
            if not want:
                raise Fail("nickname is only spaces, marks or an honorific; choose another")
            for uid, name, nick in conn.execute(
                    "SELECT user_id, display_name, nickname FROM customers"
                    " WHERE user_id<>?", (user_id,)):
                if want in (name_key(clean(nick)), name_key(clean(name))):
                    raise Fail("nickname already used for another person (ID末尾 %s);"
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
    if args.cmd == "set":
        name = read_name(args.name_file)
    elif args.cmd == "nickname" and not args.clear:
        name = read_name(args.name_file, whole=True)
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
                   " short_id, label, name_clash, like_nickname as JSON")
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
