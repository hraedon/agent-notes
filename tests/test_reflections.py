"""Integration tests for Phase 5 — Reflections workflow.

Phase 3.6 concluded that memories suffice for reflections (decision 25).
Phase 5 adds: reflection vocabulary seeding, find_reflections, extract_gaps,
mark_gaps_filed tools on the memory model layer.

Tests:
- memory_type='reflection' is seeded in the schema
- find_reflections: search/list reflection-type memories
- extract_gaps: parse 'Gaps to flag' section from reflection body
- mark_gaps_filed: update attributes.gaps_filed_as on reflection memory
- End-to-end: file reflection -> extract gaps -> mark filed
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent_notes.core import db as coredb
from agent_notes.core.memory_model import (
    add_memory,
    extract_gaps,
    find_reflections,
    mark_gaps_filed,
)


def _fake_embed(text, task="document"):
    return [0.0] * 768


@pytest.fixture(scope="module")
def pg(ephemeral_db):
    return ephemeral_db


@pytest.fixture(scope="module")
def refl_ws(pg):
    return coredb.get_or_create_workspace("refl-test-ws", "Reflection Test WS")


@pytest.fixture(scope="module")
def refl_proj(refl_ws):
    return coredb.get_or_create_project(refl_ws.id, "refl-proj", "Reflection Proj")


@pytest.fixture(scope="module")
def seeded_vocab(refl_ws):
    coredb.add_vocabulary(refl_ws.id, "memory_type", "note")
    coredb.add_vocabulary(refl_ws.id, "memory_type", "decision")
    coredb.add_vocabulary(refl_ws.id, "memory_type", "reflection")
    return refl_ws


REFLECTION_BODY = """# Reflection 2026-05-21

## What worked
The phased approach reduced integration risk.

## What didn't work
Embedding load time was higher than expected.

## Gaps to flag
- [[BC-GAP-001]]: Need retry logic in the embedding singleton.
- [[BC-GAP-002]]: Connection pool sizing needs dynamic adjustment.

