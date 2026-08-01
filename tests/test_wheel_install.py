"""Wheel-install regression tests for the schema migrations (WI-047).

The Plan 020 Linux qualification found that the built wheel shipped **zero**
``schema/*.sql`` files and that ``agent-notes-migrate`` located them by walking
``Path(__file__).parents`` looking for a ``schema/`` directory — a shape that can
only ever succeed inside a source checkout. On an artifact-only host the
projection database therefore could not be migrated at all, and there was no
supported way to fix it.

Editable-source coverage cannot catch that class of defect: in a source checkout
the parent walk finds the repository-root ``schema/`` and every existing test
passes. These tests therefore do the one thing that distinguishes the two cases —
they **build the wheel, install it where no source tree is reachable, and run the
entry point** — rather than asserting anything about the working tree.

Deliberately not marked ``slow``: ``addopts = -m 'not slow'`` means a ``slow``
test does not run in the default suite, and a gate that does not run is not a
gate. The wheel build is hatchling-only and takes about a second.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_SCHEMA_DIR = REPO_ROOT / "schema"

# Where force-include places the schema inside the wheel; the same anchor
# ``agent_notes.scripts.migrate`` resolves through importlib.resources.
WHEEL_SCHEMA_PREFIX = "agent_notes/schema/"

# Console scripts land in ``bin/`` on POSIX and ``Scripts/`` on Windows.
# Resolved from the interpreter's own install scheme rather than hardcoded, so
# the tests follow whichever layout the venv actually uses (WI-055).
_SCRIPT_DIR_NAME = "Scripts" if os.name == "nt" else "bin"


def _venv_script_dir(venv: Path) -> Path:
    """Return the directory holding the venv's console scripts.

    A fresh venv inherits the running interpreter's install scheme, so query it
    via ``sysconfig`` under that venv's own interpreter — the authoritative
    source that agrees with where ``pip`` / ``venv`` actually place scripts on
    every platform. Falls back to the conventional layout if the query fails.
    """
    probe = subprocess.run(
        [str(_venv_python(venv)), "-c", "import sysconfig; print(sysconfig.get_path('scripts'))"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode == 0 and probe.stdout.strip():
        return Path(probe.stdout.strip())
    return venv / _SCRIPT_DIR_NAME


def _venv_python(venv: Path) -> Path:
    """Return the venv's interpreter path (``Scripts`` on Windows, ``bin`` else)."""
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _source_sql_names() -> list[str]:
    names = sorted(p.name for p in SOURCE_SCHEMA_DIR.glob("*.sql"))
    assert names, f"no .sql files in {SOURCE_SCHEMA_DIR} — fixture is wrong, not the code"
    return names


def _build_wheel(tmp_path: Path) -> Path:
    """Build the agent-notes wheel into *tmp_path* and return its path.

    ``--no-isolation`` uses the already-installed build backend (``build`` and
    ``hatchling`` are test dependencies), so this needs no network.
    """
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    proc = subprocess.run(
        [
            sys.executable, "-m", "build", "--wheel", "--no-isolation",
            "--outdir", str(wheel_dir), str(REPO_ROOT),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"wheel build failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
    wheels = list(wheel_dir.glob("agent_notes_hraedon-*.whl"))
    assert wheels, "no agent-notes wheel built"
    return wheels[0]


def _install_wheel(wheel: Path, venv: Path) -> Path:
    """Create a venv at *venv*, install *wheel* with no dependencies, return its script dir."""
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    proc = subprocess.run(
        [str(_venv_python(venv)), "-m", "pip", "install", "--no-deps", str(wheel)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"wheel install failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
    return _venv_script_dir(venv)


def test_wheel_ships_every_schema_sql_file(tmp_path: Path) -> None:
    """Every ``schema/*.sql`` in the source tree is present inside the wheel.

    The qualification measured 0 of 14. Asserting set equality (not "more than
    zero") means a newly added migration that is not packaged also fails here.
    """
    wheel = _build_wheel(tmp_path)
    with zipfile.ZipFile(wheel) as zf:
        packaged = sorted(
            name[len(WHEEL_SCHEMA_PREFIX):]
            for name in zf.namelist()
            if name.startswith(WHEEL_SCHEMA_PREFIX) and name.endswith(".sql")
        )
    assert packaged == _source_sql_names(), (
        "wheel schema contents differ from schema/ in the source tree; "
        "check [tool.hatch.build.targets.wheel.force-include] in pyproject.toml"
    )


def test_wheel_install_migrate_resolves_schema_with_no_source_tree(tmp_path: Path) -> None:
    """``agent-notes-migrate`` finds the schema from the installed package alone.

    The venv lives under ``tmp_path`` and the subprocess runs with that as its
    working directory, so there is no ``schema/`` directory anywhere on the path
    from the installed module up to ``/``. Pre-fix this died with
    ``FileNotFoundError: Could not locate schema/ directory``; the parent-walk
    resolver cannot pass this test even if the wheel does ship the files.
    """
    wheel = _build_wheel(tmp_path)
    venv = tmp_path / "venv"
    bindir = _install_wheel(wheel, venv)

    proc = subprocess.run(
        [str(bindir / "agent-notes-migrate"), "--list", "--json"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        check=False,
    )
    assert proc.returncode == 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"

    payload = json.loads(proc.stdout)
    assert [f["name"] for f in payload["files"]] == _source_sql_names()

    # Resolved from inside the installed package, not from a source checkout.
    # Anchor on the venv root (platform-independent) rather than a hardcoded
    # ``lib/`` prefix, which does not exist on Windows (WI-055).
    origin = Path(payload["origin"])
    assert venv in origin.parents, (
        f"schema resolved from {origin}, which is outside the installed venv "
        f"{venv} — the resolver is still reaching for a source tree"
    )
    assert REPO_ROOT not in origin.parents and origin != SOURCE_SCHEMA_DIR

    # The bytes actually made it, not just the names.
    for entry in payload["files"]:
        assert entry["bytes"] == (SOURCE_SCHEMA_DIR / entry["name"]).stat().st_size


def test_migrate_single_file_does_not_require_the_schema_directory(tmp_path: Path) -> None:
    """``--file`` works standalone (WI-047: the resolver ran unconditionally).

    ``main()`` used to call ``_find_schema_dir()`` before dispatching, so the
    single-file escape hatch was dead on exactly the hosts that needed it. Run
    from a venv with no source tree reachable and point ``--file`` at a scratch
    SQL file: the failure must come from the database, never from schema
    resolution.
    """
    wheel = _build_wheel(tmp_path)
    venv = tmp_path / "venv"
    bindir = _install_wheel(wheel, venv)
    scratch = tmp_path / "001_scratch.sql"
    scratch.write_text("SELECT 1;\n", encoding="utf-8")

    proc = subprocess.run(
        [str(bindir / "agent-notes-migrate"), "--file", str(scratch)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env={"AGENT_NOTES_DSN": "postgresql://127.0.0.1:1/nonexistent", "PATH": "/usr/bin:/bin"},
        check=False,
    )
    combined = proc.stdout + proc.stderr
    assert "Could not locate" not in combined, combined
    assert "schema/ directory" not in combined, combined
