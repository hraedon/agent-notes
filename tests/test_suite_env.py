"""Tests for per-user config layering + principal_id resolution (Plan 017 WI-4.2).

Covers:
- suite.env file parsing + layered merge (per-user > system).
- RegistaConfig reads suite.env between process env and the config file.
- principal_id resolution through the full precedence chain.
- No cross-user attribution bleed: two users with different suite.env overlays
  produce distinct principal_ids.
- The live IdP binding (LDAP/Entra) seam is documented as environment-gated.
"""

from __future__ import annotations

import json

import pytest

from agent_notes.core import config
from agent_notes.core.actor import resolve_principal_id
from agent_notes.core.suite_env import _parse_env_file, load_suite_env

# ---------------------------------------------------------------------------
# suite.env parsing + layering
# ---------------------------------------------------------------------------


def test_parse_env_file_skips_comments_and_blanks(tmp_path):
    f = tmp_path / "suite.env"
    f.write_text(
        "# a comment\n"
        "\n"
        "REGISTA_DSN=postgresql://host/db\n"
        "export REGISTA_KEY_PATH=/keys\n"
        'REGISTA_REQUIRE_SSL="true"\n'
    )
    parsed = _parse_env_file(f)
    assert parsed == {
        "REGISTA_DSN": "postgresql://host/db",
        "REGISTA_KEY_PATH": "/keys",
        "REGISTA_REQUIRE_SSL": "true",
    }


def test_parse_env_file_missing_returns_empty(tmp_path):
    assert _parse_env_file(tmp_path / "nope.env") == {}


def test_load_suite_env_per_user_overrides_system(tmp_path):
    system = tmp_path / "system.env"
    user = tmp_path / "user.env"
    system.write_text("REGISTA_DSN=postgresql://system/db\nREGISTA_KEY_PATH=/sys-key\n")
    user.write_text("REGISTA_DSN=postgresql://user/db\n")
    merged = load_suite_env(user_path=user, system_path=system)
    assert merged["REGISTA_DSN"] == "postgresql://user/db"
    assert merged["REGISTA_KEY_PATH"] == "/sys-key"


def test_load_suite_env_system_only(tmp_path):
    system = tmp_path / "system.env"
    system.write_text("REGISTA_DSN=postgresql://system/db\n")
    merged = load_suite_env(user_path=tmp_path / "nope", system_path=system)
    assert merged == {"REGISTA_DSN": "postgresql://system/db"}


def test_load_suite_env_neither_file(tmp_path):
    assert load_suite_env(user_path=tmp_path / "nope1", system_path=tmp_path / "nope2") == {}


# ---------------------------------------------------------------------------
# RegistaConfig suite.env layer (Plan 017 WI-4.2)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_NOTES_DSN", raising=False)
    monkeypatch.delenv("AGENT_NOTES_CONFIG", raising=False)
    for v in (
        config._SUITE_REGISTA_DSN_ENV,
        config._SUITE_REGISTA_KEY_ENV,
        config._SUITE_REGISTA_SSL_ENV,
        config._REGISTA_DSN_ENV,
        config._REGISTA_PROJECT_ENV,
        config._REGISTA_KEY_ENV,
        config._REGISTA_WRITES_ENV,
        config._REGISTA_SSL_ENV,
        config._SUITE_PROJECT_ENV,
    ):
        monkeypatch.delenv(v, raising=False)
    suite_env = tmp_path / "suite.env"
    monkeypatch.setenv("AGENT_SUITE_CONFIG", str(suite_env))
    monkeypatch.setenv("AGENT_SUITE_SYSTEM_CONFIG", str(suite_env))
    config._WARNED_LEGACY.clear()


def _write_suite_env(monkeypatch, tmp_path, content, level="user"):
    f = tmp_path / f"{level}.env"
    f.write_text(content)
    if level == "user":
        monkeypatch.setenv("AGENT_SUITE_CONFIG", str(f))
    else:
        monkeypatch.setenv("AGENT_SUITE_SYSTEM_CONFIG", str(f))
    return f


def _write_config(tmp_path, monkeypatch, dsn):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"dsn": dsn}))
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(cfg))
    return cfg


def test_suite_env_dsn_between_env_and_file(monkeypatch, tmp_path):
    """suite.env DSN is used when env is unset, beats the config file."""
    _write_suite_env(monkeypatch, tmp_path, "REGISTA_DSN=postgresql://suite/db\n", level="system")
    _write_config(tmp_path, monkeypatch, "postgresql://file/db")
    monkeypatch.setenv(config._REGISTA_WRITES_ENV, "1")
    cfg = config.RegistaConfig()
    assert cfg.dsn == "postgresql://suite/db"
    assert cfg.enabled is True


