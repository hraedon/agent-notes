"""Validation test for data/cli-manifest.json (CLI contract §6).

The manifest is a committed contract artifact: it declares the commands this
CLI exposes, their mutability, and their output framing. This test proves the
manifest is authoritative by validating every declared command path against the
live parser — each must accept ``--help`` with exit 0 (not exit 2, which means
the subcommand doesn't exist). It also detects live commands absent from the
manifest (the "lying by omission" class).

The manifest must not be missing — its absence is a hard failure, not a skip.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "data" / "cli-manifest.json"

_CLI = (sys.executable, "-m", "agent_notes.cli")

# Top-level nouns that are suppressed or internal (not part of the public
# contract surface). These are excluded from the "absent from manifest" check.
_SUPPRESSED_NOUNS = frozenset({"codex-hook"})


@pytest.fixture(scope="module")
def manifest() -> dict:
    # The manifest is a committed contract artifact (CLI contract §6). Its
    # absence is a build error, not a skip — a missing manifest means the
    # component silently dropped its contract declaration.
    assert MANIFEST_PATH.exists(), (
        f"data/cli-manifest.json is missing — the CLI contract §6 manifest is a "
        f"committed artifact and must not be absent. Expected at: {MANIFEST_PATH}"
    )
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _run_help(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*_CLI, *args, "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _get_subcommands(*noun_path: str) -> list[str]:
    """Return the subcommand names for a given noun path, or [] if it's a leaf."""
    proc = _run_help(*noun_path)
    if proc.returncode != 0:
        return []
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and "}" in stripped:
            inner = stripped[stripped.index("{") + 1 : stripped.index("}")]
            return [s.strip() for s in inner.split(",") if s.strip()]
    return []


def test_manifest_schema_is_valid(manifest: dict) -> None:
    """The manifest has the required top-level fields and every command entry
    has the required per-command fields (contract §6 normative schema)."""
    assert manifest["cli_contract_version"] == 1
    assert manifest["manifest_schema_version"] == 1
    assert manifest["component"] == "agent-notes"
    assert isinstance(manifest["commands"], list)
    assert len(manifest["commands"]) > 0

    for cmd in manifest["commands"]:
        assert "name" in cmd, f"command entry missing 'name': {cmd}"
        assert cmd["mutability"] in ("read-only", "mutating"), (
            f"invalid mutability for {cmd['name']!r}: {cmd['mutability']!r}"
        )
        assert isinstance(cmd["json"], bool), f"'json' must be bool for {cmd['name']!r}"
        assert cmd["framing"] in ("document", "ndjson"), (
            f"invalid framing for {cmd['name']!r}: {cmd['framing']!r}"
        )
        # Contract §6 requires 'aliases' on every command entry.
        assert "aliases" in cmd, (
            f"command {cmd['name']!r} missing required 'aliases' field"
        )
        assert isinstance(cmd["aliases"], list), (
            f"'aliases' must be a list for {cmd['name']!r}"
        )


def test_every_manifest_command_accepts_help_with_exit_zero(manifest: dict) -> None:
    """Every command path in the manifest must accept ``--help`` with exit 0.

    Exit 2 means the subcommand doesn't exist in the parser — the manifest
    would be declaring a phantom command. This validates the FULL nested path
    (e.g. ``work-item review pass --help``), not just the top-level noun.
    """
    failures: list[str] = []
    for cmd in manifest["commands"]:
        parts = cmd["name"].split()
        proc = _run_help(*parts)
        if proc.returncode != 0:
            failures.append(
                f"'{' '.join(parts)} --help' exited {proc.returncode} "
                f"(expected 0): {proc.stderr[-200:]!r}"
            )

    assert failures == [], (
        "Manifest declares commands the live parser does not recognize "
        f"({len(failures)} failure(s)):\n" + "\n".join(failures)
    )


def test_live_commands_are_not_absent_from_manifest(manifest: dict) -> None:
    """Detect live parser commands that are absent from the manifest.

    Walks the parser tree (top-level nouns and their subcommands, one level
    deep for nouns with subcommands, two levels for ``work-item review``)
    and checks that every discovered command path appears in the manifest.
    Suppressed/internal nouns are excluded.
    """
    manifest_names = {cmd["name"] for cmd in manifest["commands"]}

    # Discover top-level nouns.
    proc = _run_help()
    top_nouns: list[str] = []
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and "}" in stripped:
            inner = stripped[stripped.index("{") + 1 : stripped.index("}")]
            top_nouns = [s.strip() for s in inner.split(",") if s.strip()]
            break

    missing: list[str] = []
    for noun in top_nouns:
        if noun in _SUPPRESSED_NOUNS:
            continue
        subcmds = _get_subcommands(noun)
        if not subcmds:
            # Leaf noun (e.g. init, resolve, doctor, search, orient, etc.)
            if noun not in manifest_names:
                missing.append(noun)
        else:
            for sub in subcmds:
                full = f"{noun} {sub}"
                # Check for a third level (e.g. work-item review <sub>).
                third = _get_subcommands(noun, sub)
                if third:
                    for t in third:
                        full3 = f"{noun} {sub} {t}"
                        if full3 not in manifest_names:
                            missing.append(full3)
                else:
                    if full not in manifest_names:
                        missing.append(full)

    assert missing == [], (
        f"Live parser commands absent from the manifest ({len(missing)}):\n"
        + "\n".join(f"  {m}" for m in sorted(missing))
    )