## Observations
Overall progress is on track for the Phase 5 deadline.
"""


@pytest.fixture(scope="module")
def seeded_reflection(refl_ws, refl_proj, seeded_vocab):
    with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
        add_memory(
            workspace_id=refl_ws.id,
            project_id=refl_proj.id,
            name="reflection-2026-05-21",
            memory_type="reflection",
            body=REFLECTION_BODY,
            attributes={"model": "test-model"},
            embedding=_fake_embed("test"),
        )
        add_memory(
            workspace_id=refl_ws.id,
            project_id=refl_proj.id,
            name="non-reflection-note",
            memory_type="note",
            body="This is a regular note, not a reflection.",
            embedding=_fake_embed("test"),
        )


# ---------------------------------------------------------------------------
# Reflection vocabulary seed
# ---------------------------------------------------------------------------


class TestReflectionVocabulary:
    def test_reflection_vocab_exists_in_default_workspace(self, pg) -> None:
        ws = coredb.get_or_create_workspace("default", "Default Workspace")
        vocabs = coredb.list_vocabulary(ws.id, kind_namespace="memory_type")
        names = {v.name for v in vocabs}
        assert "reflection" in names

    def test_other_memory_types_seeded(self, pg) -> None:
        ws = coredb.get_or_create_workspace("default", "Default Workspace")
        vocabs = coredb.list_vocabulary(ws.id, kind_namespace="memory_type")
        names = {v.name for v in vocabs}
        for expected in ("note", "decision", "feedback", "reference", "user", "reflection"):
            assert expected in names, f"memory_type '{expected}' should be seeded"


# ---------------------------------------------------------------------------
# find_reflections
# ---------------------------------------------------------------------------


class TestFindReflections:
    def test_find_reflections_with_query(self, pg, refl_ws, refl_proj, seeded_reflection) -> None:
        rows = find_reflections(
            workspace_id=refl_ws.id,
            project_id=refl_proj.id,
            query_vec=_fake_embed("phased approach"),
        )
        names = {r["name"] for r in rows}
        assert "reflection-2026-05-21" in names

    def test_find_reflections_without_query(
        self, pg, refl_ws, refl_proj, seeded_reflection
    ) -> None:
        rows = find_reflections(
            workspace_id=refl_ws.id,
            project_id=refl_proj.id,
        )
        names = {r["name"] for r in rows}
        assert "reflection-2026-05-21" in names

    def test_find_reflections_excludes_non_reflections(
        self, pg, refl_ws, refl_proj, seeded_reflection
    ) -> None:
        rows = find_reflections(
            workspace_id=refl_ws.id,
            project_id=refl_proj.id,
        )
        names = {r["name"] for r in rows}
        assert "non-reflection-note" not in names

    def test_find_reflections_empty_project(self, pg, refl_ws, seeded_vocab) -> None:
        empty_proj = coredb.get_or_create_project(refl_ws.id, "empty-proj", "Empty Proj")
        rows = find_reflections(
            workspace_id=refl_ws.id,
            project_id=empty_proj.id,
        )
        assert len(rows) == 0

    def test_find_reflections_with_body(self, pg, refl_ws, refl_proj, seeded_reflection) -> None:
        rows = find_reflections(
            workspace_id=refl_ws.id,
            project_id=refl_proj.id,
            query_vec=_fake_embed("phased"),
            include_body=True,
        )
        names = {r["name"] for r in rows}
        assert "reflection-2026-05-21" in names


# ---------------------------------------------------------------------------
# extract_gaps
# ---------------------------------------------------------------------------


class TestExtractGaps:
    def test_extract_gaps_from_reflection(self, pg, refl_ws, refl_proj, seeded_reflection) -> None:
        result = extract_gaps(
            workspace_id=refl_ws.id,
            project_id=refl_proj.id,
            name="reflection-2026-05-21",
        )
        assert "gaps" in result
        assert len(result["gaps"]) == 2
        identifiers = {g["identifier"] for g in result["gaps"]}
        assert "BC-GAP-001" in identifiers
        assert "BC-GAP-002" in identifiers

    def test_extract_gaps_nonexistent_reflection(
        self, pg, refl_ws, refl_proj, seeded_reflection
    ) -> None:
        result = extract_gaps(
            workspace_id=refl_ws.id,
            project_id=refl_proj.id,
            name="nonexistent-reflection",
        )
        assert "error" in result
        assert "not found" in result["error"]

    def test_extract_gaps_non_reflection_memory(
        self, pg, refl_ws, refl_proj, seeded_reflection
    ) -> None:
        result = extract_gaps(
            workspace_id=refl_ws.id,
            project_id=refl_proj.id,
            name="non-reflection-note",
        )
        assert "error" in result
        assert "not found" in result["error"]

    def test_extract_gaps_no_gaps_section(self, pg, refl_ws, refl_proj, seeded_vocab) -> None:
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            add_memory(
                workspace_id=refl_ws.id,
                project_id=refl_proj.id,
                name="reflection-no-gaps",
                memory_type="reflection",
                body="# Reflection\n\n## What worked\nNothing much.\n",
                embedding=_fake_embed("test"),
            )
        result = extract_gaps(
            workspace_id=refl_ws.id,
            project_id=refl_proj.id,
            name="reflection-no-gaps",
        )
        assert "error" in result
        assert "No 'Gaps to flag' section" in result["error"]


# ---------------------------------------------------------------------------
# mark_gaps_filed
# ---------------------------------------------------------------------------


class TestMarkGapsFiled:
    def test_mark_gaps_filed(self, pg, refl_ws, refl_proj, seeded_reflection) -> None:
        result = mark_gaps_filed(
            workspace_id=refl_ws.id,
            project_id=refl_proj.id,
            name="reflection-2026-05-21",
            filed_identifiers=["BC-GAP-001"],
        )
        assert "gaps_filed_as" in result
        assert "BC-GAP-001" in result["gaps_filed_as"]

    def test_mark_multiple_gaps(self, pg, refl_ws, refl_proj, seeded_vocab) -> None:
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            add_memory(
                workspace_id=refl_ws.id,
                project_id=refl_proj.id,
                name="reflection-mark-multi",
                memory_type="reflection",
                body="# Reflection\n\n## Gaps to flag\n- [[BC-M1]]: Gap 1.\n- [[BC-M2]]: Gap 2.\n",
                embedding=_fake_embed("test"),
            )
        result = mark_gaps_filed(
            workspace_id=refl_ws.id,
            project_id=refl_proj.id,
            name="reflection-mark-multi",
            filed_identifiers=["BC-M1", "BC-M2"],
        )
        assert "gaps_filed_as" in result
        assert set(result["gaps_filed_as"]) == {"BC-M1", "BC-M2"}

    def test_mark_gaps_filed_nonexistent(self, pg, refl_ws, refl_proj) -> None:
        result = mark_gaps_filed(
            workspace_id=refl_ws.id,
            project_id=refl_proj.id,
            name="nonexistent",
            filed_identifiers=["BC-X"],
        )
        assert "error" in result


# ---------------------------------------------------------------------------
# End-to-end: file reflection -> extract gaps -> mark filed
# ---------------------------------------------------------------------------


class TestReflectionEndToEnd:
    def test_full_reflection_workflow(self, pg, refl_ws, refl_proj, seeded_vocab) -> None:
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            add_memory(
                workspace_id=refl_ws.id,
                project_id=refl_proj.id,
                name="reflection-e2e",
                memory_type="reflection",
                body=REFLECTION_BODY,
                embedding=_fake_embed("test"),
            )

        gaps = extract_gaps(
            workspace_id=refl_ws.id,
            project_id=refl_proj.id,
            name="reflection-e2e",
        )
        assert len(gaps["gaps"]) == 2

        result = mark_gaps_filed(
            workspace_id=refl_ws.id,
            project_id=refl_proj.id,
            name="reflection-e2e",
            filed_identifiers=["BC-GAP-001", "BC-GAP-002"],
        )
        assert set(result["gaps_filed_as"]) == {"BC-GAP-001", "BC-GAP-002"}
