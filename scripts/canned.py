#!/usr/bin/env python3
"""Keep canned replies (定型文) and fill in their placeholders.

Usage: canned.py list [--json]
       canned.py show <name>
       canned.py save <name> (--text-file PATH | --text -)
       canned.py delete <name> --yes
       canned.py fill <name> [--var KEY=VALUE ...] [--out PATH]

Placeholders are written {KEY} (KEY = any characters except braces, e.g.
{名前}, {日付}); write {{ and }} for literal braces. fill puts each --var
value in as plain text. If a placeholder stays unfilled (no --var for it, or
an empty value), the text is still printed, the missing ones are listed on
stderr as "unfilled: {日付}, {時間}" and the exit code is 3.
--out writes the text to PATH (mode 0600) instead of stdout.

This script only prepares text. It never contacts LINE and never sends.

Env: DESK_DB  required (this bot's own DB path; no default)
     (table canned_replies, created by init_db.py)
Exit: 0 ok, 1 error (also delete without --yes), 3 unfilled placeholders.
"""
import argparse
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
COLUMNS = ("name", "body", "updated_at")
NAME_MAX = 50
PREVIEW_LEN = 40
# {{ and }} are literal braces; {KEY} is a placeholder (KEY has no braces).
TOKEN = re.compile(r"\{\{|\}\}|\{([^{}]+)\}")


class Fail(Exception):
    pass


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def one_line(text):
    """Line breaks and other control characters become spaces."""
    return "".join(" " if unicodedata.category(c) in ("Cc", "Zl", "Zp") else c
                   for c in str(text))


def preview(body):
    more = "..." if len(body) > PREVIEW_LEN else ""
    return one_line(body[:PREVIEW_LEN]) + more


def check_name(name):
    name = name.strip()
    if not 1 <= len(name) <= NAME_MAX:
        raise Fail("name must be 1-%d characters" % NAME_MAX)
    if one_line(name) != name:
        raise Fail("name must not contain line breaks or control characters")
    return name


def open_db():
    db = os.environ.get("DESK_DB", "")
    if not db.strip():
        raise Fail(NO_DB)
    path = os.path.abspath(db)
    if not os.path.isfile(path):
        raise Fail("database not found: %s (run init_db.py first)" % path)
    uri = "file:%s?mode=rw" % urllib.parse.quote(path)
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(canned_replies)")}
        if not cols:
            raise Fail("table canned_replies not found in %s "
                       "(run init_db.py first)" % path)
        for col in COLUMNS:
            if col not in cols:
                raise Fail("column canned_replies.%s is missing "
                           "(run init_db.py first)" % col)
    except Exception:
        conn.close()
        raise
    return conn


def get(conn, name):
    row = conn.execute("SELECT body, updated_at FROM canned_replies"
                       " WHERE name=?", (name,)).fetchone()
    if row is None:
        raise Fail("no canned reply named %s (see: canned.py list)" % name)
    return row[0] or "", row[1]


def placeholders(body):
    keys = []
    for m in TOKEN.finditer(body):
        if m.group(1) is not None and m.group(1) not in keys:
            keys.append(m.group(1))
    return keys


def braced(keys):
    return ", ".join("{%s}" % one_line(k) for k in keys) or "none"


def fill_text(body, values):
    """One pass over body, so inserted values are never read as placeholders."""
    unfilled = []

    def repl(m):
        key = m.group(1)
        if key is None:
            return m.group(0)[0]  # {{ -> {, }} -> }
        if values.get(key, "").strip():
            return values[key]
        if key not in unfilled:
            unfilled.append(key)
        return m.group(0)

    return TOKEN.sub(repl, body), unfilled


def parse_vars(items):
    values = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key or "{" in key or "}" in key:
            raise Fail("--var must be KEY=VALUE (KEY not empty, without { or })")
        if key in values:
            raise Fail("--var %s is given twice" % one_line(key))
        values[key] = one_line(value)
    return values


def read_text(args):
    if args.text_file is not None:
        with open(args.text_file, "rb") as f:
            data = f.read()
    elif args.text == "-":
        data = getattr(sys.stdin, "buffer", sys.stdin).read()
    else:
        raise Fail("--text only takes - (read the text from stdin); "
                   "use --text-file PATH for a file")
    try:
        text = data.decode("utf-8-sig") if isinstance(data, bytes) else data
    except UnicodeDecodeError:
        raise Fail("the text is not valid UTF-8")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.lstrip("\n").rstrip()
    if not text:
        raise Fail("the text is empty")
    if any(c not in "\n\t" and unicodedata.category(c) == "Cc" for c in text):
        raise Fail("the text contains control characters")
    return text


