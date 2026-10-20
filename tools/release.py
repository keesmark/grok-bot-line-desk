#!/usr/bin/env python3
"""Sign SHA256SUMS for a release, or check the signature.

Usage: tools/release.py [--verify | --no-push]

Without --verify: runs tools/sync.py and, unless SHA256SUMS.asc already
passes the checks below, signs SHA256SUMS with the release key from your own
gpg keyring (gpg asks for the passphrase) into SHA256SUMS.asc, then checks.
When the checks pass it commits SHA256SUMS and SHA256SUMS.asc ("Sign SHA256SUMS
for v<VERSION>") and runs git push, unless --no-push. It stops without
committing when any other file has uncommitted changes.

--verify checks that the key block in skills/desk-update/SKILL.md is the same as
keys/release.asc, that the key's fingerprint is the 「鍵の番号」 in that skill
and appears in both READMEs, and that SHA256SUMS.asc is a good signature
(GOODSIG and VALIDSIG) of SHA256SUMS by that key. Changes nothing. CI runs it.
Exit: 0 ok, 1 a check failed, 2 cannot run (gpg missing, key or skill unreadable).
"""
import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sync

ROOT = sync.ROOT
SKILL = "skills/desk-update/SKILL.md"
KEY = "keys/release.asc"
READMES = ("README.md", "README.en.md")
SIGNED = ("SHA256SUMS", "SHA256SUMS.asc")
KEY_RE = re.compile(r"^-----BEGIN PGP PUBLIC KEY BLOCK-----$.*?^-----END PGP PUBLIC KEY BLOCK-----$",
                    re.M | re.S)
FPR_RE = re.compile(r"^鍵の番号：([0-9A-F]{40})$", re.M)


def gpg(*args, home=None):
    cmd = ["gpg", "--batch"] + (["--homedir", str(home)] if home else []) + list(args)
    return subprocess.run(cmd, capture_output=True, text=True)


def check(root):
    problems = []
    skill = sync.read(root / SKILL)
    key = sync.read(root / KEY)
    block = KEY_RE.search(skill)
    fpr = FPR_RE.search(skill)
    if not block:
        problems.append("%s: no public key block" % SKILL)
    elif block.group(0) + "\n" != key:
        problems.append("%s: key block differs from %s" % (SKILL, KEY))
    if not fpr:
        problems.append("%s: no 「鍵の番号：<40 hex>」 line" % SKILL)
        return problems
    fpr = fpr.group(1)
    for name in READMES:
        if fpr not in sync.read(root / name):
            problems.append("%s: does not mention 鍵の番号 %s" % (name, fpr))
    with tempfile.TemporaryDirectory() as home:
        gpg("--quiet", "--import", str(root / KEY), home=home)
        listed = gpg("--with-colons", "--list-keys", home=home).stdout
        found = [line.split(":")[9] for line in listed.splitlines() if line.startswith("fpr:")]
        if found != [fpr]:
            problems.append("%s: fingerprints %s are not just 鍵の番号 %s" % (KEY, found, fpr))
        status = gpg("--status-fd", "1", "--verify", str(root / "SHA256SUMS.asc"), str(root / "SHA256SUMS"),
                     home=home).stdout
        lines = status.splitlines()
        if not (any(line.startswith("[GNUPG:] GOODSIG ") for line in lines)
                and any(line.startswith("[GNUPG:] VALIDSIG %s " % fpr) for line in lines)):
            problems.append("SHA256SUMS.asc: not a good signature of SHA256SUMS by %s" % fpr)
    return problems


def sign(root):
    if sync.main([], root=root) != 0:
        return False
    if not check(root):
        print("SHA256SUMS.asc is already a good signature; not signing again")
        return True
    fpr = FPR_RE.search(sync.read(root / SKILL))
    if not fpr:
        return False
    signed = subprocess.run(["gpg", "--armor", "--detach-sign", "--yes", "--local-user", fpr.group(1),
                             "--output", str(root / "SHA256SUMS.asc"), str(root / "SHA256SUMS")])
    return signed.returncode == 0


def git(root, *args):
    return subprocess.run(["git", "-C", str(root)] + list(args), capture_output=True, text=True)


def publish(root):
    status = git(root, "status", "--porcelain", "--untracked-files=all")
    if status.returncode != 0:
        print("error: not a git checkout; commit and push SHA256SUMS.asc yourself", file=sys.stderr)
        return False
    changed = sorted({line[3:] for line in status.stdout.splitlines()})
    others = [path for path in changed if path not in SIGNED]
    if others:
        print("error: other files have uncommitted changes (%s); commit them first, then run again"
              % ", ".join(others), file=sys.stderr)
        return False
    if changed:
        version = sync.read(root / "VERSION").strip()
        for step in (["add"], ["commit", "--quiet", "-m", "Sign SHA256SUMS for v%s" % version]):
            done = git(root, *step, "--", *changed)
            if done.returncode != 0:
                print("error: git %s failed: %s" % (step[0], done.stderr.strip()), file=sys.stderr)
                return False
        print("committed %s" % ", ".join(changed))
    pushed = git(root, "push")
    if pushed.returncode != 0:
        print("error: git push failed: %s" % pushed.stderr.strip(), file=sys.stderr)
        return False
    print("pushed")
    return True


def main(argv=None, root=ROOT):
    ap = argparse.ArgumentParser(description="Sign SHA256SUMS for a release, or check the signature.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--verify", action="store_true", help="change nothing; only check the signature")
    mode.add_argument("--no-push", action="store_true", help="sign only; do not commit or push")
    args = ap.parse_args(argv)
    try:
        if not args.verify and not sign(root):
            print("error: signing failed", file=sys.stderr)
            return 1
        problems = check(root)
    except (OSError, UnicodeDecodeError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    for problem in problems:
        print("error: %s" % problem, file=sys.stderr)
    if problems:
        return 1
    print("ok, SHA256SUMS.asc is signed by the release key")
    if args.verify or args.no_push:
        return 0
    try:
        return 0 if publish(root) else 1
    except (OSError, UnicodeDecodeError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
