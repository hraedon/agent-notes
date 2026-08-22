"""Unit tests for core/config.py DSN resolution (no database required)."""

from __future__ import annotations

import json

import pytest

from agent_notes.core import config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_NOTES_DSN", raising=False)
    monkeypatch.delenv("AGENT_NOTES_CONFIG", raising=False)
    for v in (
        config._SUITE_REGISTA_DSN_ENV,
        config._SUITE_REGISTA_KEY_ENV,
        config._SUITE_REGISTA_SSL_ENV,
        config._REGISTA_WRITES_ENV,
        config._SUITE_PROJECT_ENV,
    ):
        monkeypatch.delenv(v, raising=False)
    # Isolate from host suite.env files (Plan 017 WI-4.2): point both paths
    # at a non-existent file so tests control their own suite.env content.
    suite_env = tmp_path / "suite.env"
    monkeypatch.setenv("AGENT_SUITE_CONFIG", str(suite_env))
    monkeypatch.setenv("AGENT_SUITE_SYSTEM_CONFIG", str(suite_env))


def _write_config(tmp_path, monkeypatch, dsn):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"dsn": dsn}))
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(cfg))
    return cfg


def test_explicit_arg_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NOTES_DSN", "postgresql://env/x")
    _write_config(tmp_path, monkeypatch, "postgresql://file/x")
    assert config.resolve_dsn("postgresql://explicit/x") == "postgresql://explicit/x"


def test_env_overrides_file(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NOTES_DSN", "postgresql://env/x")
    _write_config(tmp_path, monkeypatch, "postgresql://file/x")
    assert config.resolve_dsn() == "postgresql://env/x"


def test_file_fallback_when_env_absent(monkeypatch, tmp_path):
    _write_config(tmp_path, monkeypatch, "postgresql://file/x")
    assert config.resolve_dsn() == "postgresql://file/x"


def test_raises_when_nothing_set(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(tmp_path / "missing.json"))
    with pytest.raises(RuntimeError, match="No Postgres DSN found"):
        config.resolve_dsn()


# ---------------------------------------------------------------------------
# Native DSN suite.env layer (WI-051): process env > per-user suite.env >
# system suite.env > config file, per bootstrap-contract §2.
# ---------------------------------------------------------------------------


def _write_native_suite_env(monkeypatch, tmp_path, dsn, level="system"):
    f = tmp_path / f"{level}-suite.env"
    f.write_text(f"AGENT_NOTES_DSN={dsn}\n")
    var = "AGENT_SUITE_CONFIG" if level == "user" else "AGENT_SUITE_SYSTEM_CONFIG"
    monkeypatch.setenv(var, str(f))
    return f


def test_native_dsn_from_system_suite_env(monkeypatch, tmp_path):
    """AGENT_NOTES_DSN in /etc-level suite.env satisfies resolve_dsn (WI-051).

    This is the Lane C qualification scenario: the bootstrap writes the DSN
    into the system suite.env and nothing exports it into the process env.
    """
    _write_native_suite_env(monkeypatch, tmp_path, "postgresql://system-suite/db")
    assert config.resolve_dsn() == "postgresql://system-suite/db"


def test_native_dsn_user_suite_env_beats_system(monkeypatch, tmp_path):
    _write_native_suite_env(monkeypatch, tmp_path, "postgresql://system-suite/db")
    _write_native_suite_env(monkeypatch, tmp_path, "postgresql://user-suite/db", level="user")
    assert config.resolve_dsn() == "postgresql://user-suite/db"


def test_native_dsn_process_env_beats_suite_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NOTES_DSN", "postgresql://env/x")
    _write_native_suite_env(monkeypatch, tmp_path, "postgresql://system-suite/db")
    assert config.resolve_dsn() == "postgresql://env/x"


def test_native_dsn_suite_env_beats_config_file(monkeypatch, tmp_path):
    _write_native_suite_env(monkeypatch, tmp_path, "postgresql://system-suite/db")
    _write_config(tmp_path, monkeypatch, "postgresql://file/x")
    assert config.resolve_dsn() == "postgresql://system-suite/db"


def test_missing_dsn_error_names_the_searched_files(monkeypatch, tmp_path):
    """The failure message names every file that was searched (WI-051)."""
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(tmp_path / "missing.json"))
    user_env = tmp_path / "user-suite.env"
    system_env = tmp_path / "system-suite.env"
    monkeypatch.setenv("AGENT_SUITE_CONFIG", str(user_env))
    monkeypatch.setenv("AGENT_SUITE_SYSTEM_CONFIG", str(system_env))
    with pytest.raises(RuntimeError) as exc_info:
        config.resolve_dsn()
    message = exc_info.value.args[0]
    assert str(user_env) in message
    assert str(system_env) in message
    assert str(tmp_path / "missing.json") in message


