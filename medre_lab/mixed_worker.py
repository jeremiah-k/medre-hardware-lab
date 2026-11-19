"""Worker entry point for the mixed-transport continuity case.

Separated from :mod:`medre_lab.worker` so the committed pair dispatch keeps
its exact argv contract. Run as::

    python -m medre_lab.mixed_worker HARDWARE_TOML MATRIX_JSON ROLE RUN \
        mixed-baseline [burst_count]
    python -m medre_lab.mixed_worker HARDWARE_TOML MATRIX_JSON ROLE RUN \
        mixed-reopen

The hardware TOML and the private campaign matrix JSON are the two config
inputs validated by :func:`medre_lab.mixed_continuity.load_mixed`; both bind
the phases together through their bytes fingerprint.
"""

from __future__ import annotations

import asyncio
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
    from .mixed_continuity import load_mixed, run_mixed_baseline, run_mixed_reopen

    config = load_mixed(Path(hardware_toml), Path(matrix_json))
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
