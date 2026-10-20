#!/usr/bin/env python3
"""Bring the release files in line with VERSION.

Usage: tools/sync.py [--check]

Rewrites SHA256SUMS from scripts/*.py, the 「版：Desk for LINE v…」 line of
every skills/*/SKILL.md and routines/*.md, and the release tag in the
download URLs of README.md, README.en.md and skills/getting-started/SKILL.md.
CHANGELOG.md and MIGRATIONS.md must already have a "## v<VERSION>" section.
Prints the tag and the SHA256SUMS digest that go in the template memory
(「Desk for LINE の版」 and 「照合値」).

--check changes nothing and exits 1 when a file is out of date.
Exit: 0 ok, 1 out of date or a section/label is missing, 2 cannot read.
"""
import argparse
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
LABEL_RE = re.compile(r"(版：Desk for LINE )v\d+(?:\.\d+)*")
TAG_RE = re.compile(r"(/archive/refs/tags/)v\d+(?:\.\d+)*(?=\.tar\.gz)")
TAGGED = ("README.md", "README.en.md", "skills/getting-started/SKILL.md")
SECTIONED = ("CHANGELOG.md", "MIGRATIONS.md")


class Fail(Exception):
    pass


def read(path):
    return path.read_bytes().decode("utf-8")


def sums(root):
    return "".join("%s  %s\n" % (hashlib.sha256(p.read_bytes()).hexdigest(), p.name)
                   for p in sorted((root / "scripts").glob("*.py")))


def plan(root):
    version = read(root / "VERSION").strip()
    if not VERSION_RE.match(version):
        raise Fail("VERSION must look like 1.2.3, got %r" % version)
    tag = "v" + version
    wanted = {root / "SHA256SUMS": sums(root)}
    problems = []
    labelled = sorted(root.glob("skills/*/SKILL.md")) + sorted(root.glob("routines/*.md"))
    for path in labelled:
        text = read(path)
        if not LABEL_RE.search(text):
            problems.append("%s: no 「版：Desk for LINE v…」 line" % path.relative_to(root))
        wanted[path] = LABEL_RE.sub(r"\g<1>" + tag, text)
    for name in TAGGED:
        path = root / name
        text = wanted[path] if path in wanted else read(path)
        if not TAG_RE.search(text):
            problems.append("%s: no /archive/refs/tags/v….tar.gz URL" % name)
        wanted[path] = TAG_RE.sub(r"\g<1>" + tag, text)
    for name in SECTIONED:
        if not re.search(r"^## %s(?![\d.])" % re.escape(tag), read(root / name), re.M):
            problems.append("%s: no \"## %s\" section" % (name, tag))
    return tag, wanted, problems


def main(argv=None, root=ROOT):
    ap = argparse.ArgumentParser(description="Bring the release files in line with VERSION.")
    ap.add_argument("--check", action="store_true", help="change nothing; exit 1 when out of date")
    args = ap.parse_args(argv)
    try:
        tag, wanted, problems = plan(root)
        stale = [p for p, text in wanted.items() if not p.exists() or read(p) != text]
        for path in stale:
            if args.check:
                problems.append("%s: out of date (run tools/sync.py)" % path.relative_to(root))
            else:
                path.write_bytes(wanted[path].encode("utf-8"))
                print("updated %s" % path.relative_to(root))
    except (Fail, OSError, UnicodeDecodeError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    for problem in problems:
        print("error: %s" % problem, file=sys.stderr)
    print("tag: %s" % tag)
    print("SHA256SUMS sha256: %s" % hashlib.sha256(wanted[root / "SHA256SUMS"].encode("utf-8")).hexdigest())
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
