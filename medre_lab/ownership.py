"""Cooperative host-wide ownership for physical radio endpoints."""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path


def _lock_root() -> Path:
    root = Path(tempfile.gettempdir()) / f"medre-lab-locks-{os.getuid()}"
    root.mkdir(mode=0o700, exist_ok=True)
    return root


@contextmanager
def own(endpoints):
    root = _lock_root()
    with ExitStack() as stack:
        for endpoint in sorted(
            {
                str(Path(e).resolve()) if e.startswith("/") else e.upper()
                for e in endpoints
            }
        ):
            name = hashlib.sha256(endpoint.encode()).hexdigest()
            stream = stack.enter_context((root / name).open("a"))
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("another lab run owns a selected radio") from exc
        yield


@contextmanager
def own_run(run: Path):
    """Own one continuity run directory for the lifetime of its MEDRE owner.

    The baseline phase holds this exclusive lock for its whole process
    lifetime; the reopen phase acquires it only after the baseline worker
    has exited, so a reopen cannot start while a previous MEDRE owner is
    still alive. The lock file itself lives inside the private run
    directory and is never committed.
    """
    run.mkdir(parents=True, mode=0o700, exist_ok=True)
    stream = (run / ".owner.lock").open("a")
    try:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "continuity run already has a live MEDRE owner; reopen is "
                "only permitted after the first owner retires"
            ) from exc
        yield
    finally:
        stream.close()
