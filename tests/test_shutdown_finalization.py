"""Regression tests for WI-046: clean interpreter shutdown on Python 3.14.

A successful command must not end with a ``PythonFinalizationError``
traceback. On Python >= 3.14, ``Thread.join()`` raises
``PythonFinalizationError`` during interpreter finalization, so a
``ConnectionPool`` left open for the garbage collector to finalize can emit
``Exception ignored while calling deallocator ... ConnectionPool.__del__ ...
cannot join thread at interpreter shutdown`` on stderr after otherwise
successful work.

The fix registers the public ``close_pool`` (BC-019) with ``atexit`` at pool
creation, so the pool closes *before* the finalization window and
``ConnectionPool.__del__`` never has live workers to join.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from agent_notes.core import db as coredb
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")

# The observer is registered BEFORE the pool exists: atexit runs callbacks
# LIFO, so it executes AFTER the close_pool hook the pool registers at
# creation and can assert the pool really was closed at exit — not merely
# that the traceback happened to be absent (pre-fix, an unlucky GC ordering
# was needed to surface it).
_SHUTDOWN_SCRIPT = """
import atexit
import time

from agent_notes.core import db


def _observe():
    print("POOL_CLOSED" if db._pool is None else "POOL_OPEN", flush=True)


atexit.register(_observe)

db.list_workspaces()  # real work through the pool


# Occupy a worker so it is mid-task at interpreter exit — the in-flight
# shape that made ConnectionPool.__del__ join a live thread at shutdown.
class _Slow:
    def run(self):
        time.sleep(1.0)


db.get_pool().run_task(_Slow())
time.sleep(0.2)
"""


def test_shutdown_is_clean_and_pool_closed_at_exit():
    proc = subprocess.run(
        [sys.executable, "-c", _SHUTDOWN_SCRIPT],
        capture_output=True,
        text=True,
        timeout=60,
        env=os.environ.copy(),
    )
    assert proc.returncode == 0, proc.stderr
    assert "PythonFinalizationError" not in proc.stderr, proc.stderr
    assert proc.stderr == "", f"expected clean stderr, got:\n{proc.stderr}"
    assert "POOL_CLOSED" in proc.stdout, proc.stdout


def test_close_pool_is_registered_once_and_idempotent():
    coredb.close_pool()
    assert coredb._pool is None
    coredb.get_pool()
    assert coredb._close_registered is True
    coredb.close_pool()
    coredb.close_pool()
    assert coredb._pool is None
