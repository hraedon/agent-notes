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

from agent_notes.core.config import config_path

_CLI = [sys.executable, "-m", "agent_notes.cli"]

_SUITE_ENV = {
    "REGISTA_DSN": "postgresql://suite/x",
    "REGISTA_KEY_PATH": "/suite/key",
    "REGISTA_REQUIRE_SSL": "true",
    "AGENT_NOTES_DSN": "postgresql://native/x",
    "AGENT_NOTES_REGISTA_WRITES": "1",
}


def _run(*args: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    # Strip inherited agent-notes/regista env so tests control their own inputs,
    # then apply the test-provided env on top.
    clean = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("AGENT_NOTES_") and not k.startswith("REGISTA_")
    }
    merged = {**clean, **(env or {})}
    return subprocess.run(
        _CLI + list(args), capture_output=True, text=True, env=merged, check=check
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
        (agents_dir / filename).write_text(
            "---\ndescription: stub\nmode: subagent\n---\nstub\n"
        )
    return agents_dir


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


def test_codex_install_writes_skills_only_no_config():
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
        # Skills land in Codex's own auto-discovery dir, SKILL.md layout.
        assert (td / ".codex" / "skills" / "demo" / "SKILL.md").is_file()
        # Decision 4: Codex config is never written (no secret/config leak).
        assert not (td / ".codex" / "config.toml").exists()
        # No env leaked into the agent-notes config file either.
        assert not config_path(home=td).exists()
        # Manifest records the installed skill and no env/plugin.
        man = json.loads((td / ".codex" / ".agent-notes-harness.json").read_text())
        assert man["skills"] == ["demo"]
        assert man["env_keys"] == []
        assert man["plugin"] is False


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
            "install-harness", "codex", "--dry-run",
            "--source", str(src), "--home", str(td), "--json",
            env=_SUITE_ENV, check=False,
        )
        assert result.returncode == 2
        assert not (td / ".codex").exists()


def test_codex_uninstall_removes_skills_and_manifest():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_skill_tree(td)
        _run(
            "install-harness", "codex", "--source", str(src), "--home", str(td), "--json",
            env=_SUITE_ENV, check=False,
        )
        # A user's own skill must survive uninstall (only tracked files removed).
        user_skill = td / ".codex" / "skills" / "mine" / "SKILL.md"
        user_skill.parent.mkdir(parents=True)
        user_skill.write_text("---\nname: mine\n---\nkeep me\n")

        result = _run(
            "install-harness", "codex", "--uninstall", "--home", str(td), "--json",
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert not (td / ".codex" / "skills" / "demo").exists()
        assert not (td / ".codex" / ".agent-notes-harness.json").exists()
        assert user_skill.is_file()  # untracked user skill preserved


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
        _cfg.write_text(
            json.dumps({"regista": {"dsn": "postgresql://user-set/x"}})
        )
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