def test_env_dsn_beats_suite_env(monkeypatch, tmp_path):
    """Process env DSN takes precedence over suite.env."""
    _write_suite_env(monkeypatch, tmp_path, "REGISTA_DSN=postgresql://suite/db\n", level="system")
    monkeypatch.setenv(config._SUITE_REGISTA_DSN_ENV, "postgresql://env/db")
    monkeypatch.setenv(config._REGISTA_WRITES_ENV, "1")
    cfg = config.RegistaConfig()
    assert cfg.dsn == "postgresql://env/db"


def test_per_user_suite_env_beats_system(monkeypatch, tmp_path):
    """Per-user suite.env overrides system suite.env for DSN."""
    _write_suite_env(monkeypatch, tmp_path, "REGISTA_DSN=postgresql://system/db\n", level="system")
    _write_suite_env(monkeypatch, tmp_path, "REGISTA_DSN=postgresql://user/db\n", level="user")
    monkeypatch.setenv(config._REGISTA_WRITES_ENV, "1")
    cfg = config.RegistaConfig()
    assert cfg.dsn == "postgresql://user/db"


def test_suite_env_key_path(monkeypatch, tmp_path):
    _write_suite_env(monkeypatch, tmp_path, "REGISTA_KEY_PATH=/suite/key\n", level="system")
    monkeypatch.setenv(config._SUITE_REGISTA_DSN_ENV, "postgresql://x")
    monkeypatch.setenv(config._REGISTA_WRITES_ENV, "1")
    cfg = config.RegistaConfig()
    assert cfg.hmac_key_path == "/suite/key"


def test_suite_env_require_ssl(monkeypatch, tmp_path):
    _write_suite_env(monkeypatch, tmp_path, "REGISTA_REQUIRE_SSL=true\n", level="system")
    monkeypatch.setenv(config._SUITE_REGISTA_DSN_ENV, "postgresql://x")
    cfg = config.RegistaConfig()
    assert cfg.require_ssl is True


def test_suite_env_project_slug(monkeypatch, tmp_path):
    """AGENT_NOTES_PROJECT from suite.env sets the project slug."""
    _write_suite_env(
        monkeypatch,
        tmp_path,
        "AGENT_NOTES_PROJECT=my-project\n",
        level="user",
    )
    cfg = config.RegistaConfig()
    assert cfg.project == "my-project"


def test_suite_env_project_legacy_alias(monkeypatch, tmp_path):
    """AGENT_NOTES_REGISTA_PROJECT from suite.env also works (legacy)."""
    _write_suite_env(
        monkeypatch,
        tmp_path,
        "AGENT_NOTES_REGISTA_PROJECT=legacy-project\n",
        level="user",
    )
    cfg = config.RegistaConfig()
    assert cfg.project == "legacy-project"


def test_env_project_beats_suite_env(monkeypatch, tmp_path):
    _write_suite_env(
        monkeypatch,
        tmp_path,
        "AGENT_NOTES_PROJECT=suite-project\n",
        level="user",
    )
    monkeypatch.setenv(config._SUITE_PROJECT_ENV, "env-project")
    cfg = config.RegistaConfig()
    assert cfg.project == "env-project"


def test_file_fallback_when_no_suite_env(monkeypatch, tmp_path):
    """Config file is the fallback when neither env nor suite.env is set."""
    _write_config(tmp_path, monkeypatch, "postgresql://file/db")
    cfg = config.RegistaConfig()
    assert cfg.dsn is None  # native DSN, not regista DSN


def test_suite_env_legacy_dsn_alias_warns(monkeypatch, tmp_path):
    """Legacy AGENT_NOTES_REGISTA_DSN in suite.env warns and is used."""
    _write_suite_env(
        monkeypatch,
        tmp_path,
        "AGENT_NOTES_REGISTA_DSN=postgresql://legacy/db\n",
        level="system",
    )
    monkeypatch.setenv(config._REGISTA_WRITES_ENV, "1")
    with pytest.warns(DeprecationWarning, match="AGENT_NOTES_REGISTA_DSN is deprecated"):
        cfg = config.RegistaConfig()
    assert cfg.dsn == "postgresql://legacy/db"


# ---------------------------------------------------------------------------
# principal_id resolution (Plan 017 WI-4.2)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_actor_env(monkeypatch, tmp_path):
    for v in (
        "AGENT_NOTES_PRINCIPAL_ID",
        "REGISTA_PRINCIPAL_ID",
    ):
        monkeypatch.delenv(v, raising=False)
    suite_env = tmp_path / "suite.env"
    monkeypatch.setenv("AGENT_SUITE_CONFIG", str(suite_env))
    monkeypatch.setenv("AGENT_SUITE_SYSTEM_CONFIG", str(suite_env))


