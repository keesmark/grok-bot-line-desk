#!/usr/bin/env python3
"""Counts for the weekly report (on request) and inquiry summary, so the bot never has to count by itself.

Usage:
  stats.py [--days N] [--json]
      the last N days (N x 24h) up to now; default 7
  stats.py --since YYYY-MM-DD [--until YYYY-MM-DD] [--json]
      whole days in local time; the --until day is included

For the period:
  inbound messages: total, by source_type, text vs other, distinct chats
  other inbound events: follow, unfollow, join, ... (not messages)
  outbound messages: total, by status (sent, app_reply, auto_reply, failed,
  ...) and by send_kind (push, segment, broadcast; older rows without it
  count as push).
  A segment or broadcast send is one row, however many friends it went to.
Right now (not limited to the period):
  unreplied chats, same rule as list_unreplied.py, 1:1 and group/room separately
  (segment/broadcast sends have no chat and are never a reply; failed and
  auto_reply_failed sends are not a reply, auto_reply and auto_reply_unknown are)
  time of the latest inbound message and how long ago (to notice that
  reception has stopped)
--json prints the same as one JSON object; outbound_by_kind holds the counts
by send_kind.

DB: env DESK_DB, required: this bot's own DB path, no default (made by init_db.py).
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

NO_DB = ("DESK_DB is not set: give this bot's own database path, e.g. "
         "DESK_DB=/workspace/line-inbox/<agent id>/messages.db "
         "(see 保存先 in memory.md); not using a shared default")
# Inbound rows that are customer messages (not follow, unfollow, join, ... events).
IS_MESSAGE = ("(line_message_id IS NOT NULL OR IFNULL(message_type, '') IN "
              "('text', 'image', 'video', 'audio', 'file', 'location', 'sticker'))")
CHAT_KEY = ("CASE source_type WHEN 'group' THEN group_id"
            " WHEN 'room' THEN room_id ELSE user_id END")
# Outbound rows that count as a reply: not failed (auto_reply / auto_reply_unknown count) ...
REPLY = "direction = 'out' AND IFNULL(status, '') NOT IN ('failed', 'auto_reply_failed')"
# ... and not a segment/broadcast send (user_id NULL, belongs to no chat).
NOT_BULK = "IFNULL(send_kind, 'push') NOT IN ('segment', 'broadcast')"


class DeskError(Exception):
    """A short, clear error for the bot."""


def open_db():
    path = os.environ.get("DESK_DB", "")
    if not path.strip():
        raise DeskError(NO_DB)
    if not os.path.isfile(path):
        raise DeskError(f"database not found: {path} (run init_db.py first)")
    con = sqlite3.connect(path, timeout=10)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for name in ("messages", "conversations"):
        if name not in tables:
            raise DeskError(f"table {name} is missing in {path} (run init_db.py first)")
    return con


def parse_date(text):
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"bad date {text!r}, use YYYY-MM-DD")


def iso(t):
    return t.isoformat(timespec="seconds")


def local_midnight(day):
    return datetime(day.year, day.month, day.day).astimezone()


def get_period(args, now):
    """Return (since, until) as local datetimes; until None means up to now."""
    if args.since is None:
        if args.until is not None:
            raise DeskError("--until needs --since")
        days = 7 if args.days is None else args.days
        if days < 1:
            raise DeskError("--days must be 1 or more")
        return (now - timedelta(days=days)).astimezone(), None
    if args.until is None:
        return local_midnight(args.since), None
    if args.until < args.since:
        raise DeskError("--until is before --since")
    return local_midnight(args.since), local_midnight(args.until + timedelta(days=1))


def minutes_since(at, now):
    try:
        t = datetime.fromisoformat(at)
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:  # no offset stored: assume local time
        t = t.astimezone()
    return max(int((now - t).total_seconds() // 60), 0)


def fmt_minutes(minutes):
    """3 -> '3m', 192 -> '3h12m', 3000 -> '2d2h'."""
    if minutes is None:
        return "?"
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{mins:02d}m"
    return f"{mins}m"


def count_by(con, sql, params):
    """Run 'SELECT key, COUNT(*) ...' grouped by key, biggest first."""
    rows = con.execute(sql + " GROUP BY 1 ORDER BY 2 DESC, 1", params)
    return {("unknown" if key is None else key): n for key, n in rows}


def message_columns(con):
    return {r[1] for r in con.execute("PRAGMA table_info(messages)")}


def reply_filter(columns):
    """SQL for outbound rows that count as a reply. Only DBs upgraded by
    init_db.py have send_kind; older ones hold 1:1 pushes only."""
    return f"{REPLY} AND {NOT_BULK}" if "send_kind" in columns else REPLY


def unreplied_now(con, columns):
    """Count chats waiting for a reply (same rule as list_unreplied.py)."""
    latest = {}  # chat_key -> (direction, source_type) of its latest message
    rows = con.execute(
        f"SELECT {CHAT_KEY}, direction, source_type FROM messages"
        f" WHERE ({reply_filter(columns)})"
        f" OR (direction = 'in' AND {IS_MESSAGE})"
        " ORDER BY at, id")
    for key, direction, source in rows:
        if key:  # segment/broadcast sends (user_id NULL) have no chat key
            latest[key] = (direction, source)
    done = {r[0] for r in con.execute("SELECT chat_key FROM conversations WHERE state = '完了'")}
    counts = {"user": 0, "group_room": 0}
    for key, (direction, source) in latest.items():
        if direction == "in" and key not in done:
            counts["group_room" if source in ("group", "room") else "user"] += 1
    return counts


def build_report(con, since, until, now):
    where = "at >= ?"
    params = [iso(since)]
    if until:
        where += " AND at < ?"
        params.append(iso(until))
    msg_in = f"FROM messages WHERE direction = 'in' AND {IS_MESSAGE} AND {where}"
    columns = message_columns(con)

    by_source = {"user": 0, "group": 0, "room": 0}
    by_source.update(count_by(con, f"SELECT IFNULL(source_type, 'user'), COUNT(*) {msg_in}", params))
    by_type = count_by(con, f"SELECT message_type, COUNT(*) {msg_in}", params)
    chats = con.execute(f"SELECT COUNT(DISTINCT {CHAT_KEY}) {msg_in}", params).fetchone()[0]
    events = count_by(con, "SELECT message_type, COUNT(*) FROM messages"
                      f" WHERE direction = 'in' AND NOT {IS_MESSAGE} AND {where}", params)
    out = count_by(con, "SELECT status, COUNT(*) FROM messages"
                   f" WHERE direction = 'out' AND {where}", params)
    by_kind = {"push": 0, "segment": 0, "broadcast": 0}
    if "send_kind" in columns:
        by_kind.update(count_by(con, "SELECT IFNULL(send_kind, 'push'), COUNT(*) FROM messages"
                                f" WHERE direction = 'out' AND {where}", params))
    else:  # DB not upgraded yet: every outbound row is a 1:1 push
        by_kind["push"] = sum(out.values())
    unreplied = unreplied_now(con, columns)
    latest = con.execute(
        f"SELECT MAX(at) FROM messages WHERE direction = 'in' AND {IS_MESSAGE}").fetchone()[0]
    ago = minutes_since(latest, now) if latest else None

    total = sum(by_type.values())
    text = by_type.get("text", 0)
    return {
        "since": iso(since),
        "until": iso(until) if until else None,
        "inbound_total": total,
        "inbound_by_source_type": by_source,
        "inbound_text": text,
        "inbound_other": total - text,
        "inbound_by_message_type": by_type,
        "inbound_chats": chats,
        "other_events": events,
        "outbound_total": sum(out.values()),
        "outbound_by_status": out,
        "outbound_by_kind": by_kind,
        "unreplied_user": unreplied["user"],
        "unreplied_group_room": unreplied["group_room"],
        "latest_inbound_at": latest,
        "latest_inbound_ago": fmt_minutes(ago) if latest else None,
        "latest_inbound_minutes_ago": ago,
    }


def show(counts):
    return ", ".join(f"{k} {n}" for k, n in counts.items()) or "none"


def print_report(r):
    until = f"{r['until']} (not included)" if r["until"] else "now"
    print(f"since: {r['since']}")
    print(f"until: {until}")
    print(f"inbound messages: {r['inbound_total']}")
    print(f"inbound by source_type: {show(r['inbound_by_source_type'])}")
    print(f"inbound text: {r['inbound_text']}, other: {r['inbound_other']}")
    print(f"inbound by message_type: {show(r['inbound_by_message_type'])}")
    print(f"inbound chats: {r['inbound_chats']}")
    print(f"other inbound events: {show(r['other_events'])}")
    print(f"outbound messages: {r['outbound_total']}")
    print(f"outbound by status: {show(r['outbound_by_status'])}")
    print(f"outbound by kind: {show(r['outbound_by_kind'])}")
    print(f"unreplied now: 1:1 {r['unreplied_user']}, group/room {r['unreplied_group_room']}")
    if r["latest_inbound_at"]:
        print(f"latest inbound message: {r['latest_inbound_at']} ({r['latest_inbound_ago']} ago)")
    else:
        print("latest inbound message: none")


def main(argv):
    p = argparse.ArgumentParser(description="Message counts for the weekly report and inquiry summary.")
    when = p.add_mutually_exclusive_group()
    when.add_argument("--since", type=parse_date, help="first day YYYY-MM-DD (local time)")
    when.add_argument("--days", type=int, help="last N days up to now (default 7)")
    p.add_argument("--until", type=parse_date, help="last day YYYY-MM-DD, included (needs --since)")
    p.add_argument("--json", action="store_true", help="print JSON")
    args = p.parse_args(argv)
    now = datetime.now().astimezone()
    try:
        since, until = get_period(args, now)
        con = open_db()
        report = build_report(con, since, until, now)
        con.close()
    except DeskError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except sqlite3.Error as e:
        print(f"error: database problem: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
