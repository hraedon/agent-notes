"""Tests for the NativeMemoryEngine (Plan 020 WI-1.1).

Behavior-preserving wrapper around the existing pgvector memory model.
Uses the ephemeral_db fixture and _fake_embed pattern from test_memory.py.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent_notes.core import db as coredb
from agent_notes.core.memory_engine import (
    EngineCapability,
    EngineHealthState,
    ForgetSelector,
    IngestBatch,
    IngestItem,
    MemoryScope,
    OperationState,
    OriginClass,
    RecallQuery,
)
from agent_notes.core.native_engine import NativeMemoryEngine


def _fake_embed(text, task="document"):
    return [0.0] * 768


@pytest.fixture(scope="module")
def pg(ephemeral_db):
    return ephemeral_db


@pytest.fixture(scope="module")
def ne_ws(pg):
    return coredb.get_or_create_workspace("ne-test-ws", "Native Engine Test WS")


@pytest.fixture(scope="module")
def ne_proj(ne_ws):
    return coredb.get_or_create_project(ne_ws.id, "ne-proj", "Native Engine Proj")


@pytest.fixture(scope="module")
def ne_vocab(ne_ws):
    coredb.add_vocabulary(ne_ws.id, "memory_type", "memory")
    coredb.add_vocabulary(ne_ws.id, "memory_type", "note")
    coredb.add_vocabulary(ne_ws.id, "memory_type", "reflection")
    return ne_ws


@pytest.fixture(scope="module")
def ne_scope(ne_ws, ne_proj):
    return MemoryScope(
        project_slug=ne_proj.slug,
        workspace_slug=ne_ws.slug,
    )


@pytest.fixture(scope="module")
def engine():
    return NativeMemoryEngine()


class TestNativeEngineDescribe:
    def test_healthy_state(self, pg, ne_ws, ne_proj, ne_vocab, engine) -> None:
        health = engine.describe()
        assert health.state == EngineHealthState.HEALTHY
        assert health.engine_name == "native"
        assert health.protocol_version == "1.0"
        assert health.indexing_backlog == 0
        assert health.indexing_freshness is None

    def test_capabilities(self, pg, ne_ws, ne_proj, ne_vocab, engine) -> None:
        health = engine.describe()
        caps = health.capabilities
        assert EngineCapability.INGEST in caps
        assert EngineCapability.RECALL in caps
        assert EngineCapability.FORGET in caps
        assert EngineCapability.EXACT_SOURCE in caps
        assert EngineCapability.SYNTHESIZE not in caps


class TestNativeEngineIngest:
    def test_ingest_returns_indexed(self, pg, ne_ws, ne_proj, ne_vocab, ne_scope, engine) -> None:
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            batch = IngestBatch(
                items=[
                    IngestItem(
                        source_id="ne-ingest-1",
                        content="First native engine test memory.",
                        memory_type="note",
                    ),
                    IngestItem(
                        source_id="ne-ingest-2",
                        content="Second native engine test memory.",
                        memory_type="note",
                    ),
                ],
                scope=ne_scope,
            )
            result = engine.ingest(batch)
        assert result.state == OperationState.INDEXED
        assert result.items_count == 2
        assert result.operation_id is None
        assert result.error is None

    def test_ingest_failed_on_bad_scope(self, pg, engine) -> None:
        batch = IngestBatch(
            items=[
                IngestItem(
                    source_id="ne-bad-scope",
                    content="This should fail.",
                    memory_type="note",
                ),
            ],
            scope=MemoryScope(
                project_slug="nonexistent-proj",
                workspace_slug="nonexistent-ws",
            ),
        )
        result = engine.ingest(batch)
        assert result.state == OperationState.FAILED
        assert result.items_count == 0
        assert result.error is not None


class TestNativeEngineRecall:
    def test_recall_returns_raw_results(
        self, pg, ne_ws, ne_proj, ne_vocab, ne_scope, engine
    ) -> None:
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            batch = IngestBatch(
                items=[
                    IngestItem(
                        source_id="ne-recall-target",
                        content="PostgreSQL vector search test for native engine.",
                        memory_type="note",
                    ),
                ],
                scope=ne_scope,
            )
            engine.ingest(batch)

            query = RecallQuery(
                query="database vector search",
                scope=ne_scope,
            )
            response = engine.recall(query)

        assert response.engine == "native"
        assert len(response.results) > 0
        assert any(r.source_ref == "ne-recall-target" for r in response.results)
        assert all(r.origin == OriginClass.RAW for r in response.results)

    def test_recall_embedding_failure_returns_empty(
        self, pg, ne_ws, ne_proj, ne_vocab, ne_scope, engine
    ) -> None:
        def _boom(text, task="document"):
            raise RuntimeError("model unavailable")

        with patch("agent_notes.core.embed.embed", side_effect=_boom):
            query = RecallQuery(
                query="anything",
                scope=ne_scope,
            )
            response = engine.recall(query)

        assert response.engine == "native"
        assert len(response.results) == 0
        assert "error" in response.usage


class TestNativeEngineForget:
    def test_forget_by_source_id(self, pg, ne_ws, ne_proj, ne_vocab, ne_scope, engine) -> None:
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            batch = IngestBatch(
                items=[
                    IngestItem(
                        source_id="ne-forget-target",
                        content="This memory will be forgotten.",
                        memory_type="note",
                    ),
                ],
                scope=ne_scope,
            )
            engine.ingest(batch)

            selector = ForgetSelector(
                source_id="ne-forget-target",
                scope=ne_scope,
            )
            result = engine.forget(selector)
        assert result.deleted_count == 1
        assert result.cascade_count == 0
        assert result.error is None

        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            query = RecallQuery(
                query="forgotten memory",
                scope=ne_scope,
            )
            response = engine.recall(query)
        assert not any(r.source_ref == "ne-forget-target" for r in response.results)

    def test_forget_nonexistent(self, pg, ne_ws, ne_proj, ne_vocab, ne_scope, engine) -> None:
        selector = ForgetSelector(
            source_id="ne-ghost-mem",
            scope=ne_scope,
        )
        result = engine.forget(selector)
        assert result.deleted_count == 0
        assert result.error is not None

    def test_forget_scope_unsupported(self, pg, ne_ws, ne_proj, ne_vocab, ne_scope, engine) -> None:
        selector = ForgetSelector(scope=ne_scope)
        result = engine.forget(selector)
        assert result.deleted_count == 0
        assert result.error is not None
        assert "scope-wide" in result.error

    def test_forget_no_selector(self, pg, engine) -> None:
        selector = ForgetSelector()
        result = engine.forget(selector)
        assert result.deleted_count == 0
        assert result.error is not None


class TestNativeEngineOperationStatus:
    def test_always_indexed(self, pg, ne_ws, ne_proj, ne_vocab, engine) -> None:
        status = engine.operation_status("any-op-id")
        assert status.state == OperationState.INDEXED
        assert status.operation_id == "any-op-id"