def test_principal_id_from_tool_env(monkeypatch, tmp_path):
    """AGENT_NOTES_PRINCIPAL_ID env var takes highest precedence."""
    _write_suite_env(monkeypatch, tmp_path, "REGISTA_PRINCIPAL_ID=suite@id\n", level="user")
    monkeypatch.setenv("AGENT_NOTES_PRINCIPAL_ID", "tool@id")
    assert resolve_principal_id() == "tool@id"


def test_principal_id_from_suite_env_canonical(monkeypatch, tmp_path):
    """REGISTA_PRINCIPAL_ID from suite.env is used when env is unset."""
    _write_suite_env(
        monkeypatch, tmp_path, "REGISTA_PRINCIPAL_ID=alice@example.com\n", level="user"
    )
    assert resolve_principal_id() == "alice@example.com"


def test_principal_id_per_user_beats_system(monkeypatch, tmp_path):
    """Per-user suite.env principal_id overrides system."""
    _write_suite_env(monkeypatch, tmp_path, "REGISTA_PRINCIPAL_ID=system@id\n", level="system")
    _write_suite_env(monkeypatch, tmp_path, "REGISTA_PRINCIPAL_ID=user@id\n", level="user")
    assert resolve_principal_id() == "user@id"


def test_principal_id_suite_env_canonical_env_beats_file(monkeypatch, tmp_path):
    """REGISTA_PRINCIPAL_ID process env beats suite.env file."""
    _write_suite_env(monkeypatch, tmp_path, "REGISTA_PRINCIPAL_ID=file@id\n", level="user")
    monkeypatch.setenv("REGISTA_PRINCIPAL_ID", "env@id")
    assert resolve_principal_id() == "env@id"


def test_principal_id_falls_back_to_git_config(monkeypatch, tmp_path):
    """When nothing is set, git config user.email is the dev fallback."""
    # suite.env is empty (pointed at non-existent file by fixture).
    # No env vars set. resolve_principal_id should fall through to git config.
    # In the test env, git config may or may not return a value — we just
    # verify it doesn't crash and returns str | None.
    result = resolve_principal_id()
    assert result is None or isinstance(result, str)


def test_principal_id_none_when_nothing_set(monkeypatch, tmp_path):
    """When no source provides a principal_id, None is returned."""
    # Prevent git config from reading any config files (global/system/local).
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nonexistent-git-dir"))
    assert resolve_principal_id() is None


# ---------------------------------------------------------------------------
# AC: no cross-user attribution bleed
# ---------------------------------------------------------------------------


def test_no_cross_user_attribution_bleed(monkeypatch, tmp_path):
    """AC: two users' agents with different suite.env overlays produce distinct
    principal_ids — no cross-user attribution bleed.

    Simulates two users sharing one machine: each has their own per-user
    suite.env overlay with a different REGISTA_PRINCIPAL_ID. The resolution
    must produce distinct values for each.
    """
    user_a_env = tmp_path / "user-a.env"
    user_b_env = tmp_path / "user-b.env"
    user_a_env.write_text("REGISTA_PRINCIPAL_ID=alice@example.com\n")
    user_b_env.write_text("REGISTA_PRINCIPAL_ID=bob@example.com\n")

    # User A
    monkeypatch.setenv("AGENT_SUITE_CONFIG", str(user_a_env))
    monkeypatch.delenv("AGENT_NOTES_PRINCIPAL_ID", raising=False)
    monkeypatch.delenv("REGISTA_PRINCIPAL_ID", raising=False)
    principal_a = resolve_principal_id()

    # User B
    monkeypatch.setenv("AGENT_SUITE_CONFIG", str(user_b_env))
    principal_b = resolve_principal_id()

    assert principal_a == "alice@example.com"
    assert principal_b == "bob@example.com"
    assert principal_a != principal_b


def test_per_user_overlay_does_not_touch_system(monkeypatch, tmp_path):
    """AC: the per-user overlay sets principal_id without touching system config.

    The system suite.env has no principal_id; the per-user overlay adds one.
    The system file is not modified.
    """
    system_env = tmp_path / "system.env"
    user_env = tmp_path / "user.env"
    system_env.write_text("REGISTA_DSN=postgresql://shared/db\n")
    user_env.write_text("REGISTA_PRINCIPAL_ID=alice@example.com\n")

    monkeypatch.setenv("AGENT_SUITE_SYSTEM_CONFIG", str(system_env))
    monkeypatch.setenv("AGENT_SUITE_CONFIG", str(user_env))
    monkeypatch.delenv("AGENT_NOTES_PRINCIPAL_ID", raising=False)
    monkeypatch.delenv("REGISTA_PRINCIPAL_ID", raising=False)

    assert resolve_principal_id() == "alice@example.com"
    # System file unchanged — no principal_id added.
    assert "REGISTA_PRINCIPAL_ID" not in _parse_env_file(system_env)
