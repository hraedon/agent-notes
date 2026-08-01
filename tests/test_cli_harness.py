"""Tests for the shared hermetic CLI-subprocess harness (WI-030).

These pin the contract that keeps ``test_cli.py`` and
``test_memory_provider_cli.py`` isolated from the operator's environment and
config-file discovery. They are subprocess-free and need no database.
"""

from __future__ import annotations

from tests.cli_harness import build_cli_env, discovery_pins


def test_build_cli_env_strips_named_keys(monkeypatch):
    monkeypatch.setenv("AGENT_NOTES_LEAK_SENTINEL", "do-not-leak")
    stripped = build_cli_env(strip_keys=("AGENT_NOTES_LEAK_SENTINEL",))
    assert "AGENT_NOTES_LEAK_SENTINEL" not in stripped
    kept = build_cli_env()
    assert kept["AGENT_NOTES_LEAK_SENTINEL"] == "do-not-leak"


def test_discovery_pins_override_caller_env():
    """Pins are applied last, so a caller env dict cannot undo the hermeticity."""
    merged = build_cli_env(
        env={
            "AGENT_SUITE_CONFIG": "/operator/real/suite.env",
            "XDG_CONFIG_HOME": "/operator/.config",
        }
    )
    pins = discovery_pins()
    assert merged["AGENT_SUITE_CONFIG"] == pins["AGENT_SUITE_CONFIG"]
    assert merged["XDG_CONFIG_HOME"] == pins["XDG_CONFIG_HOME"]
    assert merged["AGENT_SUITE_CONFIG"] != "/operator/real/suite.env"


def test_discovery_pins_do_not_override_home():
    """HOME is left intact so user site-packages (e.g. regista) stay importable.

    See ``discovery_pins`` for the rationale: config discovery is fully covered
    by the override vars plus ``XDG_CONFIG_HOME``, so pinning HOME would only
    break ``~/.local`` package resolution.
    """
    pins = discovery_pins()
    assert "HOME" not in pins
    assert "XDG_CONFIG_HOME" in pins
    assert "AGENT_SUITE_CONFIG" in pins
    assert "AGENT_NOTES_CONFIG" in pins
