"""Unit tests for ``agent-notes install-harness`` (Plan 017 WI-2.1).

No database required — these test the harness config-merge / skill-install /
uninstall wiring against temp home directories. Mirrors the install-skills test
style (subprocess against the real CLI) but redirects *all* paths (home, source,
dest) so nothing touches the operator's real config.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from agent_notes.core.config import config_path

_CLI = [sys.executable, "-m", "agent_notes.cli"]

_SUITE_ENV = {
    "REGISTA_DSN": "postgresql://suite/x",
    "REGISTA_KEY_PATH": "/suite/key",
    "REGISTA_REQUIRE_SSL": "true",
    "AGENT_NOTES_DSN": "postgresql://native/x",
    "AGENT_NOTES_REGISTA_WRITES": "1",
}

_DISCOVERY_PINS = (
    "AGENT_NOTES_CONFIG",
    "AGENT_SUITE_CONFIG",
    "AGENT_SUITE_SYSTEM_CONFIG",
)


def _build_test_env(env: dict | None = None) -> dict[str, str]:
    # Strip inherited agent-notes/regista env so tests control their own inputs,
    # but carry through the config-discovery pins conftest establishes (WI-059):
    # without AGENT_NOTES_CONFIG the install-harness subprocess can read the
    # operator's real config and lose hermetic isolation. Apply the pins last so
    # they always win, mirroring tests/cli_harness.build_cli_env.
    clean = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("AGENT_NOTES_") and not k.startswith("REGISTA_")
    }
    merged = {**clean, **(env or {})}
    for pin in _DISCOVERY_PINS:
        value = os.environ.get(pin)
        if value is not None:
            merged[pin] = value
    return merged


def _run(*args: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        _CLI + list(args),
        capture_output=True,
        text=True,
        env=_build_test_env(env),
        check=check,
    )


def _make_skill_tree(td: Path, name: str = "demo") -> Path:
    src = td / "skills"
    d = src / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: x\n---\nbody\n")
    return src


def _make_opencode_agents(td: Path, *extra: str) -> Path:
    """Create a stub .opencode/agents/ tree alongside the skills source.

    ``install-harness --source <skills-root>`` resolves the repo root as
    ``<skills-root>.parent``, so agents live at ``td/.opencode/agents/``.
    """
    agents_dir = td / ".opencode" / "agents"
    agents_dir.mkdir(parents=True)
    for filename in ("glm.md", "kimi.md", *extra):
        (agents_dir / filename).write_text("---\ndescription: stub\nmode: subagent\n---\nstub\n")
    return agents_dir


# ---------------------------------------------------------------------------
# hermetic env construction (WI-059)
# ---------------------------------------------------------------------------


def test_build_test_env_preserves_discovery_pins(monkeypatch: pytest.MonkeyPatch) -> None:
    """_run must carry through conftest's config-discovery pins (WI-059).

    The helper strips the AGENT_NOTES_* / REGISTA_* namespaces so tests control
    their own inputs, but the config-discovery pins (AGENT_NOTES_CONFIG and the
    suite-level override paths) must survive — otherwise the install-harness
    subprocess reads the operator's real config and loses hermetic isolation.
    Operator config values (DSN, key paths) stay stripped and test-controlled.
    """
    monkeypatch.setenv("AGENT_NOTES_CONFIG", "/hermetic/empty.json")
    monkeypatch.setenv("AGENT_SUITE_CONFIG", "/hermetic/suite.env")
    monkeypatch.setenv("AGENT_SUITE_SYSTEM_CONFIG", "/hermetic/sys.env")
    monkeypatch.setenv("AGENT_NOTES_DSN", "postgresql://operator/prod")
    monkeypatch.setenv("REGISTA_DSN", "postgresql://operator/regista")
    monkeypatch.setenv("AGENT_NOTES_LEAK_SENTINEL", "do-not-leak")

    env = _build_test_env({"AGENT_NOTES_DSN": "postgresql://native/x"})

    assert env["AGENT_NOTES_CONFIG"] == "/hermetic/empty.json"
    assert env["AGENT_SUITE_CONFIG"] == "/hermetic/suite.env"
    assert env["AGENT_SUITE_SYSTEM_CONFIG"] == "/hermetic/sys.env"
    assert env["AGENT_NOTES_DSN"] == "postgresql://native/x"
    assert "REGISTA_DSN" not in env
    assert "AGENT_NOTES_LEAK_SENTINEL" not in env
    # Pins win over a caller-supplied conflicting value: a test cannot point
    # install-harness at the operator's real config by passing it in env.
    override = _build_test_env({"AGENT_NOTES_CONFIG": "/attacker/config.json"})
    assert override["AGENT_NOTES_CONFIG"] == "/hermetic/empty.json"


# ---------------------------------------------------------------------------
# claude target
# ---------------------------------------------------------------------------


def test_claude_install_wires_env_and_skills():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        result = _run(
            "install-harness",
            "claude",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            env=_SUITE_ENV,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["tool"] == "agent-notes"
        assert data["harness"] == "claude"
        assert data["status"] == "installed"
        assert data["no_op"] is False
        # settings.json env block populated
        settings = json.loads((td / ".claude" / "settings.json").read_text())
        assert settings["env"]["REGISTA_DSN"] == "postgresql://suite/x"
        assert settings["env"]["REGISTA_KEY_PATH"] == "/suite/key"
        assert settings["env"]["AGENT_NOTES_DSN"] == "postgresql://native/x"
        # skills installed
        assert (td / ".claude" / "skills" / "demo" / "SKILL.md").is_file()
        # manifest written
        man = json.loads((td / ".claude" / ".agent-notes-harness.json").read_text())
        assert "REGISTA_DSN" in man["env_keys"]
        assert man["plugin"] is False


def test_claude_reinstall_is_noop_and_manifest_preserved():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        common = ["install-harness", "claude", "--source", str(src), "--home", str(td), "--json"]
        _run(*common, env=_SUITE_ENV, check=False)
        # Re-install
        result = _run(*common, env=_SUITE_ENV, check=False)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["no_op"] is True
        # Manifest must still list all managed keys (regression: re-install must
        # not blank the manifest, or a later uninstall can't find them).
        man = json.loads((td / ".claude" / ".agent-notes-harness.json").read_text())
        assert "REGISTA_DSN" in man["env_keys"]
        assert "AGENT_NOTES_DSN" in man["env_keys"]


def test_claude_uninstall_removes_tracked_and_preserves_user_keys():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        _run(
            "install-harness",
            "claude",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            env=_SUITE_ENV,
            check=False,
        )
        # Add a user-authored key install-harness did not create.
        settings_path = td / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text())
        settings["env"]["USER_KEY"] = "keep-me"
        settings_path.write_text(json.dumps(settings))

        result = _run(
            "install-harness",
            "claude",
            "--uninstall",
            "--home",
            str(td),
            "--json",
            check=False,
        )
        assert result.returncode == 0
        settings = json.loads(settings_path.read_text())
        env = settings.get("env", {})
        # Tracked keys removed
        assert "REGISTA_DSN" not in env
        assert "AGENT_NOTES_DSN" not in env
        # User key preserved
        assert env.get("USER_KEY") == "keep-me"
        # Skills removed
        assert not (td / ".claude" / "skills" / "demo" / "SKILL.md").exists()
        # Manifest removed
        assert not (td / ".claude" / ".agent-notes-harness.json").exists()


def test_claude_uninstall_on_clean_profile_is_noop():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        result = _run(
            "install-harness",
            "claude",
            "--uninstall",
            "--home",
            str(td),
            "--json",
            check=False,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["no_op"] is True
        assert data["actions"] == []


def test_no_clobber_keeps_existing_different_value():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        # Pre-populate settings.json with a user-set REGISTA_DSN.
        (td / ".claude").mkdir(parents=True)
        (td / ".claude" / "settings.json").write_text(
            json.dumps({"env": {"REGISTA_DSN": "postgresql://user-set/x"}})
        )
        result = _run(
            "install-harness",
            "claude",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            env=_SUITE_ENV,
            check=False,
        )
        assert result.returncode == 0
        assert "no clobber" in result.stderr
        settings = json.loads((td / ".claude" / "settings.json").read_text())
        # User value kept
        assert settings["env"]["REGISTA_DSN"] == "postgresql://user-set/x"
        # Other keys written
        assert settings["env"]["REGISTA_KEY_PATH"] == "/suite/key"
        # Manifest does NOT track the clobbered key (so uninstall won't remove it)
        man = json.loads((td / ".claude" / ".agent-notes-harness.json").read_text())
        assert "REGISTA_DSN" not in man["env_keys"]
        assert "REGISTA_KEY_PATH" in man["env_keys"]


def test_dry_run_mutates_nothing_exit_2():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        result = _run(
            "install-harness",
            "claude",
            "--dry-run",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            env=_SUITE_ENV,
            check=False,
        )
        assert result.returncode == 2
        data = json.loads(result.stdout)
        assert data["harness"] == "claude"
        assert isinstance(data["actions"], list)
        # Nothing written
        assert not (td / ".claude" / "settings.json").exists()
        assert not (td / ".claude" / ".agent-notes-harness.json").exists()
        assert not (td / ".claude" / "skills").exists()


# ---------------------------------------------------------------------------
# opencode target
# ---------------------------------------------------------------------------


def test_opencode_install_writes_config_file_and_plugin():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        _make_opencode_agents(td)
        result = _run(
            "install-harness",
            "opencode",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            env=_SUITE_ENV,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        # env -> agent-notes config file (opencode.json has no env block)
        cfg = json.loads(config_path(home=td).read_text())
        assert cfg["regista"]["dsn"] == "postgresql://suite/x"
        assert cfg["regista"]["hmac_key_path"] == "/suite/key"
        assert cfg["regista"]["require_ssl"] is True  # coerced to bool
        assert cfg["regista"]["writes_enabled"] is True
        assert cfg["dsn"] == "postgresql://native/x"
        # plugin -> opencode.json
        oc = json.loads((td / ".config" / "opencode" / "opencode.json").read_text())
        assert any("index.js" in p for p in oc["plugin"])
        # skills
        assert (td / ".config" / "opencode" / "command" / "demo.md").is_file()


def test_opencode_reinstall_noop_and_uninstall():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        _make_opencode_agents(td)
        common = ["install-harness", "opencode", "--source", str(src), "--home", str(td), "--json"]
        _run(*common, env=_SUITE_ENV, check=False)
        # Idempotent
        r2 = _run(*common, env=_SUITE_ENV, check=False)
        assert r2.returncode == 0
        assert json.loads(r2.stdout)["no_op"] is True
        # Uninstall
        result = _run(
            "install-harness",
            "opencode",
            "--uninstall",
            "--home",
            str(td),
            "--json",
            check=False,
        )
        assert result.returncode == 0
        cfg = json.loads(config_path(home=td).read_text())
        assert "dsn" not in cfg
        assert cfg.get("regista", {}) == {}
        oc = json.loads((td / ".config" / "opencode" / "opencode.json").read_text())
        assert not any("index.js" in p for p in oc.get("plugin", []))


# ---------------------------------------------------------------------------
# all target + user + contract shape
# ---------------------------------------------------------------------------


def test_all_target_expands_supported_public_targets_only():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        _make_opencode_agents(td)
        result = _run(
            "install-harness",
            "all",
            "--dry-run",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            env=_SUITE_ENV,
            check=False,
        )
        assert result.returncode == 2, result.stderr
        data = json.loads(result.stdout)
        assert data["harness"] == "all"
        assert len(data["results"]) == 2
        assert [item["harness"] for item in data["results"]] == [
            "claude",
            "opencode",
        ]
        assert not (td / ".hermes").exists()
        assert not (td / ".claude").exists()
        assert not (td / ".config" / "opencode").exists()


def test_codex_install_writes_skills_and_owned_hooks_but_no_toml_config():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        result = _run(
            "install-harness",
            "codex",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            env=_SUITE_ENV,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["harness"] == "codex"
        assert data["status"] == "installed"
        assert data["no_op"] is False
        # Skills land at $HOME/.agents/skills (suite contract §2 Codex; Plan 019
        # Decision 2 restored), SKILL.md layout — NOT under $CODEX_HOME/skills.
        assert (td / ".agents" / "skills" / "demo" / "SKILL.md").is_file()
        assert not (td / ".codex" / "skills").exists()
        # Decision 4: Codex config is never written (no secret/config leak).
        assert not (td / ".codex" / "config.toml").exists()
        hooks = json.loads((td / ".codex" / "hooks.json").read_text())
        assert set(hooks["hooks"]) == {"SessionStart", "Stop"}
        assert hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"] == (
            "agent-notes codex-hook session-start"
        )
        assert hooks["hooks"]["Stop"][0]["hooks"][0]["command"] == (
            "agent-notes codex-hook stop"
        )
        # No env leaked into the agent-notes config file either.
        assert not config_path(home=td).exists()
        # Manifest stays under $CODEX_HOME as a stable ownership sidecar and
        # records the installed skill and no env/plugin.
        man = json.loads((td / ".codex" / ".agent-notes-harness.json").read_text())
        assert "demo" in man["skills"]
        assert isinstance(man["skills"]["demo"], str)
        assert man["env_keys"] == []
        assert man["plugin"] is False
        assert set(man["hooks"]) == {"SessionStart", "Stop"}
        assert man["hooks_file_created"] is True


def test_codex_reinstall_is_noop():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        common = ["install-harness", "codex", "--source", str(src), "--home", str(td), "--json"]
        _run(*common, env=_SUITE_ENV, check=False)
        result = _run(*common, env=_SUITE_ENV, check=False)
        assert result.returncode == 0
        assert json.loads(result.stdout)["no_op"] is True


def test_codex_dry_run_writes_nothing():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        result = _run(
            "install-harness",
            "codex",
            "--dry-run",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            env=_SUITE_ENV,
            check=False,
        )
        assert result.returncode == 2
        assert not (td / ".codex").exists()
        assert not (td / ".agents").exists()


def test_codex_uninstall_removes_skills_and_manifest():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        _run(
            "install-harness",
            "codex",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            env=_SUITE_ENV,
            check=False,
        )
        # A user's own skill must survive uninstall (only tracked files removed).
        user_skill = td / ".agents" / "skills" / "mine" / "SKILL.md"
        user_skill.parent.mkdir(parents=True)
        user_skill.write_text("---\nname: mine\n---\nkeep me\n")

        result = _run(
            "install-harness",
            "codex",
            "--uninstall",
            "--home",
            str(td),
            "--json",
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert not (td / ".agents" / "skills" / "demo").exists()
        assert not (td / ".codex" / ".agent-notes-harness.json").exists()
        assert not (td / ".codex" / "hooks.json").exists()
        assert user_skill.is_file()  # untracked user skill preserved


def test_codex_hooks_merge_and_uninstall_preserve_cairn_and_user_groups():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        hooks_path = td / ".codex" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        original = {
            "description": "operator-owned hooks",
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "cairn _codex_hook SessionStart",
                            }
                        ]
                    }
                ],
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "user-policy"}],
                    }
                ],
            },
        }
        hooks_path.write_text(json.dumps(original), encoding="utf-8")

        install = _run(
            "install-harness",
            "codex",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            check=False,
        )
        assert install.returncode == 0, install.stderr
        installed = json.loads(hooks_path.read_text())
        assert installed["description"] == "operator-owned hooks"
        assert len(installed["hooks"]["SessionStart"]) == 2
        assert installed["hooks"]["PreToolUse"] == original["hooks"]["PreToolUse"]

        uninstall = _run(
            "install-harness",
            "codex",
            "--uninstall",
            "--home",
            str(td),
            "--json",
            check=False,
        )
        assert uninstall.returncode == 0, uninstall.stderr
        assert json.loads(hooks_path.read_text()) == original


def test_codex_modified_owned_hook_is_preserved_and_reported():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        common = ["install-harness", "codex", "--source", str(src), "--home", str(td)]
        _run(*common, "--json", check=False)
        hooks_path = td / ".codex" / "hooks.json"
        hooks = json.loads(hooks_path.read_text())
        hooks["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 99
        hooks_path.write_text(json.dumps(hooks), encoding="utf-8")

        reinstall = _run(*common, "--json", check=False)
        assert reinstall.returncode == 1
        assert json.loads(reinstall.stdout)["status"] == "failed"
        assert "locally modified" in reinstall.stderr
        assert json.loads(hooks_path.read_text())["hooks"]["Stop"][0]["hooks"][0][
            "timeout"
        ] == 99

        uninstall = _run(
            "install-harness", "codex", "--uninstall", "--home", str(td), "--json", check=False
        )
        assert uninstall.returncode == 1
        assert hooks_path.is_file()
        assert "Stop" in json.loads(hooks_path.read_text())["hooks"]
        manifest = json.loads((td / ".codex" / ".agent-notes-harness.json").read_text())
        assert set(manifest["hooks"]) == {"Stop"}


def test_codex_preexisting_same_command_hook_is_not_adopted():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        hooks_path = td / ".codex" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        hooks_path.write_text(
            json.dumps(
                {
                    "hooks": {
                        "Stop": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "agent-notes codex-hook stop",
                                    }
                                ]
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        result = _run(
            "install-harness",
            "codex",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            check=False,
        )
        assert result.returncode == 1
        assert "pre-existing and not owned" in result.stderr
        manifest = json.loads((td / ".codex" / ".agent-notes-harness.json").read_text())
        assert set(manifest["hooks"]) == {"SessionStart"}


def test_empty_source_fails_not_false_green():
    """An empty/missing skills source must not report installed/no_op (WI-022).

    Contract §4 bounds `no_op: true` to an already-installed idempotent state.
    A source that resolves to zero skills is a failure surfaced with an explicit
    action, in both real and dry-run modes, for every harness.
    """
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        empty_src = td / "empty-skills"
        empty_src.mkdir()
        for harness in ("claude", "codex"):
            # Real install: status=failed, no_op=false, exit 1.
            result = _run(
                "install-harness",
                harness,
                "--source",
                str(empty_src),
                "--home",
                str(td),
                "--json",
                check=False,
            )
            assert result.returncode == 1, (harness, result.stderr)
            data = json.loads(result.stdout)
            assert data["status"] == "failed", harness
            assert data["no_op"] is False, harness
            assert any(a.get("status") == "missing" for a in data["actions"]), harness
            # Nothing written despite the failure.
            assert not (td / ".claude").exists()
            assert not (td / ".agents").exists()

            # Dry-run previews the same failure (status=failed in the payload;
            # exit code is 2 because dry-run completed, per contract §4).
            dry = _run(
                "install-harness",
                harness,
                "--dry-run",
                "--source",
                str(empty_src),
                "--home",
                str(td),
                "--json",
                check=False,
            )
            assert dry.returncode == 2, (harness, dry.stderr)
            dry_data = json.loads(dry.stdout)
            assert dry_data["status"] == "failed", harness
            assert dry_data["no_op"] is False, harness


def test_codex_install_uses_home_agents_skills_not_codex_home():
    """Codex skills must land under $HOME/.agents/skills, never $CODEX_HOME/skills
    (suite contract §2 Codex). Regression guard for the WI-022 location bug."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        result = _run(
            "install-harness",
            "codex",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            env=_SUITE_ENV,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert (td / ".agents" / "skills" / "demo" / "SKILL.md").is_file()
        # The old, wrong location must remain empty.
        assert not (td / ".codex" / "skills").exists()