def write_private(path, text):
    """Write text to path with mode 0600, also when the file exists."""
    if os.path.islink(path):
        raise Fail("%s is a symlink; not writing through it" % one_line(path))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                 | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with open(fd, "w", encoding="utf-8") as f:
        os.fchmod(fd, 0o600)
        f.write(text)


def cmd_list(conn, args):
    rows = conn.execute("SELECT name, body, updated_at FROM canned_replies"
                        " ORDER BY name").fetchall()
    if args.json:
        print(json.dumps([{"name": n, "preview": preview(b or ""),
                           "placeholders": placeholders(b or ""),
                           "updated_at": u} for n, b, u in rows],
                         ensure_ascii=False))
        return 0
    if not rows:
        print("none")
    for name, body, _ in rows:
        print("%s: %s" % (one_line(name), preview(body or "")))
    return 0


def cmd_show(conn, args):
    name = check_name(args.name)
    body, updated_at = get(conn, name)
    print("name: %s" % name)
    print("updated_at: %s" % one_line(updated_at or "-"))
    print("placeholders: %s" % braced(placeholders(body)))
    print("---")
    print(body)
    return 0


def cmd_save(conn, args):
    name = check_name(args.name)
    body = read_text(args)
    at = now_iso()
    with conn:
        cur = conn.execute("UPDATE canned_replies SET body=?, updated_at=?"
                           " WHERE name=?", (body, at, name))
        updated = cur.rowcount > 0
        if not updated:
            conn.execute("INSERT INTO canned_replies(name, body, updated_at)"
                         " VALUES(?, ?, ?)", (name, body, at))
    print("updated" if updated else "saved")
    return 0


def cmd_delete(conn, args):
    name = check_name(args.name)
    body, _ = get(conn, name)
    if not args.yes:
        print("would delete %s: %s" % (name, preview(body)))
        print("not deleted; add --yes to delete it", file=sys.stderr)
        return 1
    with conn:
        conn.execute("DELETE FROM canned_replies WHERE name=?", (name,))
    print("deleted %s" % name)
    return 0


def cmd_fill(conn, args):
    name = check_name(args.name)
    values = parse_vars(args.var or [])
    body, _ = get(conn, name)
    text, unfilled = fill_text(body, values)
    if args.out:
        write_private(args.out, text)
        print("wrote %s" % one_line(args.out))
    else:
        print(text)
    keys = placeholders(body)
    unused = [k for k in values if k not in keys]
    if unused:
        print("warning: no such placeholder, ignored: %s" % braced(unused),
              file=sys.stderr)
    if unfilled:
        print("unfilled: %s" % braced(unfilled), file=sys.stderr)
        return 3
    return 0


COMMANDS = {"list": cmd_list, "show": cmd_show, "save": cmd_save,
            "delete": cmd_delete, "fill": cmd_fill}


def main(argv):
    parser = argparse.ArgumentParser(
        description="Keep canned replies (定型文) and fill in placeholders. "
                    "Never sends anything.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="names with the start of each text")
    p.add_argument("--json", action="store_true", help="print JSON")
    p = sub.add_parser("show", help="full text and its placeholders")
    p.add_argument("name")
    p = sub.add_parser("save", help="add or replace a canned reply")
    p.add_argument("name")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--text-file", metavar="PATH",
                     help="read the text from a UTF-8 file")
    src.add_argument("--text", metavar="-",
                     help="give - to read the text from stdin")
    p = sub.add_parser("delete", help="delete a canned reply")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true", help="really delete it")
    p = sub.add_parser("fill", help="fill in placeholders (never sends)")
    p.add_argument("name")
    p.add_argument("--var", action="append", metavar="KEY=VALUE",
                   help="value for {KEY}; repeat for more")
    p.add_argument("--out", metavar="PATH",
                   help="write the text to PATH (mode 0600) instead of stdout")
    args = parser.parse_args(argv)
    try:
        conn = open_db()
        try:
            return COMMANDS[args.cmd](conn, args)
        finally:
            conn.close()
    except Fail as e:
        print("error: %s" % e, file=sys.stderr)
    except sqlite3.Error as e:
        print("error: database problem: %s" % e, file=sys.stderr)
    except OSError as e:
        where = ": " + one_line(e.filename) if e.filename else ""
        print("error: %s%s" % (e.strerror or e, where), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
