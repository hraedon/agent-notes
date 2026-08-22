"""Codex plugin packaging checks (suite Plan 007 WI-0.1)."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from agent_notes.codex_lifecycle import codex_hook_document

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "agent-notes"
MANIFEST = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"


def test_component_owned_plugin_bundles_exact_canonical_skills() -> None:
    """The small distributable bundle must remain byte-equal to canonical skills."""
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert payload["name"] == "agent-notes"
    assert payload["version"] == "1.1.0"
    assert payload["skills"] == "./skills/"
    assert "hooks" not in payload  # Codex discovers the default hooks/hooks.json

    skills_root = (PLUGIN_ROOT / payload["skills"]).resolve()
    assert sorted(path.parent.name for path in skills_root.glob("*/SKILL.md")) == [
        "add-memory",
        "adversarial-review",
        "end",
        "file-breadcrumb",
        "find-breadcrumb",
        "reflect",
        "start",
        "update-breadcrumb",
    ]
    for bundled in skills_root.glob("*/SKILL.md"):
        canonical = REPO_ROOT / "skills" / bundled.parent.name / "SKILL.md"
        assert bundled.read_bytes() == canonical.read_bytes()


def test_component_owned_plugin_has_no_secret_or_runtime_config() -> None:
    """Distribution metadata must not smuggle suite configuration into Codex."""
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert "mcpServers" not in payload
    assert "apps" not in payload
    hook_path = PLUGIN_ROOT / "hooks" / "hooks.json"
    assert json.loads(hook_path.read_text(encoding="utf-8")) == codex_hook_document()
    serialized = json.dumps(payload).upper()
    for forbidden in ("DSN", "TOKEN", "PASSWORD", "SECRET", "KEY_PATH"):
        assert forbidden not in serialized


def test_wheel_force_includes_the_canonical_skill_tree() -> None:
    """Keep the built-wheel resource mapping pinned to the canonical source."""
    with (REPO_ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)

    force_include = project["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert force_include == {
        "skills": "agent_notes/skills",
        "data/cli-manifest.json": "agent_notes/cli-manifest.json",
        # WI-047: the migration DDL must reach an artifact-only host. Asserted
        # by contents, not just by mapping, in tests/test_wheel_install.py.
        "schema": "agent_notes/schema",
    }


def test_plugin_bundle_excludes_checkout_and_virtualenv() -> None:
    """Regression guard for the repo-root 5 GiB plugin packaging failure."""
    bundled_files = [path for path in PLUGIN_ROOT.rglob("*") if path.is_file()]
    assert not (REPO_ROOT / ".codex-plugin" / "plugin.json").exists()
    assert all(".venv" not in path.parts and ".git" not in path.parts for path in bundled_files)
    assert sum(path.stat().st_size for path in bundled_files) < 1_000_000