def test_regista_dsn_does_not_fallback_to_native(monkeypatch, tmp_path):
    """F-5: REGISTA_DSN must NOT satisfy the native agent-notes DSN.

    The deployment model uses separate regista and agent-notes databases.
    A missing AGENT_NOTES_DSN must be an actionable failure, not a silent
    connection to the regista database.
    """
    monkeypatch.setenv(config._SUITE_REGISTA_DSN_ENV, "postgresql://regista/db")
    monkeypatch.setenv("AGENT_NOTES_REGISTA_DSN", "postgresql://legacy-regista/db")
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(tmp_path / "missing.json"))
    with pytest.raises(RuntimeError, match="No Postgres DSN found") as exc_info:
        config.resolve_dsn()
    assert "REGISTA_DSN" not in exc_info.value.args[0]
    assert "AGENT_NOTES_REGISTA_DSN" not in exc_info.value.args[0]


def test_removed_regista_dsn_alone_does_not_satisfy_native(monkeypatch, tmp_path):
    """The removed regista DSN name cannot satisfy the native DSN."""
    monkeypatch.setenv("AGENT_NOTES_REGISTA_DSN", "postgresql://legacy-regista/db")
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(tmp_path / "missing.json"))
    with pytest.raises(RuntimeError, match="No Postgres DSN found") as exc_info:
        config.resolve_dsn()
    assert "AGENT_NOTES_REGISTA_DSN" not in exc_info.value.args[0]
    assert "REGISTA_DSN" not in exc_info.value.args[0]


def test_malformed_config_raises(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{not valid json")
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(cfg))
    with pytest.raises(RuntimeError, match="could not be read"):
        config.resolve_dsn()


def test_config_path_honors_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(tmp_path / "custom.json"))
    assert config.config_path() == tmp_path / "custom.json"


# ---------------------------------------------------------------------------
# RegistaConfig file fallback (Plan 012 WI-1)
# ---------------------------------------------------------------------------


def _write_regista_config(tmp_path, monkeypatch, regista, native="postgresql://native/x"):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"dsn": native, "regista": regista}))
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(cfg))
    return cfg


def test_regista_env_wins_over_file(monkeypatch, tmp_path):
    monkeypatch.setenv(config._SUITE_REGISTA_DSN_ENV, "postgresql://env/x")
    monkeypatch.setenv(config._SUITE_REGISTA_KEY_ENV, "/env/key")
    monkeypatch.setenv(config._REGISTA_WRITES_ENV, "1")
    _write_regista_config(
        tmp_path,
        monkeypatch,
        {"dsn": "postgresql://file/x", "key_path": "/file/key", "writes_enabled": False},
    )
    cfg = config.RegistaConfig()
    assert cfg.dsn == "postgresql://env/x"
    assert cfg.key_path == "/env/key"
    assert cfg.writes_enabled is True
    assert cfg.enabled is True


def test_regista_file_fallback_when_env_absent(monkeypatch, tmp_path):
    _write_regista_config(
        tmp_path,
        monkeypatch,
        {
            "dsn": "postgresql://file/x",
            "key_path": "/file/key",
            "writes_enabled": True,
            "require_ssl": True,
        },
    )
    cfg = config.RegistaConfig()
    assert cfg.dsn == "postgresql://file/x"
    assert cfg.key_path == "/file/key"
    assert cfg.require_ssl is True
    assert cfg.writes_enabled is True
    assert cfg.enabled is True


def test_regista_file_gate_off(monkeypatch, tmp_path):
    # writes_enabled missing or False -> enabled stays False even with a DSN.
    for gate_val in (None, False):
        regista = {"dsn": "postgresql://file/x"}
        if gate_val is not None:
            regista["writes_enabled"] = gate_val
        _write_regista_config(tmp_path, monkeypatch, regista)
        cfg = config.RegistaConfig()
        assert cfg.dsn == "postgresql://file/x"
        assert cfg.writes_enabled is False
        assert cfg.enabled is False


