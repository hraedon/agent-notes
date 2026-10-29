"""Tests for the canonical actor boundary."""

from __future__ import annotations

import pytest

from agent_notes.core.actor import (
    Actor,
    ActorConfigurationError,
    load_actor_config,
    migration_actor,
    resolve_actor,
    validate_actor,
)


@pytest.fixture(autouse=True)
def _clean_actor_env(monkeypatch, tmp_path):
    for name in (
        "AGENT_NOTES_ACTOR_ID",
        "REGISTA_PRINCIPAL_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    suite_env = tmp_path / "suite.env"
    monkeypatch.setenv("AGENT_SUITE_CONFIG", str(suite_env))
    monkeypatch.setenv("AGENT_SUITE_SYSTEM_CONFIG", str(suite_env))


def test_tool_actor_wins_over_suite_principal(monkeypatch):
    monkeypatch.setenv("REGISTA_PRINCIPAL_ID", "human:operator")
    monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:worker")

    actor = resolve_actor()

    assert actor.actor_id == "agent:worker"
    assert actor.actor_kind == "agent"


def test_suite_principal_is_used_when_tool_actor_is_absent(monkeypatch):
    monkeypatch.setenv("REGISTA_PRINCIPAL_ID", "human:operator")

    actor = resolve_actor()

    assert actor.actor_id == "human:operator"
    assert actor.actor_kind == "human"


def test_service_principal_maps_to_system_actor(monkeypatch):
    monkeypatch.setenv("REGISTA_PRINCIPAL_ID", "service:hooks")

    assert resolve_actor().actor_kind == "system"


def test_missing_actor_fails_closed():
    with pytest.raises(ActorConfigurationError, match="ACTOR_ID_NOT_CONFIGURED"):
        resolve_actor()


@pytest.mark.parametrize("actor_id", ["worker", "agent:", "human:bad space", "key:test"])
def test_noncanonical_actor_fails_closed(monkeypatch, actor_id):
    monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", actor_id)

    with pytest.raises(ActorConfigurationError):
        resolve_actor()


def test_actor_kind_must_match_principal_kind():
    with pytest.raises(ActorConfigurationError, match="requires actor_kind 'agent'"):
        validate_actor(Actor(actor_id="agent:worker", actor_kind="human"))


def test_actor_metadata_contains_no_producer_identity():
    actor = Actor(actor_id="agent:worker", actor_kind="agent")

    assert actor.actor_metadata() == {"display_name": "agent:worker", "role": "agent"}


def test_migration_actor_is_canonical_system_identity():
    actor = migration_actor()

    assert actor.actor_id == "service:agent-notes-migration"
    assert actor.actor_kind == "system"


def test_retired_delegation_paths_setting_is_ignored(monkeypatch, tmp_path):
    """Plan 026 D3 deleted action delegation (no host configured it): the old
    setting, even naming a missing file, no longer affects actor resolution."""
    monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:worker")
    monkeypatch.setenv("AGENT_NOTES_ACTION_DELEGATION_PATHS", str(tmp_path / "missing.json"))

    assert load_actor_config().actor_id == "agent:worker"
    assert resolve_actor().actor_id == "agent:worker"
