"""Unit tests for core/config.py DSN resolution (no database required)."""

from __future__ import annotations

import json

import pytest

from agent_notes.core import config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
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
    ):
        monkeypatch.delenv(v, raising=False)
    # Reset the one-shot deprecation-warning guard so each test can observe it.
    config._WARNED_LEGACY.clear()


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
    monkeypatch.setenv(config._REGISTA_DSN_ENV, "postgresql://env/x")
    monkeypatch.setenv(config._REGISTA_KEY_ENV, "/env/key")
    monkeypatch.setenv(config._REGISTA_WRITES_ENV, "1")
    _write_regista_config(
        tmp_path,
        monkeypatch,
        {"dsn": "postgresql://file/x", "hmac_key_path": "/file/key", "writes_enabled": False},
    )
    cfg = config.RegistaConfig()
    assert cfg.dsn == "postgresql://env/x"
    assert cfg.hmac_key_path == "/env/key"
    assert cfg.writes_enabled is True
    assert cfg.enabled is True


def test_regista_file_fallback_when_env_absent(monkeypatch, tmp_path):
    _write_regista_config(
        tmp_path,
        monkeypatch,
        {
            "dsn": "postgresql://file/x",
            "hmac_key_path": "/file/key",
            "writes_enabled": True,
            "require_ssl": True,
        },
    )
    cfg = config.RegistaConfig()
    assert cfg.dsn == "postgresql://file/x"
    assert cfg.hmac_key_path == "/file/key"
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
    assert cfg.hmac_key_path is None
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
    monkeypatch.setenv(config._REGISTA_DSN_ENV, "")
    monkeypatch.setenv(config._REGISTA_KEY_ENV, "")
    _write_regista_config(
        tmp_path,
        monkeypatch,
        {"dsn": "postgresql://file/x", "hmac_key_path": "/file/key", "writes_enabled": True},
    )
    cfg = config.RegistaConfig()
    assert cfg.dsn == "postgresql://file/x"
    assert cfg.hmac_key_path == "/file/key"
    assert cfg.enabled is True


def test_regista_env_disables_overrides_file_enable(monkeypatch, tmp_path):
    # Env gate "0" must win over file writes_enabled:true.
    monkeypatch.setenv(config._REGISTA_DSN_ENV, "postgresql://env/x")
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


def test_canonical_dsn_wins_over_legacy_alias(monkeypatch):
    """REGISTA_DSN takes precedence over the AGENT_NOTES_REGISTA_DSN alias."""
    monkeypatch.setenv(config._SUITE_REGISTA_DSN_ENV, "postgresql://canonical/x")
    monkeypatch.setenv(config._REGISTA_DSN_ENV, "postgresql://legacy/x")
    monkeypatch.setenv(config._REGISTA_WRITES_ENV, "1")
    cfg = config.RegistaConfig()
    assert cfg.dsn == "postgresql://canonical/x"
    assert cfg.enabled is True


def test_canonical_key_wins_over_legacy_alias(monkeypatch):
    monkeypatch.setenv(config._SUITE_REGISTA_KEY_ENV, "/canonical/key")
    monkeypatch.setenv(config._REGISTA_KEY_ENV, "/legacy/key")
    monkeypatch.setenv(config._SUITE_REGISTA_DSN_ENV, "postgresql://x")
    monkeypatch.setenv(config._REGISTA_WRITES_ENV, "1")
    cfg = config.RegistaConfig()
    assert cfg.hmac_key_path == "/canonical/key"


def test_canonical_ssl_wins_over_legacy_alias(monkeypatch):
    monkeypatch.setenv(config._SUITE_REGISTA_SSL_ENV, "true")
    monkeypatch.setenv(config._REGISTA_SSL_ENV, "false")
    monkeypatch.setenv(config._SUITE_REGISTA_DSN_ENV, "postgresql://x")
    cfg = config.RegistaConfig()
    assert cfg.require_ssl is True


def test_legacy_dsn_falls_back_with_deprecation_warning(monkeypatch):
    """When only the legacy alias is set, it is used and a DeprecationWarning fires."""
    monkeypatch.setenv(config._REGISTA_DSN_ENV, "postgresql://legacy/x")
    monkeypatch.setenv(config._REGISTA_WRITES_ENV, "1")
    with pytest.warns(DeprecationWarning, match="AGENT_NOTES_REGISTA_DSN is deprecated"):
        cfg = config.RegistaConfig()
    assert cfg.dsn == "postgresql://legacy/x"
    assert cfg.enabled is True