def test_regista_disabled_when_nothing_set(monkeypatch, tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"dsn": "postgresql://native/x"}))
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(cfg_file))
    cfg = config.RegistaConfig()
    assert cfg.dsn is None
    assert cfg.key_path is None
    assert cfg.enabled is False


def test_regista_malformed_file_does_not_crash(monkeypatch, tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text("{not valid json")
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(cfg_file))
    cfg = config.RegistaConfig()  # must not raise
    assert cfg.dsn is None
    assert cfg.enabled is False


def test_regista_project_stays_default_from_file_ignored(monkeypatch, tmp_path):
    _write_regista_config(
        tmp_path,
        monkeypatch,
        {"dsn": "postgresql://file/x", "writes_enabled": True, "project": "should_be_ignored"},
    )
    cfg = config.RegistaConfig()
    assert cfg.project == config._REGISTA_PROJECT_DEFAULT


def test_regista_file_missing_entirely(monkeypatch, tmp_path):
    # No config file at all -> regista disabled, no crash.
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(tmp_path / "missing.json"))
    cfg = config.RegistaConfig()
    assert cfg.dsn is None
    assert cfg.enabled is False


def test_regista_empty_env_falls_through_to_file(monkeypatch, tmp_path):
    # An explicitly-empty env var is treated as unset (mirrors resolve_dsn):
    # the file value wins.
    monkeypatch.setenv(config._SUITE_REGISTA_DSN_ENV, "")
    monkeypatch.setenv(config._SUITE_REGISTA_KEY_ENV, "")
    _write_regista_config(
        tmp_path,
        monkeypatch,
        {"dsn": "postgresql://file/x", "key_path": "/file/key", "writes_enabled": True},
    )
    cfg = config.RegistaConfig()
    assert cfg.dsn == "postgresql://file/x"
    assert cfg.key_path == "/file/key"
    assert cfg.enabled is True


def test_regista_env_disables_overrides_file_enable(monkeypatch, tmp_path):
    # Env gate "0" must win over file writes_enabled:true.
    monkeypatch.setenv(config._SUITE_REGISTA_DSN_ENV, "postgresql://env/x")
    monkeypatch.setenv(config._REGISTA_WRITES_ENV, "0")
    _write_regista_config(
        tmp_path, monkeypatch, {"dsn": "postgresql://file/x", "writes_enabled": True}
    )
    cfg = config.RegistaConfig()
    assert cfg.dsn == "postgresql://env/x"
    assert cfg.writes_enabled is False
    assert cfg.enabled is False


def test_regista_file_string_false_is_not_truthy(monkeypatch, tmp_path):
    # JSON string "false" must NOT enable the flag (bool("false") would be True).
    _write_regista_config(
        tmp_path,
        monkeypatch,
        {
            "dsn": "postgresql://file/x",
            "writes_enabled": "false",
            "require_ssl": "true",
        },
    )
    cfg = config.RegistaConfig()
    assert cfg.writes_enabled is False
    assert cfg.enabled is False
    assert cfg.require_ssl is True


def test_regista_file_require_ssl_bool_and_string(monkeypatch, tmp_path):
    _write_regista_config(
        tmp_path, monkeypatch, {"dsn": "postgresql://file/x", "require_ssl": True}
    )
    assert config.RegistaConfig().require_ssl is True
    _write_regista_config(
        tmp_path, monkeypatch, {"dsn": "postgresql://file/x", "require_ssl": "no"}
    )
    assert config.RegistaConfig().require_ssl is False


def test_regista_nondict_toplevel_json_does_not_crash(monkeypatch, tmp_path):
    for bad in ("[]", '"a string"', "42"):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(bad)
        monkeypatch.setenv("AGENT_NOTES_CONFIG", str(cfg_file))
        cfg = config.RegistaConfig()  # must not raise
        assert cfg.dsn is None
        assert cfg.enabled is False


def test_regista_nondict_regista_block_ignored(monkeypatch, tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"dsn": "postgresql://native/x", "regista": ["nope"]}))
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(cfg_file))
    cfg = config.RegistaConfig()
    assert cfg.dsn is None
    assert cfg.enabled is False


def test_regista_file_nonstring_dsn_coerced_to_none(monkeypatch, tmp_path):
    _write_regista_config(tmp_path, monkeypatch, {"dsn": 123, "writes_enabled": True})
    cfg = config.RegistaConfig()
    assert cfg.dsn is None
    assert cfg.enabled is False


