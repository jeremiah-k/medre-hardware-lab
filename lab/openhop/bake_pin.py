#!/usr/bin/env python3
"""Stamp the openHop build provenance into the installed site-packages.

Records, for each installed distribution, which pinned build context it
came from and the version that was actually installed. The build ARGs carry
the exact commit SHAs of the selected checkouts (lab/up.sh exports them
from the checkouts' HEADs), so the stamp plus the installed versions make
the baked source verifiable INSIDE the container.

The metadata is read from the install prefix's dist-info directories
directly: the `--prefix` install tree is not on sys.path in the build
stage, so importlib.metadata cannot see it.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys


def installed_version(site_packages: str, name: str) -> str:
    hits = glob.glob(os.path.join(site_packages, f"{name}-*.dist-info"))
    if len(hits) != 1:
        raise SystemExit(f"expected exactly one {name} dist-info, got {hits}")
    metadata = open(os.path.join(hits[0], "METADATA"), encoding="utf-8").read()
    match = re.search(r"^Version: (.+)$", metadata, re.MULTILINE | re.IGNORECASE)
    if not match:
        raise SystemExit(f"no VERSION in {hits[0]}/METADATA")
    return match.group(1).strip()


def main() -> int:
    prefix = sys.argv[1]
    site_packages = glob.glob(os.path.join(prefix, "lib", "python*", "site-packages"))
    if len(site_packages) != 1:
        raise SystemExit(f"unexpected site-packages layout under {prefix}")
    site_packages = site_packages[0]
    stamp = {
        "openhop_core": {
            "source": "openhop-core-src build context",
            "ref": os.environ["OPENHOP_CORE_REF"],
            "version": installed_version(site_packages, "openhop_core"),
        },
        "openhop_repeater": {
            "source": "openhop-repeater-src build context",
            "ref": os.environ["OPENHOP_REPEATER_REF"],
            "version": installed_version(site_packages, "openhop_repeater"),
        },
    }
    target = os.path.join(site_packages, "openhop-bake-pin.json")
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(stamp, handle, indent=2)
        handle.write("\n")
    print(f"baked openhop from {json.dumps(stamp, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