def test_legacy_key_falls_back_with_deprecation_warning(monkeypatch):
    monkeypatch.setenv(config._SUITE_REGISTA_DSN_ENV, "postgresql://x")
    monkeypatch.setenv(config._REGISTA_KEY_ENV, "/legacy/key")
    monkeypatch.setenv(config._REGISTA_WRITES_ENV, "1")
    with pytest.warns(DeprecationWarning, match="AGENT_NOTES_REGISTA_HMAC_KEY_PATH is deprecated"):
        cfg = config.RegistaConfig()
    assert cfg.hmac_key_path == "/legacy/key"


def test_legacy_warning_fires_once_per_process(monkeypatch):
    """The one-shot guard suppresses repeat warnings for the same alias."""
    monkeypatch.setenv(config._REGISTA_DSN_ENV, "postgresql://legacy/x")
    monkeypatch.setenv(config._REGISTA_WRITES_ENV, "1")
    with pytest.warns(DeprecationWarning):
        config.RegistaConfig()
    # Second call must NOT re-warn (guard already spent for this alias).
    import warnings as _w

    with _w.catch_warnings(record=True) as caught:
        _w.simplefilter("always")
        config.RegistaConfig()
    assert not any(issubclass(w.category, DeprecationWarning) for w in caught)


def test_canonical_env_wins_over_file(monkeypatch, tmp_path):
    _write_regista_config(
        tmp_path,
        monkeypatch,
        {"dsn": "postgresql://file/x", "hmac_key_path": "/file/key", "writes_enabled": True},
    )
    monkeypatch.setenv(config._SUITE_REGISTA_DSN_ENV, "postgresql://canonical/x")
    monkeypatch.setenv(config._SUITE_REGISTA_KEY_ENV, "/canonical/key")
    cfg = config.RegistaConfig()
    assert cfg.dsn == "postgresql://canonical/x"
    assert cfg.hmac_key_path == "/canonical/key"
    assert cfg.enabled is True


def test_legacy_env_wins_over_file(monkeypatch, tmp_path):
    """Legacy alias still beats the file value (just with a warning)."""
    _write_regista_config(
        tmp_path,
        monkeypatch,
        {"dsn": "postgresql://file/x", "hmac_key_path": "/file/key", "writes_enabled": True},
    )
    monkeypatch.setenv(config._REGISTA_DSN_ENV, "postgresql://legacy/x")
    monkeypatch.setenv(config._REGISTA_KEY_ENV, "/legacy/key")
    with pytest.warns(DeprecationWarning):
        cfg = config.RegistaConfig()
    assert cfg.dsn == "postgresql://legacy/x"
    assert cfg.hmac_key_path == "/legacy/key"
    assert cfg.enabled is True


def test_suite_env_only_scenario(monkeypatch):
    """AC: the CLI operates reading only the canonical suite vars (no legacy)."""
    monkeypatch.setenv(config._SUITE_REGISTA_DSN_ENV, "postgresql://suite/x")
    monkeypatch.setenv(config._SUITE_REGISTA_KEY_ENV, "/suite/key")
    monkeypatch.setenv(config._SUITE_REGISTA_SSL_ENV, "true")
    monkeypatch.setenv(config._REGISTA_WRITES_ENV, "1")
    import warnings as _w

    with _w.catch_warnings(record=True) as caught:
        _w.simplefilter("always")
        cfg = config.RegistaConfig()
    assert cfg.dsn == "postgresql://suite/x"
    assert cfg.hmac_key_path == "/suite/key"
    assert cfg.require_ssl is True
    assert cfg.enabled is True
    # No deprecation warning when only canonical vars are used.
    assert not any(issubclass(w.category, DeprecationWarning) for w in caught)


def test_canonical_empty_falls_through_to_legacy(monkeypatch):
    """An empty canonical env var is treated as unset (mirrors resolve_dsn)."""
    monkeypatch.setenv(config._SUITE_REGISTA_DSN_ENV, "")
    monkeypatch.setenv(config._REGISTA_DSN_ENV, "postgresql://legacy/x")
    monkeypatch.setenv(config._REGISTA_WRITES_ENV, "1")
    with pytest.warns(DeprecationWarning):
        cfg = config.RegistaConfig()
    assert cfg.dsn == "postgresql://legacy/x"


def test_writes_gate_and_project_keep_tool_specific_names(monkeypatch):
    """AGENT_NOTES_REGISTA_WRITES and _PROJECT are tool-specific and keep their names."""
    monkeypatch.setenv(config._SUITE_REGISTA_DSN_ENV, "postgresql://x")
    monkeypatch.setenv(config._REGISTA_PROJECT_ENV, "my-project-slug")
    monkeypatch.setenv(config._REGISTA_WRITES_ENV, "1")
    cfg = config.RegistaConfig()
    assert cfg.project == "my-project-slug"
    assert cfg.writes_enabled is True