# ---------------------------------------------------------------------------
# Suite config adoption — canonical REGISTA_* precedence (Plan 017 WI-1.1)
# ---------------------------------------------------------------------------


def test_canonical_env_wins_over_file(monkeypatch, tmp_path):
    _write_regista_config(
        tmp_path,
        monkeypatch,
        {"dsn": "postgresql://file/x", "key_path": "/file/key", "writes_enabled": True},
    )
    monkeypatch.setenv(config._SUITE_REGISTA_DSN_ENV, "postgresql://canonical/x")
    monkeypatch.setenv(config._SUITE_REGISTA_KEY_ENV, "/canonical/key")
    cfg = config.RegistaConfig()
    assert cfg.dsn == "postgresql://canonical/x"
    assert cfg.key_path == "/canonical/key"
    assert cfg.enabled is True


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AGENT_NOTES_REGISTA_DSN", "postgresql://old/x"),
        ("AGENT_NOTES_REGISTA_HMAC_KEY_PATH", "/old/key"),
        ("AGENT_NOTES_REGISTA_REQUIRE_SSL", "true"),
        ("AGENT_NOTES_REGISTA_PROJECT", "old-project"),
    ],
)
def test_removed_env_names_do_not_affect_resolution(monkeypatch, tmp_path, name, value):
    """The removed AGENT_NOTES_REGISTA_* names are not runtime aliases."""
    _write_regista_config(
        tmp_path,
        monkeypatch,
        {
            "dsn": "postgresql://file/x",
            "key_path": "/file/key",
            "require_ssl": False,
            "writes_enabled": True,
        },
    )
    monkeypatch.setenv(name, value)
    cfg = config.RegistaConfig()
    assert cfg.dsn == "postgresql://file/x"
    assert cfg.key_path == "/file/key"
    assert cfg.require_ssl is False
    assert cfg.project == config._REGISTA_PROJECT_DEFAULT
    assert cfg.enabled is True


def test_suite_env_only_scenario(monkeypatch):
    """The CLI operates using only canonical suite vars."""
    monkeypatch.setenv(config._SUITE_REGISTA_DSN_ENV, "postgresql://suite/x")
    monkeypatch.setenv(config._SUITE_REGISTA_KEY_ENV, "/suite/key")
    monkeypatch.setenv(config._SUITE_REGISTA_SSL_ENV, "true")
    monkeypatch.setenv(config._REGISTA_WRITES_ENV, "1")
    cfg = config.RegistaConfig()
    assert cfg.dsn == "postgresql://suite/x"
    assert cfg.key_path == "/suite/key"
    assert cfg.require_ssl is True
    assert cfg.enabled is True


def test_canonical_empty_falls_through_to_file(monkeypatch, tmp_path):
    """An empty canonical env var is treated as unset."""
    _write_regista_config(
        tmp_path,
        monkeypatch,
        {"dsn": "postgresql://file/x", "key_path": "/file/key", "writes_enabled": True},
    )
    monkeypatch.setenv(config._SUITE_REGISTA_DSN_ENV, "")
    monkeypatch.setenv(config._REGISTA_WRITES_ENV, "1")
    cfg = config.RegistaConfig()
    assert cfg.dsn == "postgresql://file/x"


def test_writes_gate_and_project_use_canonical_names(monkeypatch):
    """The write gate and project use their canonical tool-specific names."""
    monkeypatch.setenv(config._SUITE_REGISTA_DSN_ENV, "postgresql://x")
    monkeypatch.setenv(config._SUITE_PROJECT_ENV, "my-project-slug")
    monkeypatch.setenv(config._REGISTA_WRITES_ENV, "1")
    cfg = config.RegistaConfig()
    assert cfg.project == "my-project-slug"
    assert cfg.writes_enabled is True


def test_removed_config_key_name_is_ignored(monkeypatch, tmp_path):
    """The old config.json key does not populate the canonical key_path field."""
    _write_regista_config(
        tmp_path,
        monkeypatch,
        {
            "dsn": "postgresql://file/x",
            "hmac_key_path": "/old/key",
            "writes_enabled": True,
        },
    )
    cfg = config.RegistaConfig()
    assert cfg.key_path is None
    assert not hasattr(cfg, "hmac_key_path")
