#!/usr/bin/env python3
"""Keyword auto-reply (キーワード応答): keyword rules and one decision per inbound message.

Usage: keyword_reply.py add <keyword> (--reply-file PATH | --reply -) [--match exact|contains]
                      [--disabled] [--json]
       keyword_reply.py list [--all] [--json]
       keyword_reply.py show <id> [--json] [--out PATH]
       keyword_reply.py edit <id> [--keyword K] [--reply-file PATH | --reply -] [--match exact|contains]
       keyword_reply.py enable <id>
       keyword_reply.py disable <id>
       keyword_reply.py remove <id> [--yes]
       keyword_reply.py setting [on|off] [--window-hours N] [--min-remaining N] [--json]
       keyword_reply.py match <message_id> [--remaining N|unlimited|unknown] [--out PATH] [--json]
       keyword_reply.py sent <message_id> --result sent|unknown|failed [--line-message-id ID]
                      [--error TEXT]
       keyword_reply.py text <message_id> --out PATH
       keyword_reply.py pending [--json]

Rules: keywords are compared after NFKC + casefold with all spaces removed.
exact = the whole message is the keyword, contains = the keyword is in it.
Only enabled rules count. If several rules match, exact wins, then the
longest keyword, then the lowest id. The reply (--reply-file UTF-8 file or
--reply - for stdin) is stored and later sent exactly as it is: 1-5000
characters, no control characters except line break and tab, and no unfilled
placeholder like {名前} ({{ and }} are allowed and kept as they are).
add/edit refuse a keyword that is already registered with the same match
type. list shows enabled rules (--all: all of them). show --out writes the
reply text exactly (mode 0600). remove without --yes only prints
"would remove <id>". Past matches keep their own copy of the reply.

setting: keyword.json next to the DB (mode 0600). Default {"auto_send": false,
"window_hours": 24, "min_remaining": 0}; a broken file counts as the default
with auto_send off. Run "setting on" only when the owner said so in chat.
window_hours >= 1, min_remaining >= 0 (0 = never stop for the quota).

match (run after log_event.py stored a 1:1 text message) prints
"action=<a> reason=<r> rule=<id>" (--json: the full result):
  already    decided before (redelivered webhook); nothing changes
  none       not a 1:1 text (not_1to1_text), no rule matches (no_match) or the
             message came for another LINE account (other_account)
  suggest    auto_send off (off), same rule auto-sent to this user within
             window_hours (window), --remaining unknown (quota_unknown) or
             below min_remaining (quota_low); the owner decides
  need_quota auto_send on, min_remaining > 0 and no --remaining: run
             quota_check.py --peek --json, then run match again with --remaining
             (remaining null = no monthly limit: --remaining unlimited or null;
             the check failed: --remaining unknown)
  auto_send  decision auto_pending; push reply_text (--out writes it, 0600),
             then run sent
need_quota / quota_unknown / quota_low only happen when min_remaining > 0; with
0 (the default) the quota is not checked and --remaining is ignored.
sent logs the pushed reply through log_event.py (status auto_reply,
auto_reply_unknown or auto_reply_failed) and records the result (only for an
auto_pending match); --error TEXT (only with failed / unknown) passes the
send error on to log_event.py. text writes the stored reply of a message's
match (for 「送信 N」). pending lists suggestions, and auto-sends that failed
(reason auto_failed), whose user has had no reply after the message yet.

This script never sends and never contacts LINE. Customer message text is
never printed: output holds only ids, keywords and rule fields.

Env: DESK_DB  required (this bot's own DB path; no default)
     (tables keyword_rules / keyword_matches, created by init_db.py)
Exit: 0 ok, 2 bad input / not found / conflict / not pending,
      1 database problem or log_event.py failed.
"""
# PART A API (PART B uses these; every cmd_* returns the exit code; imports incl. subprocess, timedelta):
#  Fail(msg, code=2)            raise on errors; main prints "error: <msg>" and returns .code (1 = DB/log_event)
#  db_path() -> str             connect() -> sqlite3.Connection: autocommit (isolation_level=None),
#                               row_factory=sqlite3.Row, 10 s busy timeout; Fail(code 1, "run init_db.py
#                               first") if the DB or messages/keyword_rules/keyword_matches is missing
#  transaction(con)             with-block: BEGIN IMMEDIATE ... COMMIT (ROLLBACK when it raises)
#  now_iso() -> str (UTC)       parse_time(s) -> aware datetime | None (no offset = UTC)
#  norm(s) -> str               NFKC + casefold, whitespace removed ("" possible)
#  one_line(s) -> str           preview(s, n=30) -> str     warn(msg)     print_json(obj)
#  int_at_least(low)            argparse type: int >= low
#  clean_keyword(k) -> (keyword, keyword_norm)   Fail if empty / control chars / too long
#  validate_reply(text) -> str  read_text_arg(args) -> str | None  (args.reply_file / args.reply == "-")
#  settings_path() -> str       load_settings() -> {"auto_send": bool, "window_hours": int,
#                               "min_remaining": int (default 0 = no quota check)} (never raises)
#                               save_settings(s) -> path
#  write_private(path, text)    mode 0600, refuses symlinks, writes the text exactly
#  rule_row_to_dict(row) -> dict (id keyword match_type enabled reply_text created_at updated_at)
#  get_rule(con, rule_id) -> Row (Fail if none)   pick_rule(con, text) -> Row | None (best enabled match)
#  constants: TABLES DEFAULT_SETTINGS MATCH_TYPES AUTO_DECISIONS RESULTS LOG_EVENT RULE_COLS
#  cmd_add cmd_list cmd_show cmd_edit cmd_enable cmd_disable cmd_remove (con, args) -> int
#  cmd_setting(args) -> int     needs no DB connection (only uses the DB folder)
#  args dests: keyword reply_file reply match (add: default "exact"; edit: None) disabled json all id
#              out yes; setting: state (None|"on"|"off") window_hours min_remaining (None = keep)
import argparse
import contextlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
import urllib.parse
from datetime import datetime, timedelta, timezone

