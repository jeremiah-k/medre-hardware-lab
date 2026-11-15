"""Print single values from the scenario environment (for bash callers).

Usage: python3 lab_env_probe.py <field>
Fields: untrusted_token, driver_token, bot_a, bot_b, bot_c, meshnet_a,
meshnet_b, map_db_a, map_db_b, map_db_c, plugin_obs_a, plugin_obs_b.
"""

from __future__ import annotations

import sys

from lab_env import (
    BOT_A_MXID,
    BOT_B_MXID,
    BOT_C_MXID,
    MAP_DB_A,
    MAP_DB_B,
    MAP_DB_C,
    MESHNET_A,
    MESHNET_B,
    PLUGIN_OBS_A,
    PLUGIN_OBS_B,
    driver_token,
    untrusted_token,
)

FIELDS = {
    "untrusted_token": untrusted_token,
    "driver_token": driver_token,
    "bot_a": lambda: BOT_A_MXID,
    "bot_b": lambda: BOT_B_MXID,
    "bot_c": lambda: BOT_C_MXID,
    "meshnet_a": lambda: MESHNET_A,
    "meshnet_b": lambda: MESHNET_B,
    "map_db_a": lambda: MAP_DB_A,
    "map_db_b": lambda: MAP_DB_B,
    "map_db_c": lambda: MAP_DB_C,
    "plugin_obs_a": lambda: PLUGIN_OBS_A,
    "plugin_obs_b": lambda: PLUGIN_OBS_B,
}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in FIELDS:
        print(f"usage: lab_env_probe.py <{'|'.join(sorted(FIELDS))}>", file=sys.stderr)
        return 2
    print(FIELDS[sys.argv[1]]())
    return 0


if __name__ == "__main__":
    sys.exit(main())