def test_file_path_source_fails_gracefully_not_traceback():
    """An operator --source that points at a file (not a dir) must surface a
    contract-shaped failure, not a NotADirectoryError traceback (WI-022).
    Guards the _discover_skills non-directory handling the false-green guard
    relies on."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        not_a_dir = td / "skills.txt"
        not_a_dir.write_text("this is a file, not a skills tree\n")
        result = _run(
            "install-harness",
            "claude",
            "--source",
            str(not_a_dir),
            "--home",
            str(td),
            "--json",
            check=False,
        )
        assert result.returncode == 1, result.stderr
        assert "Traceback" not in result.stderr, "must not crash with a traceback"
        data = json.loads(result.stdout)
        assert data["status"] == "failed"
        assert data["no_op"] is False
        miss = [a for a in data["actions"] if a.get("status") == "missing"]
        assert len(miss) == 1


def test_user_flag_sets_principal_id():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        result = _run(
            "install-harness",
            "claude",
            "--user",
            "paul@hraedon.com",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            env=_SUITE_ENV,
            check=False,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["user"] == "paul@hraedon.com"
        settings = json.loads((td / ".claude" / "settings.json").read_text())
        assert settings["env"]["AGENT_NOTES_PRINCIPAL_ID"] == "paul@hraedon.com"


def test_dry_run_contract_shape():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        result = _run(
            "install-harness",
            "claude",
            "--dry-run",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            env=_SUITE_ENV,
            check=False,
        )
        data = json.loads(result.stdout)
        for key in ("tool", "harness", "user", "actions", "no_op"):
            assert key in data
        for a in data["actions"]:
            assert "kind" in a
            assert "path" in a
            assert "detail" in a


def test_unknown_harness_exits_1():
    result = _run("install-harness", "slack", "--json", check=False)
    assert result.returncode == 1


def test_install_skills_still_works_alongside_install_harness():
    """install-skills (the skills-only sub-step) must remain functional (Plan 004 AC)."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        dest = td / "claude-skills"
        result = _run(
            "install-skills",
            "--target",
            "claude",
            "--source",
            str(src),
            "--dest",
            str(dest),
            "--json",
            check=False,
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["skills"][0]["status"] == "created"


# ---------------------------------------------------------------------------
# review B1: manifest-drift on re-install with a reduced env-var set
# ---------------------------------------------------------------------------


def test_reinstall_with_fewer_env_vars_preserves_manifest_tracking():
    """Re-installing after unsetting an env var must not drop the key from the
    manifest — uninstall must still remove the value install-harness wrote
    (review B1, contract §3 rule 4).
    """
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        full = ["install-harness", "claude", "--source", str(src), "--home", str(td), "--json"]
        # Install with full env.
        _run(*full, env=_SUITE_ENV, check=False)
        # Re-install with REGISTA_DSN unset (user removed it from their shell).
        reduced = {k: v for k, v in _SUITE_ENV.items() if k != "REGISTA_DSN"}
        _run(*full, env=reduced, check=False)
        # Manifest must still track REGISTA_DSN (still in settings.json from
        # the first install).
        man = json.loads((td / ".claude" / ".agent-notes-harness.json").read_text())
        assert "REGISTA_DSN" in man["env_keys"]
        # Uninstall must remove it.
        _run("install-harness", "claude", "--uninstall", "--home", str(td), "--json", check=False)
        settings = json.loads((td / ".claude" / "settings.json").read_text())
        assert "REGISTA_DSN" not in settings.get("env", {})


def test_reinstall_with_removed_skill_preserves_manifest_tracking():
    """A skill removed from the repo between installs must stay tracked so
    uninstall removes the orphaned file (review B1).
    """
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # Two skills initially.
        src = td / "skills"
        for name in ("demo", "extra"):
            d = src / name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody\n")
        common = ["install-harness", "claude", "--source", str(src), "--home", str(td), "--json"]
        _run(*common, env=_SUITE_ENV, check=False)
        assert (td / ".claude" / "skills" / "extra" / "SKILL.md").is_file()
        # Remove 'extra' from the source tree.
        import shutil

        shutil.rmtree(src / "extra")
        # Re-install (only 'demo' is discovered now).
        _run(*common, env=_SUITE_ENV, check=False)
        # Manifest must still list 'extra' (file still on disk).
        man = json.loads((td / ".claude" / ".agent-notes-harness.json").read_text())
        assert "extra" in man["skills"]
        # Uninstall removes the orphaned skill file.
        _run("install-harness", "claude", "--uninstall", "--home", str(td), "--json", check=False)
        assert not (td / ".claude" / "skills" / "extra" / "SKILL.md").exists()


# ---------------------------------------------------------------------------
# opencode no-clobber (review T3) + --user opencode (review T2)
# ---------------------------------------------------------------------------


def test_opencode_no_clobber_keeps_existing_different_value():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        _make_opencode_agents(td)
        # Pre-populate the agent-notes config with a user-set regista.dsn.
        _cfg = config_path(home=td)
        _cfg.parent.mkdir(parents=True, exist_ok=True)
        _cfg.write_text(json.dumps({"regista": {"dsn": "postgresql://user-set/x"}}))
        result = _run(
            "install-harness",
            "opencode",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            env=_SUITE_ENV,
            check=False,
        )
        assert result.returncode == 0
        assert "no clobber" in result.stderr
        cfg = json.loads(config_path(home=td).read_text())
        # User value kept.
        assert cfg["regista"]["dsn"] == "postgresql://user-set/x"
        # Other keys written.
        assert cfg["regista"]["hmac_key_path"] == "/suite/key"
        # Clobbered key NOT tracked in manifest.
        man = json.loads((td / ".config" / "opencode" / ".agent-notes-harness.json").read_text())
        assert "regista.dsn" not in man["env_keys"]
        assert "regista.hmac_key_path" in man["env_keys"]


def test_user_flag_warns_for_opencode():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        _make_opencode_agents(td)
        result = _run(
            "install-harness",
            "opencode",
            "--user",
            "paul@hraedon.com",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            env=_SUITE_ENV,
            check=False,
        )
        assert result.returncode == 0
        assert "principal overlay skipped" in result.stderr


def test_no_env_vars_still_installs_skills_with_warning():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        result = _run(
            "install-harness",
            "claude",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            env={},
            check=False,
        )
        assert result.returncode == 0
        assert "no suite env vars found" in result.stderr
        # Skills still installed.
        assert (td / ".claude" / "skills" / "demo" / "SKILL.md").is_file()
        # Manifest with empty env_keys.
        man = json.loads((td / ".claude" / ".agent-notes-harness.json").read_text())
        assert man["env_keys"] == []


# ---------------------------------------------------------------------------
# hermes target
# ---------------------------------------------------------------------------


def test_hermes_install_wires_env_and_skills():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        result = _run(
            "install-harness",
            "hermes",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            env=_SUITE_ENV,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["tool"] == "agent-notes"
        assert data["harness"] == "hermes"
        assert data["no_op"] is False
        # .env file populated with sentinel-wrapped managed block
        env_content = (td / ".hermes" / ".env").read_text()
        assert "REGISTA_DSN=postgresql://suite/x" in env_content
        assert "REGISTA_KEY_PATH=/suite/key" in env_content
        assert "AGENT_NOTES_DSN=postgresql://native/x" in env_content
        assert "# BEGIN agent-notes-harness-managed" in env_content
        assert "# END agent-notes-harness-managed" in env_content
        # skills installed (directory-based SKILL.md, same as claude)
        assert (td / ".hermes" / "skills" / "demo" / "SKILL.md").is_file()
        # manifest written
        man = json.loads((td / ".hermes" / ".agent-notes-harness.json").read_text())
        assert "REGISTA_DSN" in man["env_keys"]
        assert man["plugin"] is False
        # No agents key for hermes (unlike opencode)
        assert "agents" not in man


def test_hermes_reinstall_is_noop_and_manifest_preserved():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        common = ["install-harness", "hermes", "--source", str(src), "--home", str(td), "--json"]
        _run(*common, env=_SUITE_ENV, check=False)
        # Re-install
        result = _run(*common, env=_SUITE_ENV, check=False)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["no_op"] is True
        # Manifest must still list all managed keys
        man = json.loads((td / ".hermes" / ".agent-notes-harness.json").read_text())
        assert "REGISTA_DSN" in man["env_keys"]
        assert "AGENT_NOTES_DSN" in man["env_keys"]


def test_hermes_uninstall_removes_managed_block_and_preserves_user_entries():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        _run(
            "install-harness",
            "hermes",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            env=_SUITE_ENV,
            check=False,
        )
        # Add a user-authored entry outside the managed block.
        env_path = td / ".hermes" / ".env"
        content = env_path.read_text()
        # Prepend a user entry before the managed block.
        content = "USER_KEY=keep-me\n" + content
        env_path.write_text(content)

        result = _run(
            "install-harness",
            "hermes",
            "--uninstall",
            "--home",
            str(td),
            "--json",
            check=False,
        )
        assert result.returncode == 0
        env_content = env_path.read_text()
        # Managed block removed
        assert "# BEGIN agent-notes-harness-managed" not in env_content
        assert "REGISTA_DSN" not in env_content
        # User entry preserved
        assert "USER_KEY=keep-me" in env_content
        # Skills removed
        assert not (td / ".hermes" / "skills" / "demo" / "SKILL.md").exists()
        # Manifest removed
        assert not (td / ".hermes" / ".agent-notes-harness.json").exists()


def test_hermes_uninstall_on_clean_profile_is_noop():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        result = _run(
            "install-harness",
            "hermes",
            "--uninstall",
            "--home",
            str(td),
            "--json",
            check=False,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["no_op"] is True
        assert data["actions"] == []


def test_hermes_no_clobber_keeps_existing_different_value():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        # Pre-populate .env with a user-set REGISTA_DSN outside the managed block.
        (td / ".hermes").mkdir(parents=True)
        (td / ".hermes" / ".env").write_text("REGISTA_DSN=postgresql://user-set/x\n")
        result = _run(
            "install-harness",
            "hermes",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            env=_SUITE_ENV,
            check=False,
        )
        assert result.returncode == 0
        assert "no clobber" in result.stderr
        env_content = (td / ".hermes" / ".env").read_text()
        # User value kept
        assert "REGISTA_DSN=postgresql://user-set/x" in env_content
        # Other keys written in managed block
        assert "REGISTA_KEY_PATH=/suite/key" in env_content
        # Manifest does NOT track the clobbered key
        man = json.loads((td / ".hermes" / ".agent-notes-harness.json").read_text())
        assert "REGISTA_DSN" not in man["env_keys"]
        assert "REGISTA_KEY_PATH" in man["env_keys"]


def test_hermes_dry_run_mutates_nothing_exit_2():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        result = _run(
            "install-harness",
            "hermes",
            "--dry-run",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            env=_SUITE_ENV,
            check=False,
        )
        assert result.returncode == 2
        data = json.loads(result.stdout)
        assert data["harness"] == "hermes"
        assert isinstance(data["actions"], list)
        # Nothing written
        assert not (td / ".hermes" / ".env").exists()
        assert not (td / ".hermes" / ".agent-notes-harness.json").exists()
        assert not (td / ".hermes" / "skills").exists()


def test_hermes_reinstall_with_fewer_env_vars_preserves_manifest_tracking():
    """Re-installing after unsetting an env var must not drop the key from the
    manifest — uninstall must still remove the value (review B1).
    """
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        full = ["install-harness", "hermes", "--source", str(src), "--home", str(td), "--json"]
        # Install with full env.
        _run(*full, env=_SUITE_ENV, check=False)
        # Re-install with REGISTA_DSN unset.
        reduced = {k: v for k, v in _SUITE_ENV.items() if k != "REGISTA_DSN"}
        _run(*full, env=reduced, check=False)
        # Manifest must still track REGISTA_DSN (still in .env managed block).
        man = json.loads((td / ".hermes" / ".agent-notes-harness.json").read_text())
        assert "REGISTA_DSN" in man["env_keys"]
        # Uninstall must remove it.
        _run("install-harness", "hermes", "--uninstall", "--home", str(td), "--json", check=False)
        env_content = (td / ".hermes" / ".env").read_text()
        assert "REGISTA_DSN" not in env_content


# ---------------------------------------------------------------------------
# hash-tracked ownership (Plan 019 Decision 5, WI-1.2)
# ---------------------------------------------------------------------------


def test_first_install_preserves_preexisting_unowned_skill():
    """A conflicting file with no ownership manifest is never overwritten or
    adopted, so a later uninstall cannot delete another installer's skill."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        skill_file = td / ".agents" / "skills" / "demo" / "SKILL.md"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text("---\nname: demo\n---\nowned elsewhere\n")

        result = _run(
            "install-harness",
            "codex",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            check=False,
        )
        data = json.loads(result.stdout)

        assert result.returncode == 1
        assert data["status"] == "failed"
        assert data["actions"][0]["status"] == "conflict"
        assert "owned elsewhere" in skill_file.read_text()
        manifest = json.loads((td / ".codex" / ".agent-notes-harness.json").read_text())
        assert "demo" not in manifest["skills"]

        uninstall = _run(
            "install-harness",
            "codex",
            "--uninstall",
            "--home",
            str(td),
            "--json",
            check=False,
        )
        assert uninstall.returncode == 0
        assert "owned elsewhere" in skill_file.read_text()


def test_first_install_does_not_adopt_identical_unowned_skill():
    """Matching bytes are not sufficient evidence that uninstall may own a
    pre-existing shared skill."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        payload = (src / "demo" / "SKILL.md").read_text()
        skill_file = td / ".agents" / "skills" / "demo" / "SKILL.md"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text(payload)

        result = _run(
            "install-harness",
            "codex",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            check=False,
        )

        assert result.returncode == 1
        assert "pre-existing and not owned" in result.stderr
        manifest = json.loads((td / ".codex" / ".agent-notes-harness.json").read_text())
        assert manifest["skills"] == {}


def test_install_preserves_user_modified_skill():
    """A locally modified skill is not overwritten on re-install (WI-1.2)."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        common = ["install-harness", "claude", "--source", str(src), "--home", str(td), "--json"]
        _run(*common, env=_SUITE_ENV, check=False)

        skill_file = td / ".claude" / "skills" / "demo" / "SKILL.md"
        original = skill_file.read_text()
        skill_file.write_text(original + "\n# user customization\n")

        result = _run(*common, env=_SUITE_ENV, check=False)
        data = json.loads(result.stdout)
        # Conflict must be surfaced in actions.
        conflict_actions = [a for a in data["actions"] if a.get("status") == "conflict"]
        assert len(conflict_actions) == 1
        assert "locally modified" in result.stderr.lower()
        # File preserved — user changes intact.
        assert "# user customization" in skill_file.read_text()


def test_uninstall_preserves_user_modified_skill():
    """Uninstall preserves a locally modified skill and reports it (WI-1.2)."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        _run(
            "install-harness",
            "claude",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            env=_SUITE_ENV,
            check=False,
        )

        skill_file = td / ".claude" / "skills" / "demo" / "SKILL.md"
        skill_file.write_text("---\nname: demo\n---\nuser changed this\n")

        result = _run(
            "install-harness",
            "claude",
            "--uninstall",
            "--home",
            str(td),
            "--json",
            check=False,
        )
        data = json.loads(result.stdout)
        # Skill preserved on disk.
        assert skill_file.is_file()
        assert "user changed this" in skill_file.read_text()
        # Warning surfaced.
        assert "locally modified" in result.stderr.lower()
        # A skip_file action recorded.
        skip_actions = [a for a in data["actions"] if a.get("kind") == "skip_file"]
        assert len(skip_actions) == 1


def test_install_updates_unchanged_outdated_skill():
    """When the source changes but the user hasn't modified the installed copy,
    the skill is updated (no false conflict)."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        common = ["install-harness", "claude", "--source", str(src), "--home", str(td), "--json"]
        _run(*common, env=_SUITE_ENV, check=False)

        # Change the source skill (simulates an upstream update).
        skill_src = src / "demo" / "SKILL.md"
        skill_src.write_text("---\nname: demo\n---\nnew upstream content\n")

        result = _run(*common, env=_SUITE_ENV, check=False)
        data = json.loads(result.stdout)
        update_actions = [a for a in data["actions"] if a.get("status") == "updated"]
        assert len(update_actions) == 1
        # File was updated.
        installed = (td / ".claude" / "skills" / "demo" / "SKILL.md").read_text()
        assert "new upstream content" in installed


def test_backward_compat_old_manifest_uninstall():
    """An old-format manifest (skills as a list of names, no hashes) must
    still work for uninstall — skills are removed without hash checking."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        _run(
            "install-harness",
            "claude",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            env=_SUITE_ENV,
            check=False,
        )
        # Overwrite the manifest with old list format.
        man_path = td / ".claude" / ".agent-notes-harness.json"
        man = json.loads(man_path.read_text())
        man["skills"] = ["demo"]
        man_path.write_text(json.dumps(man))

        result = _run(
            "install-harness",
            "claude",
            "--uninstall",
            "--home",
            str(td),
            "--json",
            check=False,
        )
        assert result.returncode == 0
        # Skill removed (no hash to check, backward compatible).
        assert not (td / ".claude" / "skills" / "demo" / "SKILL.md").exists()


def test_backward_compat_old_manifest_reinstall():
    """Re-install with an old-format manifest migrates to hash format."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        _run(
            "install-harness",
            "claude",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            env=_SUITE_ENV,
            check=False,
        )
        # Overwrite manifest with old list format.
        man_path = td / ".claude" / ".agent-notes-harness.json"
        man = json.loads(man_path.read_text())
        man["skills"] = ["demo"]
        man_path.write_text(json.dumps(man))

        # Re-install: should migrate to dict format.
        _run(
            "install-harness",
            "claude",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            env=_SUITE_ENV,
            check=False,
        )
        man = json.loads(man_path.read_text())
        assert isinstance(man["skills"], dict)
        assert "demo" in man["skills"]
        assert isinstance(man["skills"]["demo"], str)


def test_agent_install_preserves_user_modified():
    """A locally modified opencode agent is not overwritten on re-install."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        _make_opencode_agents(td)
        common = ["install-harness", "opencode", "--source", str(src), "--home", str(td), "--json"]
        _run(*common, env=_SUITE_ENV, check=False)

        agent_file = td / ".config" / "opencode" / "agents" / "glm.md"
        original = agent_file.read_text()
        agent_file.write_text(original + "\n# user tweak\n")

        result = _run(*common, env=_SUITE_ENV, check=False)
        data = json.loads(result.stdout)
        conflict_actions = [a for a in data["actions"] if a.get("status") == "conflict"]
        assert len(conflict_actions) >= 1
        assert "locally modified" in result.stderr.lower()
        assert "# user tweak" in agent_file.read_text()


def test_agent_uninstall_preserves_user_modified():
    """Uninstall preserves a locally modified opencode agent."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        _make_opencode_agents(td)
        _run(
            "install-harness",
            "opencode",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            env=_SUITE_ENV,
            check=False,
        )

        agent_file = td / ".config" / "opencode" / "agents" / "glm.md"
        agent_file.write_text("---\ndescription: stub\n---\nuser changed\n")

        result = _run(
            "install-harness",
            "opencode",
            "--uninstall",
            "--home",
            str(td),
            "--json",
            check=False,
        )
        # Agent preserved.
        assert agent_file.is_file()
        assert "user changed" in agent_file.read_text()
        assert "locally modified" in result.stderr.lower()


def test_dry_run_detects_conflict():
    """Dry-run surfaces conflicts without mutating anything."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        common = ["install-harness", "claude", "--source", str(src), "--home", str(td), "--json"]
        _run(*common, env=_SUITE_ENV, check=False)

        skill_file = td / ".claude" / "skills" / "demo" / "SKILL.md"
        original = skill_file.read_text()
        skill_file.write_text(original + "\n# user edit\n")

        result = _run(
            "install-harness",
            "claude",
            "--dry-run",
            "--source",
            str(src),
            "--home",
            str(td),
            "--json",
            env=_SUITE_ENV,
            check=False,
        )
        assert result.returncode == 2
        data = json.loads(result.stdout)
        conflict_actions = [a for a in data["actions"] if a.get("status") == "conflict"]
        assert len(conflict_actions) == 1
        # Nothing was written — user edit still intact.
        assert "# user edit" in skill_file.read_text()


def test_uninstall_then_reinstall_preserves_user_modified_skill():
    """Uninstall preserves a modified skill and retains a reduced manifest so
    a subsequent install detects the conflict (review Finding 1)."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        common = ["install-harness", "claude", "--source", str(src), "--home", str(td), "--json"]
        _run(*common, env=_SUITE_ENV, check=False)

        skill_file = td / ".claude" / "skills" / "demo" / "SKILL.md"
        skill_file.write_text("---\nname: demo\n---\nuser changed this\n")

        # Uninstall: skill preserved, manifest retained.
        _run("install-harness", "claude", "--uninstall", "--home", str(td), "--json", check=False)
        assert skill_file.is_file()
        assert "user changed this" in skill_file.read_text()
        # Reduced manifest still on disk.
        man_path = td / ".claude" / ".agent-notes-harness.json"
        assert man_path.is_file()
        man = json.loads(man_path.read_text())
        assert "demo" in man["skills"]
        assert isinstance(man["skills"]["demo"], str)
        assert man["env_keys"] == []
        assert man["plugin"] is False

        # Re-install: conflict detected, user changes preserved.
        result = _run(*common, env=_SUITE_ENV, check=False)
        data = json.loads(result.stdout)
        conflict_actions = [a for a in data["actions"] if a.get("status") == "conflict"]
        assert len(conflict_actions) == 1
        assert "user changed this" in skill_file.read_text()


def test_agent_backward_compat_old_manifest_uninstall():
    """Old-format agent manifest (list of names) works with uninstall —
    agents removed without hash checking (backward compatible)."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        _make_opencode_agents(td)
        _run(
            "install-harness", "opencode", "--source", str(src), "--home", str(td), "--json",
            env=_SUITE_ENV, check=False,
        )
        # Overwrite manifest agents with old list format.
        man_path = td / ".config" / "opencode" / ".agent-notes-harness.json"
        man = json.loads(man_path.read_text())
        man["agents"] = ["glm"]
        man_path.write_text(json.dumps(man))

        result = _run(
            "install-harness", "opencode", "--uninstall", "--home", str(td), "--json",
            check=False,
        )
        assert result.returncode == 0
        # Agent removed (no hash to check, backward compatible).
        assert not (td / ".config" / "opencode" / "agents" / "glm.md").exists()


def test_corrupted_manifest_dry_run_does_not_crash():
    """A corrupted manifest should not block --dry-run (review Finding 3)."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        # Write a corrupted manifest.
        man_path = td / ".claude" / ".agent-notes-harness.json"
        man_path.parent.mkdir(parents=True, exist_ok=True)
        man_path.write_text("{invalid json")
        result = _run(
            "install-harness", "claude", "--dry-run",
            "--source", str(src), "--home", str(td), "--json",
            env=_SUITE_ENV, check=False,
        )
        assert result.returncode == 2
        assert "could not read manifest" in result.stderr


def test_corrupted_manifest_non_dry_run_exits_1():
    """A corrupted manifest in non-dry-run mode must exit 1 (not silently
    proceed)."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        man_path = td / ".claude" / ".agent-notes-harness.json"
        man_path.parent.mkdir(parents=True, exist_ok=True)
        man_path.write_text("{invalid json")
        result = _run(
            "install-harness", "claude",
            "--source", str(src), "--home", str(td), "--json",
            env=_SUITE_ENV, check=False,
        )
        assert result.returncode == 1


def test_non_string_hash_coerced_to_none():
    """A crafted manifest with non-string hash values must not cause false
    conflicts (review Finding 5)."""
    from agent_notes.cli.harness import _normalize_hash_map

    assert _normalize_hash_map({"demo": 42}) == {"demo": None}
    assert _normalize_hash_map({"demo": True}) == {"demo": None}
    assert _normalize_hash_map({"demo": [1, 2]}) == {"demo": None}
    assert _normalize_hash_map({"demo": None}) == {"demo": None}
    assert _normalize_hash_map({"demo": "abc123"}) == {"demo": "abc123"}


def test_agent_uninstall_then_reinstall_preserves_modified():
    """Opencode agent: uninstall preserves modified agent, reinstall detects
    conflict via reduced manifest."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        _make_opencode_agents(td)
        common = [
            "install-harness", "opencode", "--source", str(src), "--home", str(td), "--json",
        ]
        _run(*common, env=_SUITE_ENV, check=False)

        agent_file = td / ".config" / "opencode" / "agents" / "glm.md"
        agent_file.write_text("---\ndescription: stub\n---\nuser changed\n")

        # Uninstall: agent preserved, reduced manifest retained.
        _run(
            "install-harness", "opencode", "--uninstall", "--home", str(td), "--json",
            check=False,
        )
        assert agent_file.is_file()
        man_path = td / ".config" / "opencode" / ".agent-notes-harness.json"
        assert man_path.is_file()
        man = json.loads(man_path.read_text())
        assert "glm" in man.get("agents", {})

        # Reinstall: conflict detected.
        result = _run(*common, env=_SUITE_ENV, check=False)
        data = json.loads(result.stdout)
        conflict_actions = [a for a in data["actions"] if a.get("status") == "conflict"]
        assert len(conflict_actions) >= 1
        assert "user changed" in agent_file.read_text()


def test_install_conflict_returns_exit_1():
    """Install with conflicts returns exit 1 and status 'failed' (not 0)."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        common = ["install-harness", "claude", "--source", str(src), "--home", str(td), "--json"]
        _run(*common, env=_SUITE_ENV, check=False)

        skill_file = td / ".claude" / "skills" / "demo" / "SKILL.md"
        skill_file.write_text("---\nname: demo\n---\nuser edit\n")

        result = _run(*common, env=_SUITE_ENV, check=False)
        assert result.returncode == 1
        data = json.loads(result.stdout)
        assert data["status"] == "failed"


def test_uninstall_preserved_returns_exit_1():
    """Uninstall with preserved files returns exit 1 (not 0)."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        _run(
            "install-harness", "claude", "--source", str(src), "--home", str(td), "--json",
            env=_SUITE_ENV, check=False,
        )

        skill_file = td / ".claude" / "skills" / "demo" / "SKILL.md"
        skill_file.write_text("---\nname: demo\n---\nuser edit\n")

        result = _run(
            "install-harness", "claude", "--uninstall", "--home", str(td), "--json",
            check=False,
        )
        assert result.returncode == 1
        data = json.loads(result.stdout)
        assert data["status"] == "failed"


def test_non_utf8_reinstall_does_not_crash():
    """A binary/non-UTF-8 replacement of a skill file must not crash reinstall.
    The file is treated as user-modified (conflict, preserved)."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        common = ["install-harness", "claude", "--source", str(src), "--home", str(td), "--json"]
        _run(*common, env=_SUITE_ENV, check=False)

        skill_file = td / ".claude" / "skills" / "demo" / "SKILL.md"
        skill_file.write_bytes(b"\x80\x81\x82binary")

        result = _run(*common, env=_SUITE_ENV, check=False)
        assert result.returncode == 1
        data = json.loads(result.stdout)
        conflict_actions = [a for a in data["actions"] if a.get("status") == "conflict"]
        assert len(conflict_actions) == 1
        # Binary content preserved.
        assert skill_file.read_bytes() == b"\x80\x81\x82binary"


def test_non_utf8_uninstall_does_not_crash():
    """A binary/non-UTF-8 replacement must not crash uninstall either.
    The file is treated as user-modified (preserved)."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        _run(
            "install-harness", "claude", "--source", str(src), "--home", str(td), "--json",
            env=_SUITE_ENV, check=False,
        )

        skill_file = td / ".claude" / "skills" / "demo" / "SKILL.md"
        skill_file.write_bytes(b"\x80\x81\x82binary")

        result = _run(
            "install-harness", "claude", "--uninstall", "--home", str(td), "--json",
            check=False,
        )
        assert result.returncode == 1
        # Binary file preserved.
        assert skill_file.read_bytes() == b"\x80\x81\x82binary"


def test_all_target_mixed_conflict_and_success():
    """install-harness all with one harness conflict returns exit 1."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        _make_opencode_agents(td)
        common = ["install-harness", "all", "--source", str(src), "--home", str(td), "--json"]
        _run(*common, env=_SUITE_ENV, check=False)

        # Modify only the claude skill.
        claude_skill = td / ".claude" / "skills" / "demo" / "SKILL.md"
        claude_skill.write_text("---\nname: demo\n---\nuser edit\n")

        result = _run(*common, env=_SUITE_ENV, check=False)
        assert result.returncode == 1
        data = json.loads(result.stdout)
        statuses = {r["harness"]: r["status"] for r in data["results"]}
        assert statuses["claude"] == "failed"
        assert statuses["opencode"] == "installed"