def test_manifest_includes_recovery_commands(manifest: dict) -> None:
    """The manifest must declare the memory rebuild-from-regista and
    check-drift commands — these are the operator recovery surface for
    the split-brain contract and must be discoverable."""
    names = {cmd["name"] for cmd in manifest["commands"]}
    assert "memory rebuild-from-regista" in names, (
        "manifest must declare 'memory rebuild-from-regista' (split-brain recovery)"
    )
    assert "memory check-drift" in names, (
        "manifest must declare 'memory check-drift' (drift diagnostic)"
    )


def test_contract_json_emits_committed_manifest(manifest: dict) -> None:
    """``agent-notes contract --json`` must emit exactly the committed manifest.

    This is the normative runtime discovery surface (contract §6): the runtime
    output and the committed artifact must be identical, so a consumer that
    calls the CLI gets the same contract as one that reads the file.
    """
    proc = subprocess.run(
        [*_CLI, "contract", "--json"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, (
        f"'contract --json' exited {proc.returncode}: {proc.stderr[-300:]!r}"
    )
    runtime_manifest = json.loads(proc.stdout)
    assert runtime_manifest == manifest, (
        "Runtime 'contract --json' output diverges from the committed "
        "data/cli-manifest.json. The two must be identical."
    )


def test_manifest_includes_contract_command(manifest: dict) -> None:
    """The manifest must declare the 'contract' command itself — it is part
    of the public CLI surface and must be discoverable."""
    names = {cmd["name"] for cmd in manifest["commands"]}
    assert "contract" in names, (
        "manifest must declare 'contract' (the §6 discovery command itself)"
    )


@pytest.mark.slow
def test_wheel_install_contract_json_equals_committed_manifest(
    manifest: dict, tmp_path: Path
) -> None:
    """Build the wheel and verify the manifest is packaged and loadable.

    Proves:
    1. The wheel contains ``agent_notes/cli-manifest.json`` (hatch force-include).
    2. ``importlib.resources`` can load it from the installed wheel.
    3. The loaded content equals the committed manifest.

    Marked slow because it builds a wheel. Does NOT require runtime deps
    (regista, psycopg, etc.) — it only verifies package data, not the full CLI.
    """
    import zipfile

    # 1. Build the wheel (no isolation — uses the current env's hatchling).
    build_proc = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", "-o", str(tmp_path / "dist")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if build_proc.returncode != 0:
        pytest.skip(f"wheel build failed (build deps missing?): {build_proc.stderr[-300:]}")

    wheels = list((tmp_path / "dist").glob("*.whl"))
    assert wheels, f"no wheel produced in {tmp_path / 'dist'}"
    wheel_path = wheels[0]

    # 2. Verify the manifest is inside the wheel at the expected path.
    with zipfile.ZipFile(wheel_path) as zf:
        names = zf.namelist()
        assert "agent_notes/cli-manifest.json" in names, (
            f"cli-manifest.json not found in wheel. Wheel contents (agent_notes/): "
            f"{[n for n in names if n.startswith('agent_notes/')][:20]}"
        )
        wheel_manifest_text = zf.read("agent_notes/cli-manifest.json").decode("utf-8")

    # 3. Verify the wheel manifest equals the committed manifest.
    wheel_manifest = json.loads(wheel_manifest_text)
    assert wheel_manifest == manifest, (
        "Wheel-packaged cli-manifest.json diverges from the committed "
        "data/cli-manifest.json."
    )

    # 4. Verify importlib.resources can load it from the wheel (simulating
    # a non-editable install). Extract the wheel to a temp dir and load.
    extract_dir = tmp_path / "extracted"
    with zipfile.ZipFile(wheel_path) as zf:
        zf.extractall(extract_dir)

    loaded_text = (extract_dir / "agent_notes" / "cli-manifest.json").read_text(encoding="utf-8")
    loaded = json.loads(loaded_text)
    assert loaded == manifest, (
        "importlib.resources-style load from extracted wheel diverges from committed manifest."
    )
