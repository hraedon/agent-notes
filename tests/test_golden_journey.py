"""Extended GJ-3 proof — full memory engine lifecycle (Plan 012 WI-4.1).

Exercises the complete memory lifecycle through the MemoryEngine protocol:
file → ingest → recall → read exact → forget (cascade) → cross-project isolation.

Runs against the native pgvector engine (ephemeral DB), the only engine since
Plan 026 D6.
"""

from __future__ import annotations

import uuid
from typing import assert_never
from unittest.mock import patch

import pytest

from agent_notes.core import db as coredb
from agent_notes.core import memory_model
from agent_notes.core.memory_engine import (
    ForgetSelector,
    IngestBatch,
    IngestItem,
    MemoryScope,
    OperationState,
    OriginClass,
    RecallQuery,
)

SIGNED_BODY = (
    "The deployment pipeline uses blue-green strategy with "
    "automated rollback on health-check failure."
)
NOTE_NAME = "gj3-deployment-note"


def _fake_embed(text: str, task: str = "document") -> list[float]:
    return [0.0] * 768


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_embed():
    with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
        yield


@pytest.fixture
def engine(ephemeral_db):
    from agent_notes.core.native_engine import NativeMemoryEngine

    return NativeMemoryEngine()


@pytest.fixture
def gj_setup(ephemeral_db):
    unique = uuid.uuid4().hex[:8]
    ws = coredb.get_or_create_workspace(f"gj3-ws-{unique}", "GJ3 Test WS")
    proj = coredb.get_or_create_project(ws.id, f"gj3-proj-{unique}", "GJ3 Proj")
    decoy_proj = coredb.get_or_create_project(ws.id, f"gj3-decoy-{unique}", "GJ3 Decoy")
    coredb.add_vocabulary(ws.id, "memory_type", "note")
    coredb.add_vocabulary(ws.id, "memory_type", "decision")

    scope = MemoryScope(
        project_slug=proj.slug,
        workspace_slug=ws.slug,
        user_id="gj3-user",
        agent_id="gj3-agent",
    )
    decoy_scope = MemoryScope(
        project_slug=decoy_proj.slug,
        workspace_slug=ws.slug,
        user_id="gj3-user",
        agent_id="gj3-agent",
    )
    return {
        "ws": ws,
        "proj": proj,
        "decoy_proj": decoy_proj,
        "scope": scope,
        "decoy_scope": decoy_scope,
    }


def _file_exact_note(setup: dict) -> dict:
    """Step 1: file a signed note through agent-notes."""
    row = memory_model.add_memory(
        workspace_id=setup["ws"].id,
        project_id=setup["proj"].id,
        name=NOTE_NAME,
        memory_type="note",
        body=SIGNED_BODY,
        embedding=_fake_embed("document"),
    )
    assert row["name"] == NOTE_NAME

    exact = memory_model.get_memory(
        workspace_id=setup["ws"].id,
        project_id=setup["proj"].id,
        name=NOTE_NAME,
    )
    assert exact is not None
    assert exact["body"] == SIGNED_BODY
    return exact


def _ingest(engine, scope: MemoryScope) -> None:
    """Step 2: observe provider ingestion."""
    batch = IngestBatch(
        items=[
            IngestItem(
                source_id=NOTE_NAME,
                content=SIGNED_BODY,
                memory_type="note",
            )
        ],
        scope=scope,
    )
    result = engine.ingest(batch)

    match result.state:
        case OperationState.INDEXED:
            pass
        case OperationState.PENDING:
            assert result.operation_id is not None
            for _ in range(10):
                status = engine.operation_status(result.operation_id)
                if status.state == OperationState.INDEXED:
                    break
                match status.state:
                    case OperationState.PENDING:
                        continue
                    case OperationState.FAILED:
                        pytest.fail(f"Ingest failed: {status.error}")
                    case OperationState.CANCELLED:
                        pytest.fail("Ingest was cancelled")
                    case OperationState.STALE:
                        pytest.fail("Ingest is stale")
                    case other:
                        assert_never(other)
            else:
                pytest.fail("Ingest did not complete within polling attempts")
        case OperationState.FAILED:
            pytest.fail(f"Ingest failed: {result.error}")
        case OperationState.CANCELLED:
            pytest.fail("Ingest was cancelled")
        case OperationState.STALE:
            pytest.fail("Ingest is stale")
        case other:
            assert_never(other)

    assert result.items_count >= 1
    assert result.error is None


