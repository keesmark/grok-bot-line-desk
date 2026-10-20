#!/usr/bin/env python3
"""Sign SHA256SUMS for a release, or check the signature.

Usage: tools/release.py [--verify]

Without --verify: runs tools/sync.py and, unless SHA256SUMS.asc already
passes the checks below, signs SHA256SUMS with the release key from your own
gpg keyring (gpg asks for the passphrase) into SHA256SUMS.asc, then checks.

--verify checks that the key block in skills/update/SKILL.md is the same as
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
SKILL = "skills/update/SKILL.md"
KEY = "keys/release.asc"
READMES = ("README.md", "README.en.md")
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


def main(argv=None, root=ROOT):
    ap = argparse.ArgumentParser(description="Sign SHA256SUMS for a release, or check the signature.")
    ap.add_argument("--verify", action="store_true", help="change nothing; only check the signature")
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
