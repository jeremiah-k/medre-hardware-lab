#!/usr/bin/env python3
"""Keep the generated repeater configs readable by the repeater containers.

The three repeater services run as uid 1000 (compose.yaml pins
``user: "1000:1000"``) and mount their generated config read-only at
``/etc/openhop_repeater/config.yaml``.  The configs carry credentials and
are written mode 0600 for the invoking user, so on a host whose invoking
user has another uid the repeater cannot read its own config and the
bring-up fails without a useful message.

The file's owner may grant a POSIX ACL entry to the container identity
without any privilege and without making the file group- or
world-readable.  ``grant PATH`` does that after a config is written;
``check PATH...`` applies only the readability decision, changing nothing.
When neither route can make the file readable, both refuse and say how
to proceed.
"""

from __future__ import annotations

import argparse
import errno
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

# The identity compose.yaml runs the three repeater services with; keep
# the compose file and this constant in step (a test pins both sides).
REPEATER_UID = 1000


class AccessRefused(Exception):
    """The repeater identity cannot be given read access to a config."""


def stat_readable(st: os.stat_result, uid: int) -> bool:
    """Whether uid can read a file with these stat attributes.

    True when uid owns the file (the generator's 0600 gives the owner
    read), when any other user may read it, or when uid's group does.
    """
    if st.st_uid == uid:
        return bool(st.st_mode & stat.S_IRUSR)
    if st.st_mode & stat.S_IROTH:
        return True
    return st.st_gid == uid and bool(st.st_mode & stat.S_IRGRP)


def acl_readable(path: Path, uid: int) -> bool:
    """Whether a POSIX ACL grants uid an effective read permission."""
    getfacl = shutil.which("getfacl")
    if getfacl is None:
        return False
    result = subprocess.run(
        [getfacl, "-n", "-p", str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        return False
    for line in result.stdout.splitlines():
        if not line.startswith(f"user:{uid}:"):
            continue
        perms = line.split(":", 2)[2]
        if "#effective:" in perms:
            perms = perms.split("#effective:", 1)[1]
        return "r" in perms
    return False


def uid_can_read(path: Path, uid: int = REPEATER_UID) -> bool:
    """Whether the repeater containers can read this config today."""
    try:
        st = path.stat()
    except OSError:
        return False
    if st.st_uid == uid:
        return bool(st.st_mode & stat.S_IRUSR)
    try:
        os.getxattr(path, "system.posix_acl_access")
    except OSError as exc:
        if exc.errno in {errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP}:
            return stat_readable(st, uid) or acl_readable(path, uid)
        return False
    # Extended ACL group bits are the mask, not group:: permissions.
    # Require a positive named-user grant (conservative for other entries).
    return acl_readable(path, uid)


def refusal(path: Path, uid: int = REPEATER_UID) -> str:
    """Why the lab stops instead of starting an unreadable repeater."""
    st = path.stat()
    return (
        f"cannot make {path} readable by the repeater containers: they "
        f"run as uid {uid}, the file is owned by uid {st.st_uid} with "
        f"mode {stat.S_IMODE(st.st_mode):04o}. Give uid {uid} read access "
        f"without making the file group- or world-readable: run the lab "
        f"as uid {uid} (this invocation is uid {os.getuid()}), or provide "
        f"Linux ACL support and the acl tools (setfacl/getfacl)."
    )


def _owner_perms(path: Path) -> str:
    mode = path.stat().st_mode
    return "".join(
        letter if mode & bit else "-"
        for letter, bit in (
            ("r", stat.S_IRUSR),
            ("w", stat.S_IWUSR),
            ("x", stat.S_IXUSR),
        )
    )


def grant(path: Path, uid: int = REPEATER_UID) -> None:
    """Give the repeater identity read access to a written config.

    Called right after the chmod that makes the file private.  A file
    the identity already reads is left untouched; otherwise the private
    base ACL plus the intended named-reader entry is installed and confirmed, or
    AccessRefused states the file, its owner and mode and the remedies.
    """
    if uid_can_read(path, uid):
        return
    setfacl = shutil.which("setfacl")
    if setfacl is None:
        raise AccessRefused(refusal(path, uid))
    result = subprocess.run(
        [
            setfacl,
            "--set",
            f"u::{_owner_perms(path)},u:{uid}:r--,g::---,m::r--,o::---",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        detail = result.stderr.strip()
        message = f"{detail}\n{refusal(path, uid)}" if detail else refusal(path, uid)
        raise AccessRefused(message)
    if not acl_readable(path, uid):
        raise AccessRefused(refusal(path, uid))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    grant_command = commands.add_parser(
        "grant", help="give the repeater identity read access to a config"
    )
    grant_command.add_argument("path", type=Path)
    check_command = commands.add_parser(
        "check", help="apply the readability decision without changing anything"
    )
    check_command.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "grant":
            grant(args.path)
        else:
            for path in args.paths:
                if path.exists() and not uid_can_read(path):
                    raise AccessRefused(refusal(path))
    except (AccessRefused, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
