"""Contract tests for the public, read-only knowledge provider."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from agent_notes.knowledge_provider import (
    SCHEMA_VERSION,
    AgentNotesKnowledgeProvider,
    KnowledgeProvider,
    KnowledgeScope,
    KnowledgeState,
    SearchMode,
)


@pytest.fixture
def scope(monkeypatch) -> KnowledgeScope:
    from agent_notes.core import db, memory_model

    workspace = SimpleNamespace(id=7, slug="shared", name="Shared")
    project = SimpleNamespace(id=11, workspace_id=7, slug="alpha", name="Alpha")
    monkeypatch.setattr(db, "list_workspaces", lambda: [workspace])
    monkeypatch.setattr(db, "list_projects", lambda workspace_id=None: [project])
    monkeypatch.setattr(memory_model, "knowledge_index_health", lambda *_: _healthy_index())
    return KnowledgeScope("shared", "alpha")


def _healthy_index() -> dict:
    return {
        "total": 1,
        "vector_indexed": 1,
        "pending_sync": 0,
        "signed": 0,
        "latest_update": datetime(2026, 7, 12, tzinfo=timezone.utc),
    }


def _row(**overrides) -> dict:
    row = {
        "id": 3,
        "workspace_id": 7,
        "project_id": 11,
        "name": "deployment-notes",
        "memory_type": "note",
        "body": "Deploy through the approved release path.",
        "body_preview": "Deploy through the approved release path.",
        "attributes": {"reviewed_at": datetime(2026, 7, 1, tzinfo=timezone.utc)},
        "regista_note_id": None,
        "pending_sync": False,
        "created_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 7, 12, tzinfo=timezone.utc),
    }
    row.update(overrides)
    return row


def test_provider_satisfies_public_protocol() -> None:
    assert isinstance(AgentNotesKnowledgeProvider(), KnowledgeProvider)


def test_browse_is_versioned_json_safe_and_paginated(monkeypatch, scope) -> None:
    from agent_notes.core import memory_model

    rows = [_row(name="one"), _row(name="two")]
    monkeypatch.setattr(memory_model, "browse_knowledge", lambda *a, **kw: rows)

    result = AgentNotesKnowledgeProvider().browse(scope, limit=1)
    payload = result.to_dict()

    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["status"]["state"] == "current"
    assert payload["items"][0]["name"] == "one"
    assert payload["next_cursor"] == "1"
    json.dumps(payload)


def test_detail_returns_full_body_and_native_authority(monkeypatch, scope) -> None:
    from agent_notes.core import memory_model

    monkeypatch.setattr(memory_model, "get_knowledge_memory", lambda *a: _row())
    result = AgentNotesKnowledgeProvider().detail(scope, "deployment-notes")

    assert result.item is not None
    assert result.item.body == "Deploy through the approved release path."
    assert result.item.authority == "agent-notes-native"
    assert result.item.authority_state is KnowledgeState.CURRENT


def test_signed_projection_does_not_claim_verified_authority(monkeypatch, scope) -> None:
    from agent_notes.core import memory_model

    note_id = UUID("12345678-1234-5678-1234-567812345678")
    monkeypatch.setattr(
        memory_model, "get_knowledge_memory", lambda *a: _row(regista_note_id=note_id)
    )

    result = AgentNotesKnowledgeProvider().detail(scope, "deployment-notes")

    assert result.status.state is KnowledgeState.UNKNOWN
    assert result.item is not None
    assert result.item.entity_ref == str(note_id)
    assert result.item.authority == "regista"


def test_pending_and_current_browse_is_partial(monkeypatch, scope) -> None:
    from agent_notes.core import memory_model

    monkeypatch.setattr(
        memory_model,
        "browse_knowledge",
        lambda *a, **kw: [_row(name="current"), _row(name="pending", pending_sync=True)],
    )
    result = AgentNotesKnowledgeProvider().browse(scope)

    assert result.status.state is KnowledgeState.PARTIAL
    assert "await authority synchronization" in " ".join(result.status.findings)


def test_lexical_search_does_not_call_embedding(monkeypatch, scope) -> None:
    from agent_notes.core import embed as embed_module
    from agent_notes.core import memory_model

    calls = []
    monkeypatch.setattr(embed_module, "embed", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(memory_model, "search_memories_exact", lambda *a, **kw: [_row()])

    result = AgentNotesKnowledgeProvider().search(scope, "deploy", mode=SearchMode.LEXICAL)

    assert result.status.state is KnowledgeState.CURRENT
    assert calls == []


def test_semantic_search_lazily_embeds_and_reports_incomplete_index(monkeypatch, scope) -> None:
    from agent_notes.core import embed as embed_module
    from agent_notes.core import memory_model

    vector = SimpleNamespace(tolist=lambda: [0.0, 1.0])
    calls = []
    monkeypatch.setattr(embed_module, "embed", lambda *a, **kw: calls.append((a, kw)) or vector)
    monkeypatch.setattr(
        memory_model,
        "search_knowledge_semantic",
        lambda *a, **kw: [_row(score=0.8)],
    )
    monkeypatch.setattr(
        memory_model,
        "knowledge_index_health",
        lambda *_: {**_healthy_index(), "total": 2, "vector_indexed": 1},
    )

    result = AgentNotesKnowledgeProvider().search(scope, "deploy", mode=SearchMode.SEMANTIC)

    assert len(calls) == 1
    assert result.status.state is KnowledgeState.PARTIAL
    assert result.items[0].score == 0.8


def test_index_health_names_vector_and_sync_gaps(monkeypatch, scope) -> None:
    from agent_notes.core import memory_model

    monkeypatch.setattr(
        memory_model,
        "knowledge_index_health",
        lambda *_: {
            "total": 4,
            "vector_indexed": 2,
            "pending_sync": 1,
            "signed": 3,
            "latest_update": None,
        },
    )
    result = AgentNotesKnowledgeProvider().index_health(scope)

    assert result.status.state is KnowledgeState.PARTIAL
    assert result.index is not None
    assert result.index.vector_missing == 2
    assert result.index.native_records == 1
    assert result.index.semantic_search is KnowledgeState.PARTIAL


def test_links_are_normalized_without_exposing_model_type(monkeypatch, scope) -> None:
    from agent_notes.core import search

    node = SimpleNamespace(
        kind="work_item",
        workspace_id=7,
        project_id=11,
        identifier="WI-42",
        relationship="supports",
        depth=1,
        title="Release qualification",
        status="in_review",
    )
    monkeypatch.setattr(search, "trace_graph_all", lambda *a, **kw: [node])
    result = AgentNotesKnowledgeProvider().links(scope, "deployment-notes")

    assert result.status.state is KnowledgeState.CURRENT
    assert result.links[0].identifier == "WI-42"
    assert result.links[0].workspace == "shared"
    assert result.links[0].project == "alpha"
    assert result.to_dict()["links"][0]["item_state"] == "in_review"


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (
            lambda provider, scope: provider.detail(KnowledgeScope(scope.workspace), "x"),
            "unsupported",
        ),
        (lambda provider, scope: provider.browse(scope, cursor="bad"), "unknown"),
        (lambda provider, scope: provider.links(scope, "x", max_depth=0), "unknown"),
    ],
)
def test_explicit_non_current_states(operation, expected, scope) -> None:
    result = operation(AgentNotesKnowledgeProvider(), scope)
    assert result.to_dict()["status"]["state"] == expected


def test_database_failure_is_sanitized_as_unavailable(monkeypatch, scope) -> None:
    from agent_notes.core import memory_model

    def fail(*args, **kwargs):
        raise RuntimeError("postgresql://operator:secret@example.invalid/private")

    monkeypatch.setattr(memory_model, "browse_knowledge", fail)
    payload = AgentNotesKnowledgeProvider().browse(scope).to_dict()

    assert payload["status"]["state"] == "unavailable"
    assert "secret" not in json.dumps(payload)


def test_model_lexical_search_and_index_health_use_real_postgres(ephemeral_db) -> None:
    """Exercise the provider's new model queries against the supported database."""
    from agent_notes.core import db, memory_model

    workspace = db.get_or_create_workspace("knowledge-provider", "Knowledge provider")
    project = db.get_or_create_project(workspace.id, "contract", "Contract")
    db.add_vocabulary(workspace.id, "memory_type", "note")
    memory_model.add_memory(
        workspace.id,
        project.id,
        "100%-ready",
        "note",
        "A literal percent sign must not broaden the search.",
        embedding=None,
    )

    rows = memory_model.search_memories_exact(workspace.id, "100%", project_id=project.id)
    posture = memory_model.knowledge_index_health(workspace.id, project.id)

    assert [row["name"] for row in rows] == ["100%-ready"]
    assert posture["total"] == 1
    assert posture["vector_indexed"] == 0
    assert posture["pending_sync"] == 0