NO_DB = ("DESK_DB is not set: give this bot's own database path, e.g. "
         "DESK_DB=/workspace/line-inbox/<agent id>/messages.db "
         "(see 保存先 in memory.md); not using a shared default")
TABLES = ("messages", "keyword_rules", "keyword_matches")
SETTINGS_FILE = "keyword.json"
DEFAULT_SETTINGS = {"auto_send": False, "window_hours": 24, "min_remaining": 0}
MATCH_TYPES = ("exact", "contains")
AUTO_DECISIONS = ("auto_pending", "auto_sent", "auto_unknown")  # these count for the window
# sent --result -> (log_event.py --status, keyword_matches.decision)
RESULTS = {"sent": ("auto_reply", "auto_sent"), "unknown": ("auto_reply_unknown", "auto_unknown"),
           "failed": ("auto_reply_failed", "auto_failed")}
LOG_EVENT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log_event.py")
RULE_COLS = "id, keyword, keyword_norm, match_type, reply_text, enabled, created_at, updated_at"
KEYWORD_MAX = 100
REPLY_MAX = 5000
PREVIEW_LEN = 30
# {{ and }} are literal braces; {KEY} is an unfilled placeholder.
TOKEN = re.compile(r"\{\{|\}\}|\{([^{}]+)\}")


class Fail(Exception):
    def __init__(self, msg, code=2):
        super().__init__(msg)
        self.code = code


def db_path():
    db = os.environ.get("DESK_DB", "")
    if not db.strip():
        raise Fail(NO_DB, 1)
    return os.path.abspath(db)


def connect():
    path = db_path()
    if not os.path.isfile(path):
        raise Fail("database not found: %s (run init_db.py first)" % path, 1)
    con = sqlite3.connect("file:%s?mode=rw" % urllib.parse.quote(path), uri=True,
                          timeout=10, isolation_level=None)
    try:
        con.execute("PRAGMA busy_timeout=10000")
        have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in TABLES if t not in have]
        if missing:
            raise Fail("table %s not found in %s (run init_db.py first)" % (", ".join(missing), path), 1)
    except Exception:
        con.close()
        raise
    con.row_factory = sqlite3.Row
    return con


