#!/usr/bin/env python3
"""Check a broadcast (一斉送信) draft and print a confirmation for the owner.

Usage: broadcast_prep.py (--text-file PATH | --text -) [--remaining N] [--json]

The draft is read as UTF-8 and used exactly as given (no trimming, no
line-ending change), so the text shown and counted is the text to send.
With --text -, a final line break added by echo or a heredoc is part of it.
Checks:
  - not empty (ignoring spaces and line breaks at both ends)
  - no control characters except line breaks and tabs (they could hide
    part of the text in the confirmation)
  - at most 5000 characters, counted as Python characters and as UTF-16
    code units; both must fit, since LINE's exact counting unit is unverified
  - no unfilled placeholders {KEY} (e.g. {名前}); same rule as canned.py:
    {{ and }} are not placeholders (they are left as they are)
On success prints a Japanese summary for the owner: the full text, who gets
it (all friends), that it uses one message per friend, this month's
remaining messages (--remaining N, e.g. from quota_check.py --peek; 不明 when
not given) and how to confirm. Without --json nothing goes to stdout when a
check fails. --json prints {ok, chars, utf16_units, remaining, placeholders}
instead of the summary, also when a check fails (not when the text cannot
be read); placeholders are the keys without braces. Problems go to stderr;
unfilled placeholders are listed as "unfilled: {名前}, {日付}".

This script only checks text. It uses no network and no database, never
contacts LINE and never sends. The bot sends once, after the owner says 送信.

Exit: 0 ok, 2 bad input (unreadable, not UTF-8, empty, control characters,
      too long; wins over 3), 3 unfilled placeholders.
"""
import argparse
import json
import re
import sys
import unicodedata

MAX_CHARS = 5000
# {{ and }} are literal braces; {KEY} is a placeholder (KEY has no braces).
TOKEN = re.compile(r"\{\{|\}\}|\{([^{}]+)\}")


class Fail(Exception):
    pass


def one_line(text):
    """Line breaks and other control characters become spaces."""
    return "".join(" " if unicodedata.category(c) in ("Cc", "Zl", "Zp") else c
                   for c in str(text))


def placeholders(text):
    keys = []
    for m in TOKEN.finditer(text):
        if m.group(1) is not None and m.group(1) not in keys:
            keys.append(m.group(1))
    return keys


def braced(keys):
    return ", ".join("{%s}" % one_line(k) for k in keys) or "none"


def utf16_units(text):
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def count_arg(value):
    try:
        n = int(value)
    except ValueError:
        n = -1
    if n < 0:
        raise argparse.ArgumentTypeError("must be a whole number, 0 or more")
    return n


def read_text(args):
    """The draft exactly as given: decoded, never trimmed or changed."""
    if args.text_file is not None:
        with open(args.text_file, "rb") as f:
            data = f.read()
    elif args.text == "-":
        data = getattr(sys.stdin, "buffer", sys.stdin).read()
    else:
        raise Fail("--text only takes - (read the text from stdin); "
                   "use --text-file PATH for a file")
    try:
        return data.decode("utf-8") if isinstance(data, bytes) else data
    except UnicodeDecodeError:
        raise Fail("the text is not valid UTF-8")


def bad_input(text, chars, units):
    """Problems that make the draft unusable as it is (exit 2)."""
    problems = []
    if not text.strip():
        problems.append("the text is empty")
    # CR only as part of CRLF: a lone CR can overwrite the shown line.
    if any(c not in "\n\t" and unicodedata.category(c) == "Cc"
           for c in text.replace("\r\n", "\n")):
        problems.append("the text contains control characters "
                        "(only line breaks and tabs are allowed)")
    if chars > MAX_CHARS or units > MAX_CHARS:
        problems.append("the text is too long: %d characters, %d UTF-16 units"
                        " (max %d each)" % (chars, units, MAX_CHARS))
    return problems


def print_summary(text, chars, units, remaining):
    print("一斉送信の確認（まだ送っていません）")
    print("----- 本文ここから -----")
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    print("----- 本文ここまで -----")
    print("文字数：%d（UTF-16単位：%d、上限%d）" % (chars, units, MAX_CHARS))
    print("送る相手：友だち全員（人数は管理画面で確認）")
    print("通数：友だちの人数分を使います")
    print("今月の残り：%s" % ("不明" if remaining is None
                            else "%d通" % remaining))
    print("よければ『送信』と言ってください。1回だけ送ります。")


def run(args):
    text = read_text(args)
    chars, units = len(text), utf16_units(text)
    keys = placeholders(text)
    problems = bad_input(text, chars, units)
    ok = not problems and not keys
    if args.json:
        print(json.dumps({"ok": ok, "chars": chars, "utf16_units": units,
                          "remaining": args.remaining, "placeholders": keys},
                         ensure_ascii=False))
    elif ok:
        print_summary(text, chars, units, args.remaining)
    for problem in problems:
        print("error: %s" % problem, file=sys.stderr)
    if keys:
        print("unfilled: %s" % braced(keys), file=sys.stderr)
    if problems:
        return 2
    return 3 if keys else 0


def main(argv):
    parser = argparse.ArgumentParser(
        description="Check a broadcast (一斉送信) draft and print a "
                    "confirmation for the owner. No network; never sends.")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--text-file", metavar="PATH",
                     help="read the draft from a UTF-8 file")
    src.add_argument("--text", metavar="-",
                     help="give - to read the draft from stdin")
    parser.add_argument("--remaining", type=count_arg, metavar="N",
                        help="messages left this month "
                             "(e.g. from quota_check.py --peek)")
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except Fail as e:
        print("error: %s" % e, file=sys.stderr)
    except OSError as e:
        where = ": " + one_line(e.filename) if e.filename else ""
        print("error: %s%s" % (e.strerror or e, where), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
