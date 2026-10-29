"""Harness hook entry point: ``agent-notes-hook <harness> <event>`` (Plan 025 Phase 4).

Kept import-light on purpose. The per-prompt events run on every prompt, so
this package must not import ``agent_notes.cli``, regista, the embedding model
or the connection pool on that path (§6 latency budget).
"""

from __future__ import annotations

import os
import sys

# A harness may export its own interpreter's paths to hooks (Hermes sets PYTHONPATH
# to its 3.14 site-packages), which shadow this venv's compiled dependencies.
_FOREIGN_PYTHON_ENV = ("PYTHONPATH", "PYTHONHOME")


def _foreign_pythonpath() -> bool:
    """PYTHONPATH is set, but this package was not loaded from any of its entries."""
    entries = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    if not entries:
        return False
    package_parent = os.path.normcase(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    return all(os.path.normcase(os.path.abspath(p)) != package_parent for p in entries)


def _rerun_isolated() -> int:
    env = {k: v for k, v in os.environ.items() if k not in _FOREIGN_PYTHON_ENV}
    args = [sys.executable, "-I", "-m", "agent_notes.hooks", *sys.argv[1:]]
    if os.name != "nt":
        os.execve(sys.executable, args, env)
    import subprocess

    return subprocess.run(args, env=env).returncode


def main(argv: list[str] | None = None) -> int:
    if argv is None and _foreign_pythonpath():
        return _rerun_isolated()
    from agent_notes.hooks.common import run

    return run(argv)