# ---------------------------------------------------------------------------
# Test 1: full golden journey lifecycle
# ---------------------------------------------------------------------------


class TestGoldenJourneyFullLifecycle:
    def test_golden_journey_full_lifecycle(self, engine, gj_setup) -> None:
        setup = gj_setup
        scope = setup["scope"]

        # Step 1: file a signed note through agent-notes.
        _file_exact_note(setup)

        # Step 2: observe provider ingestion.
        _ingest(engine, scope)

        # Step 3: recall learned context with a source reference.
        recall_resp = engine.recall(RecallQuery(query="deployment pipeline rollback", scope=scope))
        assert recall_resp.results
        assert recall_resp.engine == engine.engine_name

        source_refs = {r.source_ref for r in recall_resp.results}
        assert NOTE_NAME in source_refs

        # Step 4: read the exact signed note through the human face.
        exact = memory_model.get_memory(
            workspace_id=setup["ws"].id,
            project_id=setup["proj"].id,
            name=NOTE_NAME,
        )
        assert exact is not None
        assert exact["body"] == SIGNED_BODY

        # Step 5: prove another project cannot retrieve it (while note is live).
        decoy_resp = engine.recall(
            RecallQuery(query="deployment pipeline rollback", scope=setup["decoy_scope"])
        )
        decoy_refs = {r.source_ref for r in decoy_resp.results}
        assert NOTE_NAME not in decoy_refs

        # Step 6: delete/supersede and verify provider cascade.
        forget_result = engine.forget(ForgetSelector(source_id=NOTE_NAME, scope=scope))
        assert forget_result.deleted_count >= 1
        assert forget_result.error is None

        recall_after = engine.recall(RecallQuery(query="deployment pipeline rollback", scope=scope))
        refs_after = {r.source_ref for r in recall_after.results}
        assert NOTE_NAME not in refs_after


# ---------------------------------------------------------------------------
# Test 2: provider outage proves exact note readability
# ---------------------------------------------------------------------------


class TestProviderOutageProvesExactReadability:
    def test_provider_outage_proves_exact_readability(self, engine, gj_setup) -> None:
        setup = gj_setup
        scope = setup["scope"]

        _file_exact_note(setup)
        _ingest(engine, scope)

        with patch(
            "agent_notes.core.embed.embed",
            side_effect=RuntimeError("model unavailable"),
        ):
            recall_resp = engine.recall(RecallQuery(query="deployment pipeline", scope=scope))
        assert len(recall_resp.results) == 0
        assert "error" in recall_resp.usage

        exact = memory_model.get_memory(
            workspace_id=setup["ws"].id,
            project_id=setup["proj"].id,
            name=NOTE_NAME,
        )
        assert exact is not None
        assert exact["body"] == SIGNED_BODY


# ---------------------------------------------------------------------------
# Test 3: synthesized output is labelled derived
# ---------------------------------------------------------------------------


class TestSynthesizedOutputIsLabelledDerived:
    def test_synthesized_output_is_labelled_derived(self, engine, gj_setup) -> None:
        setup = gj_setup
        scope = setup["scope"]

        _file_exact_note(setup)
        _ingest(engine, scope)

        recall_resp = engine.recall(RecallQuery(query="deployment pipeline rollback", scope=scope))
        assert recall_resp.results

        for result in recall_resp.results:
            match result.origin:
                case OriginClass.RAW:
                    assert result.source_ref is not None
                case OriginClass.EXTRACTED:
                    pass
                case OriginClass.DERIVED:
                    assert result.text != SIGNED_BODY
                case OriginClass.SYNTHESIZED:
                    assert result.text != SIGNED_BODY
                case other:
                    assert_never(other)
