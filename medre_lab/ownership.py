"""Cooperative host-wide ownership for physical radio endpoints."""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path


@contextmanager
def own(endpoints):
    root = Path(tempfile.gettempdir()) / f"medre-lab-locks-{os.getuid()}"
    root.mkdir(mode=0o700, exist_ok=True)
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
