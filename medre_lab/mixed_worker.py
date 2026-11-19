"""Worker entry point for the mixed-transport continuity case.

Separated from :mod:`medre_lab.worker` so the committed pair dispatch keeps
its exact argv contract. Run as::

    python -m medre_lab.mixed_worker HARDWARE_TOML MATRIX_JSON ROLE RUN \
        mixed-baseline [burst_count]
    python -m medre_lab.mixed_worker HARDWARE_TOML MATRIX_JSON ROLE RUN \
        mixed-reopen

The hardware TOML and the private campaign matrix JSON are the two config
inputs validated by :func:`medre_lab.mixed_continuity.load_mixed`. The CLI
passes `--effective-config SNAPSHOT ROLE RUN PHASE [burst_count]` to both
workers, using one private, normalized and fingerprinted configuration.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) not in (5, 6):
        raise SystemExit(
            "mixed worker requires [hardware-toml matrix-json role run phase "
            "[burst_count]] with phase mixed-baseline or mixed-reopen"
        )
    hardware_toml, matrix_json, role, run, phase = args[:5]
    if phase not in ("mixed-baseline", "mixed-reopen"):
        raise SystemExit(f"unknown mixed phase: {phase}")
    from .mixed_continuity import (
        effective_fingerprint,
        load_mixed,
        run_mixed_baseline,
        run_mixed_reopen,
        verify_controller,
    )

    if role not in ("0", "1") or (phase == "mixed-reopen" and len(args) != 5):
        raise SystemExit("invalid mixed worker role or phase arguments")
    if hardware_toml == "--effective-config":
        snapshot = Path(matrix_json).resolve()
        if snapshot.parent != Path(run).resolve() or snapshot.stat().st_mode & 0o077:
            raise SystemExit(
                "effective configuration must be private and inside the run directory"
            )
        config = json.loads(snapshot.read_bytes())
        if config["matrix"]["fingerprint"] != effective_fingerprint(config):
            raise SystemExit("effective mixed configuration changed")
    else:
        config = load_mixed(Path(hardware_toml), Path(matrix_json))
        config["hardware"]["ble_adapter"] = verify_controller(config["hardware"])
        config["matrix"]["fingerprint"] = effective_fingerprint(config)
    verify_controller(config["hardware"])
    try:
        if phase == "mixed-baseline":
            burst_count = int(args[5]) if len(args) == 6 else 3
            ok = asyncio.run(
                run_mixed_baseline(config, int(role), Path(run), burst_count)
            )
        else:
            ok = asyncio.run(run_mixed_reopen(config, int(role), Path(run)))
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 130
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
