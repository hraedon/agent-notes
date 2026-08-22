"""Tests for the canonical v6 actor boundary."""

from __future__ import annotations

import json

import pytest

from agent_notes.core.actor import (
    Actor,
    ActorConfigurationError,
    DelegationConfigurationError,
    load_actor_config,
    migration_actor,
    resolve_actor,
    validate_actor,
)
from tests.conftest import shape_valid_delegation


@pytest.fixture(autouse=True)
def _clean_actor_env(monkeypatch, tmp_path):
    for name in (
        "AGENT_NOTES_ACTOR_ID",
        "REGISTA_PRINCIPAL_ID",
        "AGENT_NOTES_ACTION_DELEGATION_PATHS",
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


def test_delegation_paths_are_loaded_in_order(monkeypatch, tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps({"credential_id": "first"}), encoding="utf-8")
    second.write_text(json.dumps({"credential_id": "second"}), encoding="utf-8")
    monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:worker")
    monkeypatch.setenv(
        "AGENT_NOTES_ACTION_DELEGATION_PATHS",
        f"{first}:{second}",
    )

    config = load_actor_config()

    assert [item["credential_id"] for item in config.action_delegation_credentials] == [
        "first",
        "second",
    ]


def test_empty_delegation_path_list_is_rejected(monkeypatch):
    monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:worker")
    monkeypatch.setenv("AGENT_NOTES_ACTION_DELEGATION_PATHS", " : ")

    with pytest.raises(DelegationConfigurationError):
        resolve_actor()


def test_delegation_terminal_subject_must_match_the_actor(monkeypatch):
    """A delegation chain whose terminal subject is a different principal
    must be refused at the configuration edge, before any write queues it."""
    monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:worker")
    mismatched = Actor(
        actor_id="agent:worker",
        actor_kind="agent",
        action_delegation_credentials=(
            shape_valid_delegation(subject_principal_id="agent:someone-else"),
        ),
    )

    with pytest.raises(DelegationConfigurationError, match="terminal action-delegation subject"):
        validate_actor(mismatched)


def test_delegation_terminal_subject_mismatch_refused_by_the_face(monkeypatch):
    """The same refusal holds through the face's actor crack — the earliest
    boundary a queued outbox op would cross."""
    from agent_notes.core.regista_face import _crack

    monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:worker")
    mismatched = Actor(
        actor_id="agent:worker",
        actor_kind="agent",
        action_delegation_credentials=(
            shape_valid_delegation(subject_principal_id="agent:someone-else"),
        ),
    )

    with pytest.raises(DelegationConfigurationError, match="does not match actor_id"):
        _crack(mismatched)
