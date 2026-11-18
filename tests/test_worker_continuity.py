"""Offline ownership tests for continuity run directories.

No radio, SDK, or MEDRE installation is required.
"""

import threading

import pytest

from medre_lab import ownership


def test_own_run_blocks_while_first_owner_alive(tmp_path):
    acquired = threading.Event()
    release = threading.Event()

    def hold():
        with ownership.own_run(tmp_path):
            acquired.set()
            release.wait(5)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert acquired.wait(5)
    with pytest.raises(RuntimeError, match="only permitted after the first owner"):
        with ownership.own_run(tmp_path):
            pass  # pragma: no cover - must not be reached
    release.set()
    thread.join(5)
    # After the first owner retires the lock is available again.
    with ownership.own_run(tmp_path):
        pass
