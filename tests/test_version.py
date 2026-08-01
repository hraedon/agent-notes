"""Regression tests for `agent-notes --version` (WI-048).

The Plan 020 Linux qualification found that ``--version`` crashed with an
unhandled ``importlib.metadata.PackageNotFoundError`` on every artifact
install: the code asked for distribution ``agent-notes`` but the published
distribution is ``agent-notes-hraedon``. The deployment guide's very first
verification command therefore ended in a traceback.

These tests need no database: ``--version`` must work before any DSN exists,
because it is the command that confirms the CLI is on PATH at all.
"""

from __future__ import annotations

import subprocess
import sys
from importlib.metadata import version as dist_version

from agent_notes.cli import _resolved_version


def test_version_flag_prints_the_installed_version() -> None:
    """The full entry point exits 0 and prints the real distribution version.

    Pre-fix this reproduced the qualification failure exactly, even in a
    source checkout: no environment installs a distribution literally named
    ``agent-notes``, so ``version("agent-notes")`` raised everywhere.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "agent_notes.cli", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert "Traceback" not in proc.stderr
    assert proc.stdout.strip() == dist_version("agent-notes-hraedon")


def test_resolved_version_matches_the_distribution() -> None:
    assert _resolved_version() == dist_version("agent-notes-hraedon")


def test_resolved_version_never_raises_without_metadata(monkeypatch) -> None:
    """No installed metadata degrades to a legible string, not a traceback."""
    import importlib.metadata as im

    def _missing(name: str) -> str:
        raise im.PackageNotFoundError(name)

    monkeypatch.setattr(im, "version", _missing)
    monkeypatch.setattr(im, "packages_distributions", dict)

    out = _resolved_version()
    assert "unknown" in out
    assert "agent_notes" in out
