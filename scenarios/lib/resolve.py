"""Resolve room aliases to room ids for scenario scripts.

Usage: python3 resolve.py <alias-localpart> [<alias-localpart> ...]
Prints one NAME=ROOM_ID line per argument using the argument's basename:
    lab-bridge=!abc:lab.test
Unresolvable aliases print NAME= (empty value) instead of failing, so
profile-gated or rig-optional rooms degrade to scenario SKIPs.
"""

from __future__ import annotations

import sys

from lab_env import room_id


def main() -> int:
    for localpart in sys.argv[1:]:
        try:
            print(f"{localpart}={room_id(localpart)}")
        except SystemExit:
            print(f"{localpart}=")
    return 0


if __name__ == "__main__":
    sys.exit(main())