@contextlib.contextmanager
def transaction(con):
    """BEGIN IMMEDIATE ... COMMIT; ROLLBACK when the block raises."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_time(s):
    """ISO 8601 text -> aware datetime (no offset = UTC); None if empty or unreadable."""
    try:
        d = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def norm(s):
    return "".join(unicodedata.normalize("NFKC", s or "").casefold().split())


def one_line(text):
    """Line breaks and other control characters become spaces."""
    return "".join(" " if unicodedata.category(c) in ("Cc", "Zl", "Zp") else c for c in str(text))


def preview(text, n=PREVIEW_LEN):
    return one_line(text[:n]) + ("..." if len(text) > n else "")


def warn(msg):
    print("warning: %s" % msg, file=sys.stderr)


def print_json(obj):
    print(json.dumps(obj, ensure_ascii=False))


def int_at_least(low):
    def parse(s):
        try:
            n = int(s)
        except ValueError:
            n = None
        if n is None or n < low:
            raise argparse.ArgumentTypeError("must be an integer >= %d" % low)
        return n
    return parse


def clean_keyword(k):
    k = (k or "").strip()
    if any(unicodedata.category(c) == "Cc" for c in k):
        raise Fail("the keyword must not contain line breaks or control characters")
    if len(k) > KEYWORD_MAX:
        raise Fail("the keyword is longer than %d characters" % KEYWORD_MAX)
    kn = norm(k)
    if not kn:
        raise Fail("the keyword is empty")
    return k, kn


def validate_reply(text):
    """Line breaks become \\n, outer blank lines and trailing spaces are dropped, then checked."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\n").rstrip()
    if not text:
        raise Fail("the reply text is empty")
    if len(text) > REPLY_MAX:
        raise Fail("the reply text has %d characters; at most %d" % (len(text), REPLY_MAX))
    if any(c not in "\n\t" and unicodedata.category(c) == "Cc" for c in text):
        raise Fail("the reply text contains control characters")
    for m in TOKEN.finditer(text):
        if m.group(1) is not None:
            raise Fail("the reply text has an unfilled placeholder {%s}; fill it in "
                       "(write {{ and }} for braces)" % preview(m.group(1), 20))
    return text


def read_text_arg(args):
    """The reply from --reply-file PATH or --reply - (stdin), validated; None if neither is given."""
    if getattr(args, "reply_file", None) is not None:
        with open(args.reply_file, "rb") as f:
            data = f.read()
    elif getattr(args, "reply", None) is not None:
        if args.reply != "-":
            raise Fail("--reply only takes - (read the text from stdin); use --reply-file PATH for a file")
        data = getattr(sys.stdin, "buffer", sys.stdin).read()
    else:
        return None
    try:
        text = data.decode("utf-8-sig") if isinstance(data, bytes) else data
    except UnicodeDecodeError:
        raise Fail("the reply text is not valid UTF-8")
    return validate_reply(text)


def settings_path():
    return os.path.join(os.path.dirname(db_path()), SETTINGS_FILE)


def load_settings():
    """keyword.json over the defaults. Never raises: a broken file counts as the defaults
    with auto_send off (warning on stderr). Only JSON true turns auto_send on."""
    s, path = dict(DEFAULT_SETTINGS), settings_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return s
    except Exception as e:
        warn("%s could not be read (%s); using the defaults, auto_send off" % (path, type(e).__name__))
        return s
    if not isinstance(data, dict):
        warn("%s is not a JSON object; using the defaults, auto_send off" % path)
        return s
    s["auto_send"] = data.get("auto_send") is True
    for key, low in (("window_hours", 1), ("min_remaining", 0)):
        v = data.get(key, s[key])
        if isinstance(v, int) and not isinstance(v, bool) and v >= low:
            s[key] = v
        else:
            warn("%s: %s must be an integer >= %d; using %d" % (path, key, low, s[key]))
    return s


