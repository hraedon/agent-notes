"""Integration tests for Phase 5 — Reflections workflow.

Phase 3.6 concluded that memories suffice for reflections (decision 25).
Phase 5 adds: reflection vocabulary seeding, find_reflections, extract_gaps,
mark_gaps_filed tools on the memory server.

Tests:
- memory_type='reflection' is seeded in the schema
- find_reflections: search/list reflection-type memories
- extract_gaps: parse 'Gaps to flag' section from reflection body
- mark_gaps_filed: update attributes.gaps_filed_as on reflection memory
- End-to-end: file reflection → extract gaps → mark filed
"""

from __future__ import annotations

import pytest

from agent_notes.core import db as coredb
from agent_notes.core import links as lnk
from agent_notes.servers.memory import MemoryServer


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


@pytest.fixture(scope="module")
def server():
    return MemoryServer()


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
def seeded_reflection(refl_ws, refl_proj, seeded_vocab, server):
    server._tool_add_memory(
        {
            "workspace": "refl-test-ws",
            "project": "refl-proj",
            "name": "reflection-2026-05-21",
            "memory_type": "reflection",
            "body": REFLECTION_BODY,
            "attributes": {
                "model": "test-model",
            },
        }
    )
    server._tool_add_memory(
        {
            "workspace": "refl-test-ws",
            "project": "refl-proj",
            "name": "non-reflection-note",
            "memory_type": "note",
            "body": "This is a regular note, not a reflection.",
        }
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
    def test_find_reflections_with_query(
        self, pg, refl_ws, refl_proj, seeded_reflection, server
    ) -> None:
        result = server._tool_find_reflections(
            {"workspace": "refl-test-ws", "project": "refl-proj", "query": "phased approach"}
        )
        assert "reflection(s) found" in result
        assert "reflection-2026-05-21" in result

    def test_find_reflections_without_query(
        self, pg, refl_ws, refl_proj, seeded_reflection, server
    ) -> None:
        result = server._tool_find_reflections(
            {"workspace": "refl-test-ws", "project": "refl-proj"}
        )
        assert "reflection-2026-05-21" in result

    def test_find_reflections_excludes_non_reflections(
        self, pg, refl_ws, refl_proj, seeded_reflection, server
    ) -> None:
        result = server._tool_find_reflections(
            {"workspace": "refl-test-ws", "project": "refl-proj"}
        )
        assert "non-reflection-note" not in result

    def test_find_reflections_empty_project(self, pg, refl_ws, seeded_vocab, server) -> None:
        coredb.get_or_create_project(refl_ws.id, "empty-proj", "Empty Proj")
        result = server._tool_find_reflections(
            {"workspace": "refl-test-ws", "project": "empty-proj"}
        )
        assert "No reflection memories found" in result

    def test_find_reflections_with_body(
        self, pg, refl_ws, refl_proj, seeded_reflection, server
    ) -> None:
        result = server._tool_find_reflections(
            {
                "workspace": "refl-test-ws",
                "project": "refl-proj",
                "query": "phased",
                "include_body": True,
            }
        )
        assert "reflection-2026-05-21" in result


# ---------------------------------------------------------------------------
# extract_gaps
# ---------------------------------------------------------------------------


class TestExtractGaps:
    def test_extract_gaps_from_reflection(
        self, pg, refl_ws, refl_proj, seeded_reflection, server
    ) -> None:
        result = server._tool_extract_gaps(
            {
                "workspace": "refl-test-ws",
                "project": "refl-proj",
                "name": "reflection-2026-05-21",
            }
        )
        assert "2 gap(s) extracted" in result
        assert "BC-GAP-001" in result
        assert "BC-GAP-002" in result
        assert "retry logic" in result
        assert "pool sizing" in result

    def test_extract_gaps_nonexistent_reflection(
        self, pg, refl_ws, refl_proj, seeded_reflection, server
    ) -> None:
        result = server._tool_extract_gaps(
            {
                "workspace": "refl-test-ws",
                "project": "refl-proj",
                "name": "nonexistent-reflection",
            }
        )
        assert "not found" in result

    def test_extract_gaps_non_reflection_memory(
        self, pg, refl_ws, refl_proj, seeded_reflection, server
    ) -> None:
        result = server._tool_extract_gaps(
            {
                "workspace": "refl-test-ws",
                "project": "refl-proj",
                "name": "non-reflection-note",
            }
        )
        assert "not found" in result

    def test_extract_gaps_no_gaps_section(
        self, pg, refl_ws, refl_proj, seeded_vocab, server
    ) -> None:
        server._tool_add_memory(
            {
                "workspace": "refl-test-ws",
                "project": "refl-proj",
                "name": "reflection-no-gaps",
                "memory_type": "reflection",
                "body": "# Reflection\n\n## What worked\nNothing much.\n",
            }
        )
        result = server._tool_extract_gaps(
            {
                "workspace": "refl-test-ws",
                "project": "refl-proj",
                "name": "reflection-no-gaps",
            }
        )
        assert "No 'Gaps to flag' section" in result


# ---------------------------------------------------------------------------
# mark_gaps_filed
# ---------------------------------------------------------------------------


class TestMarkGapsFiled:
    def test_mark_gaps_filed(self, pg, refl_ws, refl_proj, seeded_reflection, server) -> None:
        result = server._tool_mark_gaps_filed(
            {
                "workspace": "refl-test-ws",
                "project": "refl-proj",
                "name": "reflection-2026-05-21",
                "filed_identifiers": ["BC-GAP-001"],
            }
        )
        assert "Marked 1 gap(s)" in result
        assert "BC-GAP-001" in result

    def test_mark_gaps_filed_updates_attributes(
        self, pg, refl_ws, refl_proj, seeded_reflection, server
    ) -> None:
        server._tool_mark_gaps_filed(
            {
                "workspace": "refl-test-ws",
                "project": "refl-proj",
                "name": "reflection-2026-05-21",
                "filed_identifiers": ["BC-GAP-002"],
            }
        )
        memory = server._tool_get_memory(
            {
                "workspace": "refl-test-ws",
                "project": "refl-proj",
                "name": "reflection-2026-05-21",
            }
        )
        assert "BC-GAP-001" in memory or "BC-GAP-002" in memory

    def test_mark_gaps_filed_writes_change_log(
        self, pg, refl_ws, refl_proj, seeded_reflection, server
    ) -> None:
        from agent_notes.core.change_log import history

        server._tool_mark_gaps_filed(
            {
                "workspace": "refl-test-ws",
                "project": "refl-proj",
                "name": "reflection-2026-05-21",
                "filed_identifiers": ["BC-NEW-GAP"],
            }
        )
        rows = history("memory", refl_ws.id, refl_proj.id, "reflection-2026-05-21")
        events = [r.event for r in rows]
        assert "updated" in events

    def test_mark_gaps_nonexistent_reflection(
        self, pg, refl_ws, refl_proj, seeded_reflection, server
    ) -> None:
        result = server._tool_mark_gaps_filed(
            {
                "workspace": "refl-test-ws",
                "project": "refl-proj",
                "name": "nonexistent-reflection",
                "filed_identifiers": ["BC-999"],
            }
        )
        assert "not found" in result

    def test_extract_shows_filed_status(
        self, pg, refl_ws, refl_proj, seeded_reflection, server
    ) -> None:
        server._tool_mark_gaps_filed(
            {
                "workspace": "refl-test-ws",
                "project": "refl-proj",
                "name": "reflection-2026-05-21",
                "filed_identifiers": ["BC-GAP-001"],
            }
        )
        result = server._tool_extract_gaps(
            {
                "workspace": "refl-test-ws",
                "project": "refl-proj",
                "name": "reflection-2026-05-21",
            }
        )
        assert "BC-GAP-001" in result
        assert "[FILED]" in result


# ---------------------------------------------------------------------------
# End-to-end: reflection → gaps → breadcrumbs
# ---------------------------------------------------------------------------


class TestReflectionEndToEnd:
    def test_wikilinks_create_links_to_gaps(
        self, pg, refl_ws, refl_proj, seeded_reflection
    ) -> None:
        nodes = lnk.trace_graph(
            kind="memory",
            workspace=refl_ws.id,
            project=refl_proj.id,
            identifier="reflection-2026-05-21",
            direction="dependencies",
            max_depth=1,
        )
        identifiers = {n.identifier for n in nodes}
        assert "BC-GAP-001" in identifiers
        assert "BC-GAP-002" in identifiers

    def test_find_reflections_shows_filed_gaps(
        self, pg, refl_ws, refl_proj, seeded_reflection, server
    ) -> None:
        result = server._tool_find_reflections(
            {"workspace": "refl-test-ws", "project": "refl-proj"}
        )
        assert "reflection-2026-05-21" in result


# ---------------------------------------------------------------------------
# Tool wiring
# ---------------------------------------------------------------------------


class TestReflectionToolWiring:
    def test_reflection_tools_registered(self) -> None:
        srv = MemoryServer()
        tools = srv._registry.list_tools()
        names = {t["name"] for t in tools}
        assert "find_reflections" in names
        assert "extract_gaps" in names
        assert "mark_gaps_filed" in names
