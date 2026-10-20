#!/usr/bin/env python3
"""Check the LINE monthly message quota and whether the owner should be told.

Usage: quota_check.py [--json] [--state PATH] [--no-update | --peek]

Reads GET /v2/bot/message/quota and /v2/bot/message/quota/consumption.
A notice is due when remaining first reaches 50, then 20, 15 and 10, and
then at each of 5, 4, 3, 2, 1, 0. The state file remembers the remaining
count at the last notice this month, so a threshold that was jumped over
between two checks still gives one notice.

  --json        print JSON instead of plain lines
  --state PATH  state file (default: <DESK_DB dir>/quota_state.json, mode 0600)
  --no-update   do not write the state file (peek only)
  --peek        same as --no-update: read only, the state file is not written.
                Use it when you only need the remaining count (e.g. before a
                broadcast), so no notice is used up.

Env: LINE_CHANNEL_ACCESS_TOKEN  required, never printed
     DESK_DB                    required unless --state is given
                                (this bot's own DB path; no default)
Exits 0 even when a notice is due; non-zero only on errors.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

NO_DB = ("DESK_DB is not set: give this bot's own database path, e.g. "
         "DESK_DB=/workspace/line-inbox/<agent id>/messages.db "
         "(see 保存先 in memory.md); not using a shared default")
API = "https://api.line.me"
THRESHOLDS = [50, 20, 15, 10, 5, 4, 3, 2, 1, 0]


class Fail(Exception):
    pass


def clip(text, secret):
    """One line, secret removed, at most 200 chars."""
    text = str(text)
    if secret:
        text = text.replace(secret, "***")
    return " ".join(text.split())[:200]


def is_count(value):
    return isinstance(value, int) and not isinstance(value, bool)


def retry_once(fn):
    """Run fn(); on a connection error, wait 2 seconds and run it once more.

    HTTP errors (any status) are never retried.
    """
    try:
        return fn()
    except urllib.error.HTTPError:
        raise
    except OSError:  # URLError, socket timeout, ConnectionError
        time.sleep(2)
        return fn()


def api_get(path, token):
    req = urllib.request.Request(API + path,
                                 headers={"Authorization": "Bearer " + token})

    def fetch():
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read()

    try:
        body = retry_once(fetch)
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            detail = ""
        raise Fail("GET %s returned HTTP %s: %s"
                   % (path, e.code, clip(detail, token)))
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise Fail("could not reach api.line.me (%s)" % clip(reason, token))
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError:
        raise Fail("GET %s returned invalid JSON" % path)
    if not isinstance(data, dict):
        raise Fail("GET %s returned unexpected data" % path)
    return data


def decide(remaining, last):
    """Return (notice_due, reason). last = remaining at last notice, or None."""
    crossed = [t for t in THRESHOLDS
               if remaining <= t and (last is None or t < last)]
    if crossed:
        return True, "crossed %d" % min(crossed)
    later = [t for t in THRESHOLDS if last is None or t < last]
    if later:
        return False, "no new threshold crossed; next notice at %d" % max(later)
    return False, "no new threshold crossed; no more notices this month"


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        print("warning: state file %s unreadable; starting fresh" % path,
              file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path, state):
    tmp = path + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(state, f)
            f.write("\n")
        os.replace(tmp, path)
    except OSError as e:
        raise Fail("cannot write state file %s (%s)" % (path, e.strerror or e))


def check(state_path, update):
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not token:
        raise Fail("LINE_CHANNEL_ACCESS_TOKEN is not set")
    state_dir = os.path.dirname(os.path.abspath(state_path))
    if update and not os.path.isdir(state_dir):
        raise Fail("folder %s does not exist (run init_db.py first, "
                   "or pass --state)" % state_dir)

    quota = api_get("/v2/bot/message/quota", token)
    usage = api_get("/v2/bot/message/quota/consumption", token)
    used = usage.get("totalUsage")
    if not is_count(used):
        raise Fail("unexpected consumption response (no totalUsage)")

    qtype = quota.get("type")
    result = {"type": qtype, "limit": None, "used": used, "remaining": None,
              "notice_due": False, "reason": "no monthly limit",
              "state_file": state_path, "state_saved": False}
    if qtype == "none":
        return result
    limit = quota.get("value")
    if qtype != "limited" or not is_count(limit):
        raise Fail("unexpected quota response from LINE")

    remaining = max(limit - used, 0)
    month = datetime.now().astimezone().strftime("%Y-%m")
    state = load_state(state_path)
    last = state.get("last_noticed_remaining")
    # New month, no valid record, or more left than at the last notice
    # (plan upgraded): start over.
    if state.get("month") != month or not is_count(last) or remaining > last:
        last = None
    due, reason = decide(remaining, last)

    new_state = {"month": month,
                 "last_noticed_remaining": remaining if due else last}
    if update and new_state != state:
        save_state(state_path, new_state)
        result["state_saved"] = True
    result.update(limit=limit, remaining=remaining, notice_due=due,
                  reason=reason)
    return result


def main(argv):
    parser = argparse.ArgumentParser(
        description="Check the LINE monthly message quota.")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--state", metavar="PATH",
                        help="state file (default: <DESK_DB dir>/quota_state.json)")
    # --peek is an alias: read only, no state write.
    parser.add_argument("--no-update", "--peek", dest="no_update",
                        action="store_true",
                        help="do not write the state file (read only)")
    args = parser.parse_args(argv)

    db = os.environ.get("DESK_DB", "")
    if not args.state and not db.strip():
        print("error: %s" % NO_DB, file=sys.stderr)
        return 1
    state_path = args.state or os.path.join(
        os.path.dirname(os.path.abspath(db)), "quota_state.json")
    try:
        r = check(state_path, not args.no_update)
    except Fail as e:
        print("error: %s" % e, file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(r, ensure_ascii=False))
        return 0
    if r["type"] == "none":
        print("limit: none (no monthly limit)")
        print("used: %d" % r["used"])
        print("remaining: unlimited")
    else:
        print("limit: %d" % r["limit"])
        print("used: %d" % r["used"])
        print("remaining: %d" % r["remaining"])
    print("notice_due: %s" % ("yes" if r["notice_due"] else "no"))
    print("reason: %s" % r["reason"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