def save_settings(s):
    """Write keyword.json atomically (tmp file + os.replace), mode 0600; returns its path."""
    path = settings_path()
    fd, tmp = tempfile.mkstemp(prefix=".keyword.", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with open(fd, "w", encoding="utf-8") as f:
            os.fchmod(fd, 0o600)
            json.dump({k: s[k] for k in DEFAULT_SETTINGS}, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(fd)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def write_private(path, text):
    """Write text exactly to path with mode 0600, also when the file exists; never via a symlink."""
    if os.path.islink(path):
        raise Fail("%s is a symlink; not writing through it" % one_line(path))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with open(fd, "w", encoding="utf-8", newline="") as f:
        os.fchmod(fd, 0o600)
        f.write(text)


def rule_row_to_dict(row):
    d = {k: row[k] for k in ("id", "keyword", "match_type", "enabled", "reply_text",
                             "created_at", "updated_at")}
    d["enabled"] = bool(d["enabled"])
    return d


def get_rule(con, rule_id):
    row = con.execute("SELECT %s FROM keyword_rules WHERE id=?" % RULE_COLS, (rule_id,)).fetchone()
    if row is None:
        raise Fail("no rule %s (see: keyword_reply.py list --all)" % rule_id)
    return row


def pick_rule(con, text):
    """The one enabled rule that matches: exact first, then longest keyword_norm, then lowest id."""
    t = norm(text)
    if not t:
        return None
    hits = [r for r in con.execute("SELECT %s FROM keyword_rules WHERE enabled=1" % RULE_COLS)
            if (r["keyword_norm"] == t if r["match_type"] == "exact" else r["keyword_norm"] in t)]
    hits.sort(key=lambda r: (r["match_type"] != "exact", -len(r["keyword_norm"]), r["id"]))
    return hits[0] if hits else None


def find_dup(con, kn, mt, not_id=-1):
    row = con.execute("SELECT id FROM keyword_rules WHERE keyword_norm=? AND match_type=? AND id<>?",
                      (kn, mt, not_id)).fetchone()
    if row:
        raise Fail("already registered as %d" % row[0])


def cmd_add(con, args):
    kw, kn = clean_keyword(args.keyword)
    mt = args.match or "exact"
    text = read_text_arg(args)
    if text is None:
        raise Fail("give the reply with --reply-file PATH or --reply -")
    at = now_iso()
    with transaction(con):
        find_dup(con, kn, mt)
        rid = con.execute("INSERT INTO keyword_rules(keyword, keyword_norm, match_type, reply_text,"
                          " enabled, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                          (kw, kn, mt, text, 0 if args.disabled else 1, at, at)).lastrowid
    if getattr(args, "json", False):
        print_json(rule_row_to_dict(get_rule(con, rid)))
    else:
        print("added %d" % rid)
    return 0


def cmd_list(con, args):
    where = "" if args.all else " WHERE enabled=1"
    rows = con.execute("SELECT %s FROM keyword_rules%s ORDER BY id" % (RULE_COLS, where)).fetchall()
    if args.json:
        print_json([rule_row_to_dict(r) for r in rows])
        return 0
    if not rows:
        print("none")
    for r in rows:
        print("%d %s %s %s %d chars: %s" % (
            r["id"], "on" if r["enabled"] else "off", r["match_type"],
            json.dumps(r["keyword"], ensure_ascii=False), len(r["reply_text"]), preview(r["reply_text"])))
    return 0


def cmd_show(con, args):
    d = rule_row_to_dict(get_rule(con, args.id))
    if args.out:
        write_private(args.out, d["reply_text"])
    if args.json:
        print_json(d)
        return 0
    print("id: %d\nkeyword: %s\nmatch: %s\nenabled: %s" % (
        d["id"], one_line(d["keyword"]), d["match_type"], "on" if d["enabled"] else "off"))
    print("created_at: %s\nupdated_at: %s\nreply: %d chars" % (
        one_line(d["created_at"]), one_line(d["updated_at"]), len(d["reply_text"])))
    print("wrote %s" % one_line(args.out) if args.out else "---\n" + d["reply_text"])
    return 0


def cmd_edit(con, args):
    kw = clean_keyword(args.keyword)[0] if args.keyword is not None else None
    text = read_text_arg(args)
    if kw is None and text is None and args.match is None:
        raise Fail("nothing to change; give --keyword, --reply-file / --reply or --match")
    with transaction(con):
        r = get_rule(con, args.id)
        k = kw or r["keyword"]
        kn, mt = norm(k) or r["keyword_norm"], args.match or r["match_type"]
        find_dup(con, kn, mt, r["id"])
        con.execute("UPDATE keyword_rules SET keyword=?, keyword_norm=?, match_type=?, reply_text=?,"
                    " updated_at=? WHERE id=?",
                    (k, kn, mt, r["reply_text"] if text is None else text, now_iso(), r["id"]))
    print("updated %d" % r["id"])
    return 0


def set_enabled(con, rule_id, on):
    with transaction(con):
        get_rule(con, rule_id)
        con.execute("UPDATE keyword_rules SET enabled=?, updated_at=? WHERE id=?",
                    (1 if on else 0, now_iso(), rule_id))
    print("%s %d" % ("enabled" if on else "disabled", rule_id))
    return 0


def cmd_enable(con, args):
    return set_enabled(con, args.id, True)


def cmd_disable(con, args):
    return set_enabled(con, args.id, False)


def cmd_remove(con, args):
    r = get_rule(con, args.id)
    if not args.yes:
        print("would remove %d" % r["id"])
        print("not removed; add --yes to remove it", file=sys.stderr)
        return 0
    with transaction(con):
        con.execute("DELETE FROM keyword_rules WHERE id=?", (r["id"],))
    print("removed %d" % r["id"])
    return 0


def cmd_setting(args):
    state, wh, mr = args.state, args.window_hours, args.min_remaining
    if wh is not None and wh < 1:
        raise Fail("--window-hours must be an integer >= 1")
    if mr is not None and mr < 0:
        raise Fail("--min-remaining must be an integer >= 0")
    path, s = settings_path(), load_settings()
    changed = state is not None or wh is not None or mr is not None
    if changed:
        if state is not None:
            s["auto_send"] = state == "on"
        s["window_hours"] = s["window_hours"] if wh is None else wh
        s["min_remaining"] = s["min_remaining"] if mr is None else mr
        save_settings(s)
    if getattr(args, "json", False):
        print_json(dict(s, file=path, exists=os.path.exists(path), saved=changed))
        return 0
    if changed:
        print("saved")
    print("auto_send: %s\nwindow_hours: %d\nmin_remaining: %d" % (
        "on" if s["auto_send"] else "off", s["window_hours"], s["min_remaining"]))
    print("file: %s%s" % (path, "" if os.path.exists(path) else " (not created yet; defaults)"))
    return 0


def _m_remaining(s):
    """--remaining -> None (not given), "unlimited", "unknown" or an int >= 0.

    "null" is quota_check.py --json's remaining for an account with no monthly limit."""
    if s == "null":
        return "unlimited"
    if s is None or s in ("unlimited", "unknown"):
        return s
    try:
        n = int(s)
    except ValueError:
        n = -1
    if n < 0:
        raise Fail("--remaining must be an integer >= 0, unlimited or unknown")
    return n


def _m_in_window(con, user_id, rule_id, hours):
    """True if this rule was auto-sent (or is being sent) to this user in the last `hours`."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    for r in con.execute("SELECT matched_at FROM keyword_matches WHERE user_id=? AND rule_id=?"
                         " AND decision IN (?, ?, ?)", (user_id, rule_id) + AUTO_DECISIONS):
        t = parse_time(r[0])
        if t is None or t >= since:  # an unreadable time counts as recent: never send twice
            return True
    return False


def _m_decide(con, mid, remaining, s, out):
    """One decision for message mid; runs inside the caller's BEGIN IMMEDIATE transaction."""
    old = con.execute("SELECT id, user_id, rule_id, keyword, match_type, reply_text, decision, reason"
                      " FROM keyword_matches WHERE message_id=?", (mid,)).fetchone()
    if old is not None:
        d = {k: old[k] for k in ("user_id", "rule_id", "keyword", "match_type", "reply_text",
                                 "decision", "reason")}
        return dict(d, action="already", match_id=old["id"])
    m = con.execute("SELECT direction, message_type, source_type, user_id, text, status"
                    " FROM messages WHERE id=?", (mid,)).fetchone()
    if m is None:
        raise Fail("no message %s" % mid)
    res = {"user_id": m["user_id"]}
    if m["status"] == "other_account":  # for another LINE account: never answer from this one
        return dict(res, reason="other_account")
    if (m["direction"], m["message_type"], m["source_type"]) != ("in", "text", "user") \
            or not m["user_id"] or not m["text"]:
        return dict(res, reason="not_1to1_text")
    rule = pick_rule(con, m["text"])
    if rule is None:
        return dict(res, reason="no_match")
    res.update(rule_id=rule["id"], keyword=rule["keyword"], match_type=rule["match_type"])
    if not s["auto_send"]:
        reason = "off"
    elif _m_in_window(con, m["user_id"], rule["id"], s["window_hours"]):
        reason = "window"
    elif s["min_remaining"] == 0:  # the default: no quota check, --remaining is ignored
        reason = "auto"
    elif remaining is None:
        return dict(res, action="need_quota", reason="no_remaining")
    elif remaining == "unknown":
        reason = "quota_unknown"
    elif remaining != "unlimited" and remaining < s["min_remaining"]:
        reason = "quota_low"
    else:
        reason = "auto"
    decision, at = "auto_pending" if reason == "auto" else "suggested", now_iso()
    match_id = con.execute(
        "INSERT INTO keyword_matches(message_id, rule_id, user_id, keyword, match_type, reply_text,"
        " decision, reason, matched_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (mid, rule["id"], m["user_id"], rule["keyword"], rule["match_type"], rule["reply_text"],
         decision, reason, at, at)).lastrowid
    if decision == "auto_pending" and out:
        write_private(out, rule["reply_text"])  # inside the transaction: a failed write stores nothing
    return dict(res, action="auto_send" if decision == "auto_pending" else "suggest", reason=reason,
                decision=decision, reply_text=rule["reply_text"], match_id=match_id)


def cmd_match(con, args):
    remaining, s = _m_remaining(args.remaining), load_settings()
    res = {"action": "none", "reason": None, "message_id": args.id, "user_id": None, "rule_id": None,
           "keyword": None, "match_type": None, "match_id": None}
    with transaction(con):
        res.update(_m_decide(con, args.id, remaining, s, args.out))
    if args.json:
        print_json(res)
    else:
        print("action=%s reason=%s rule=%s" % (res["action"], one_line(res["reason"] or "-"),
                                               "-" if res["rule_id"] is None else res["rule_id"]))
    return 0


def cmd_sent(con, args):
    err = getattr(args, "error", None)
    if err is not None and args.result not in ("failed", "unknown"):
        raise Fail("--error only goes with --result failed or unknown; nothing was logged")
    m = con.execute("SELECT id, user_id, reply_text, decision FROM keyword_matches WHERE message_id=?",
                    (args.id,)).fetchone()
    if m is None:
        raise Fail("no keyword match for message %s" % args.id)
    if m["decision"] != "auto_pending":
        raise Fail("not pending: message %s has decision %s" % (args.id, m["decision"]))
    if not m["user_id"]:
        raise Fail("match %d has no user_id" % m["id"])
    status, decision = RESULTS[args.result]
    lmid = getattr(args, "line_message_id", None)
    # The snapshot goes to log_event.py through a 0600 file in the (private) DB folder.
    fd, tmp = tempfile.mkstemp(prefix=".keyword-sent.", suffix=".txt", dir=os.path.dirname(db_path()))
    os.close(fd)
    try:
        write_private(tmp, m["reply_text"])
        cmd = [sys.executable, LOG_EVENT, "--out", "--to", m["user_id"], "--text-file", tmp,
               "--status", status]
        cmd += ["--line-message-id", lmid] if lmid else []
        cmd += ["--error=" + err] if err is not None else []
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=120, env=dict(os.environ, DESK_DB=db_path()))
    except (OSError, subprocess.SubprocessError) as e:
        raise Fail("log_event.py could not run (%s); the match stays auto_pending"
                   % one_line(e), 1)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
    got = re.search(r"^inserted (\d+)\b", p.stdout or "", re.M)
    if p.returncode != 0 or not got:
        err = (p.stderr or "").strip()
        if err:
            print(err, file=sys.stderr)
        raise Fail("log_event.py did not log the reply (exit %d); the match stays auto_pending"
                   % p.returncode, 1)
    out_id = int(got.group(1))
    n = con.execute("UPDATE keyword_matches SET decision=?, out_message_id=?, updated_at=?"
                    " WHERE id=? AND decision='auto_pending'",
                    (decision, out_id, now_iso(), m["id"])).rowcount
    if not n:
        raise Fail("logged %d but match %d is no longer pending (another sent run?)" % (out_id, m["id"]))
    print("logged %d decision=%s" % (out_id, decision))
    return 0


def cmd_text(con, args):
    row = con.execute("SELECT id, decision, reply_text FROM keyword_matches WHERE message_id=?",
                      (args.id,)).fetchone()
    if row is None:
        raise Fail("no keyword match for message %s (see: keyword_reply.py pending)" % args.id)
    if row["decision"] not in ("suggested", "auto_failed"):
        warn("match %d has decision %s; check it was not sent already" % (row["id"], row["decision"]))
    write_private(args.out, row["reply_text"])
    print("wrote %s match=%d chars=%d" % (one_line(args.out), row["id"], len(row["reply_text"])))
    return 0


PENDING_FIELDS = ("match_id", "message_id", "user_id", "rule_id", "keyword", "reason",
                  "reply_text", "matched_at")


def cmd_pending(con, args):
    """'suggested' and 'auto_failed' (shown with reason auto_failed) matches whose user got
    no reply (a push to them that did not fail) at or after the inbound message.
    Times are compared as parsed datetimes."""
    rows = con.execute(
        "SELECT k.id AS match_id, k.message_id, k.user_id, k.rule_id, k.keyword,"
        " CASE k.decision WHEN 'auto_failed' THEN 'auto_failed' ELSE k.reason END AS reason,"
        " k.reply_text, k.matched_at, m.at AS in_at FROM keyword_matches k"
        " JOIN messages m ON m.id=k.message_id WHERE k.decision IN ('suggested', 'auto_failed')"
        " ORDER BY k.id").fetchall()
    outs, items = {}, []
    for r in rows:
        uid = r["user_id"]
        if uid not in outs:
            outs[uid] = [t for t in (parse_time(o[0]) for o in con.execute(
                "SELECT at FROM messages WHERE direction='out' AND user_id=?"
                " AND IFNULL(status,'') NOT IN ('failed','auto_reply_failed')"
                " AND IFNULL(send_kind,'push')='push'", (uid,))) if t is not None]
        t = parse_time(r["in_at"])
        if t is not None and any(o >= t for o in outs[uid]):
            continue
        items.append({k: r[k] for k in PENDING_FIELDS})
    if args.json:
        print_json(items)
        return 0
    if not items:
        print("none")
    for d in items:
        print("match=%d message=%d user=%s rule=%s keyword=%s reason=%s matched_at=%s %d chars: %s" % (
            d["match_id"], d["message_id"], one_line(d["user_id"] or "-"),
            "-" if d["rule_id"] is None else d["rule_id"], json.dumps(d["keyword"], ensure_ascii=False),
            one_line(d["reason"] or "-"), one_line(d["matched_at"]), len(d["reply_text"]),
            preview(d["reply_text"])))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="Keyword auto-reply (キーワード応答): keyword rules and one decision per "
                    "inbound message. Never sends anything and never contacts LINE.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def command(name, text, id_name=None, js=False):
        p = sub.add_parser(name, help=text)
        if id_name:
            p.add_argument("id", type=int, metavar=id_name)
        if js:
            p.add_argument("--json", action="store_true", help="print JSON")
        return p

    def reply_source(p, required):
        g = p.add_mutually_exclusive_group(required=required)
        g.add_argument("--reply-file", metavar="PATH", help="read the reply from a UTF-8 file")
        g.add_argument("--reply", metavar="-", help="give - to read the reply from stdin")

    p = command("add", "add a rule", js=True)
    p.add_argument("keyword")
    reply_source(p, True)
    p.add_argument("--match", choices=MATCH_TYPES, default="exact")
    p.add_argument("--disabled", action="store_true", help="add it switched off")
    p = command("list", "enabled rules", js=True)
    p.add_argument("--all", action="store_true", help="also the disabled rules")
    p = command("show", "one rule with its full reply", "id", js=True)
    p.add_argument("--out", metavar="PATH", help="write the reply text exactly to PATH (mode 0600)")
    p = command("edit", "change a rule", "id")
    p.add_argument("--keyword")
    reply_source(p, False)
    p.add_argument("--match", choices=MATCH_TYPES)
    command("enable", "switch a rule on", "id")
    command("disable", "switch a rule off", "id")
    p = command("remove", "remove a rule (dry run without --yes)", "id")
    p.add_argument("--yes", action="store_true", help="really remove it")
    p = command("setting", "show or change auto_send / window_hours / min_remaining", js=True)
    p.add_argument("state", nargs="?", choices=("on", "off"),
                   help="turn auto_send on or off (only when the owner said so in chat)")
    p.add_argument("--window-hours", type=int_at_least(1), metavar="N")
    p.add_argument("--min-remaining", type=int_at_least(0), metavar="N")
    p = command("match", "decide one stored inbound message", "message_id", js=True)
    p.add_argument("--remaining", metavar="N|unlimited|unknown",
                   help="push messages left (from quota_check.py --peek --json); "
                        "ignored when min_remaining is 0")
    p.add_argument("--out", metavar="PATH", help="auto_send: write the reply text to PATH (mode 0600)")
    p = command("sent", "log and record the result of an auto-sent reply", "message_id")
    p.add_argument("--result", choices=tuple(RESULTS), required=True)
    p.add_argument("--line-message-id", metavar="ID")
    p.add_argument("--error", metavar="TEXT",
                   help="the error the send came back with (only with --result failed or unknown); "
                        "passed on to log_event.py")
    p = command("text", "write the stored reply of a message's match", "message_id")
    p.add_argument("--out", metavar="PATH", required=True, help="write it to PATH (mode 0600)")
    command("pending", "suggestions and failed auto-sends (reason auto_failed) still waiting "
                       "for the owner", js=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    commands = {"add": cmd_add, "list": cmd_list, "show": cmd_show, "edit": cmd_edit,
                "enable": cmd_enable, "disable": cmd_disable, "remove": cmd_remove,
                "match": cmd_match, "sent": cmd_sent, "text": cmd_text, "pending": cmd_pending}
    try:
        if args.cmd == "setting":
            return cmd_setting(args)
        con = connect()
        try:
            return commands[args.cmd](con, args)
        finally:
            con.close()
    except Fail as e:
        print("error: %s" % e, file=sys.stderr)
        return e.code
    except sqlite3.Error as e:
        print("error: database problem: %s (run init_db.py first if a table is missing)" % e,
              file=sys.stderr)
        return 1
    except OSError as e:
        where = ": " + one_line(e.filename) if e.filename else ""
        print("error: %s%s" % (e.strerror or e, where), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
