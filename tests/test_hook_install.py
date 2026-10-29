"""install-harness wiring for the Plan 025 Phase 4 hooks (claude, codex, agy)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_notes.cli.harness import (
    _install_harness_one,
    _json_hash,
    _targets_for,
    _uninstall_one,
)
from agent_notes.codex_lifecycle import canonical_hook_groups
from agent_notes.hooks.agy import HOOK_NAME, agy_hook_entry
from agent_notes.hooks.claude import claude_hook_groups


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("AGENT_NOTES_DSN", "AGENT_NOTES_PROJECT", "CODEX_HOME"):
        monkeypatch.delenv(var, raising=False)


def _install(harness: str, home: Path, dry_run: bool = False) -> dict:
    result, _warns = _install_harness_one(harness, dry_run, None, None, None, home=home)
    return result


def _commands(groups: list) -> list[str]:
    return [h["command"] for g in groups for h in g.get("hooks", [])]


# ---------------------------------------------------------------------------
# claude
# ---------------------------------------------------------------------------


def test_claude_installs_user_hooks_idempotently_and_keeps_user_groups(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    user_group = {"hooks": [{"type": "command", "command": "gitea-orient x"}]}
    settings_path.write_text(
        json.dumps({"model": "m", "hooks": {"SessionStart": [user_group]}}), encoding="utf-8"
    )

    result = _install("claude", tmp_path)
    assert result["status"] == "installed" and result["no_op"] is False
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["model"] == "m"
    assert settings["hooks"]["SessionStart"][0] == user_group
    for event, group in claude_hook_groups().items():
        assert settings["hooks"][event].count(group) == 1
    assert _commands(settings["hooks"]["UserPromptSubmit"]) == [
        "agent-notes-hook claude user-prompt-submit"
    ]
    manifest = json.loads((tmp_path / ".claude" / ".agent-notes-harness.json").read_text())
    assert set(manifest["hooks"]) == {"SessionStart", "UserPromptSubmit", "Stop"}

    again = _install("claude", tmp_path)
    assert again["no_op"] is True
    assert json.loads(settings_path.read_text(encoding="utf-8")) == settings

    removed, _ = _uninstall_one("claude", home=tmp_path)
    assert removed["status"] == "installed"
    after = json.loads(settings_path.read_text(encoding="utf-8"))
    assert after["hooks"] == {"SessionStart": [user_group]}
    assert after["model"] == "m"


def test_claude_dry_run_writes_nothing(tmp_path):
    result = _install("claude", tmp_path, dry_run=True)
    assert any(a.get("keys") == ["hooks.Stop"] for a in result["actions"])
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_claude_modified_owned_hook_is_preserved(tmp_path):
    _install("claude", tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 99
    settings_path.write_text(json.dumps(settings), encoding="utf-8")

    result = _install("claude", tmp_path)
    assert result["status"] == "failed"
    assert json.loads(settings_path.read_text())["hooks"]["Stop"][0]["hooks"][0]["timeout"] == 99

    removed, warns = _uninstall_one("claude", home=tmp_path)
    assert removed["status"] == "failed"
    left = json.loads(settings_path.read_text(encoding="utf-8"))["hooks"]
    assert set(left) == {"Stop"}
    assert any("Stop" in w for w in warns)


def test_claude_warns_about_a_hand_written_orient_hook(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    orient = {"hooks": [{"type": "command", "command": "agent-notes orient --path x"}]}
    settings_path.write_text(json.dumps({"hooks": {"SessionStart": [orient]}}), encoding="utf-8")
    _result, warns = _install_harness_one("claude", False, None, None, None, home=tmp_path)
    assert any("skips its orient summary" in w for w in warns)
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert orient in settings["hooks"]["SessionStart"]


# ---------------------------------------------------------------------------
# codex
# ---------------------------------------------------------------------------


def _legacy_codex_group(event: str) -> dict:
    command = {"SessionStart": "session-start", "Stop": "stop"}[event]
    return {"hooks": [{"type": "command", "command": f"agent-notes codex-hook {command}"}]}


def test_codex_upgrades_owned_legacy_groups_in_place(tmp_path):
    codex = tmp_path / ".codex"
    codex.mkdir()
    legacy = {event: _legacy_codex_group(event) for event in ("SessionStart", "Stop")}
    cairn = {"hooks": [{"type": "command", "command": "cairn _codex_hook Stop"}]}
    (codex / "hooks.json").write_text(
        json.dumps(
            {"hooks": {"SessionStart": [legacy["SessionStart"]], "Stop": [cairn, legacy["Stop"]]}}
        ),
        encoding="utf-8",
    )
    (codex / ".agent-notes-harness.json").write_text(
        json.dumps(
            {
                "tool": "agent-notes",
                "harness": "codex",
                "env_keys": [],
                "skills": {},
                "plugin": False,
                "hooks": {e: _json_hash(g) for e, g in legacy.items()},
                "hooks_file_created": False,
            }
        ),
        encoding="utf-8",
    )

    result = _install("codex", tmp_path)
    assert result["status"] == "installed", result["warnings"]
    hooks = json.loads((codex / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    canonical = canonical_hook_groups()
    assert hooks["SessionStart"] == [canonical["SessionStart"]]
    assert hooks["Stop"] == [cairn, canonical["Stop"]]
    assert hooks["UserPromptSubmit"] == [canonical["UserPromptSubmit"]]

    _uninstall_one("codex", home=tmp_path)
    hooks = json.loads((codex / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert hooks == {"Stop": [cairn]}


# ---------------------------------------------------------------------------
# agy
# ---------------------------------------------------------------------------


def test_agy_merges_named_hook_and_skills_and_uninstalls(tmp_path):
    config = tmp_path / ".gemini" / "config"
    config.mkdir(parents=True)
    other = {"Stop": [{"type": "command", "command": "./mine.sh"}]}
    (config / "hooks.json").write_text(json.dumps({"mine": other}), encoding="utf-8")

    result = _install("agy", tmp_path)
    assert result["status"] == "installed", result["warnings"]
    document = json.loads((config / "hooks.json").read_text(encoding="utf-8"))
    assert document == {"mine": other, HOOK_NAME: agy_hook_entry()}
    assert (config / "skills" / "agent-messages" / "SKILL.md").is_file()
    assert (config / "skills" / "start" / "SKILL.md").is_file()

    assert _install("agy", tmp_path)["no_op"] is True

    removed, _ = _uninstall_one("agy", home=tmp_path)
    assert removed["status"] == "installed"
    assert json.loads((config / "hooks.json").read_text(encoding="utf-8")) == {"mine": other}
    assert not (config / "skills" / "start").exists()
    assert not (config / ".agent-notes-harness.json").exists()


def test_agy_creates_and_removes_its_own_hooks_file(tmp_path):
    _install("agy", tmp_path)
    hooks_path = tmp_path / ".gemini" / "config" / "hooks.json"
    commands = [
        h["command"]
        for handlers in json.loads(hooks_path.read_text())[HOOK_NAME].values()
        for h in handlers
    ]
    assert commands == [
        "agent-notes-hook agy session-start",
        "agent-notes-hook agy pre-invocation",
        "agent-notes-hook agy stop",
    ]
    _uninstall_one("agy", home=tmp_path)
    assert not hooks_path.exists()


def test_agy_does_not_adopt_or_clobber_a_foreign_entry(tmp_path):
    config = tmp_path / ".gemini" / "config"
    config.mkdir(parents=True)
    foreign = {"SessionStart": [{"command": "something else"}]}
    (config / "hooks.json").write_text(json.dumps({HOOK_NAME: foreign}), encoding="utf-8")
    result = _install("agy", tmp_path)
    assert result["status"] == "failed"
    assert json.loads((config / "hooks.json").read_text()) == {HOOK_NAME: foreign}
    _uninstall_one("agy", home=tmp_path)
    assert json.loads((config / "hooks.json").read_text()) == {HOOK_NAME: foreign}


def test_agy_modified_owned_hook_is_preserved(tmp_path):
    _install("agy", tmp_path)
    hooks_path = tmp_path / ".gemini" / "config" / "hooks.json"
    document = json.loads(hooks_path.read_text())
    document[HOOK_NAME]["Stop"][0]["timeout"] = 5
    hooks_path.write_text(json.dumps(document), encoding="utf-8")
    assert _install("agy", tmp_path)["status"] == "failed"
    removed, _ = _uninstall_one("agy", home=tmp_path)
    assert removed["status"] == "failed"
    assert json.loads(hooks_path.read_text())[HOOK_NAME]["Stop"][0]["timeout"] == 5


# ---------------------------------------------------------------------------
# all
# ---------------------------------------------------------------------------


def test_all_adds_codex_and_agy_only_where_installed(tmp_path):
    assert _targets_for("all", tmp_path) == ["claude", "opencode"]
    (tmp_path / ".codex").mkdir()
    assert _targets_for("all", tmp_path) == ["claude", "opencode", "codex"]
    (tmp_path / ".gemini").mkdir()
    assert _targets_for("all", tmp_path) == ["claude", "opencode", "codex", "agy"]
    assert _targets_for("agy", tmp_path) == ["agy"]
