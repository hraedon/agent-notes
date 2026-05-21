"""Integration tests for the memory server (Phase 3).

Tests the full tool surface against ephemeral Postgres:
- add_memory / get_memory / list_memories / delete_memory
- search_memory (semantic search with body elision)
- [[name]] wikilink auto-linking
- supersedes chain
- trace_graph for memories
- vocabulary reference check for memory_type
- change_log integration
"""

from __future__ import annotations

from datetime import datetime, timezone

import psycopg
import pytest
from psycopg.rows import dict_row

from agent_notes.core import change_log as cl
from agent_notes.core import db as coredb
from agent_notes.core import links as lnk
from agent_notes.servers.memory import MemoryServer, _parse_wikilinks


@pytest.fixture(scope="module")
def pg(ephemeral_db):
    return ephemeral_db


@pytest.fixture(scope="module")
def mem_ws(pg):
    ws = coredb.get_or_create_workspace("mem-test-ws", "Memory Test WS")
    return ws


@pytest.fixture(scope="module")
def mem_proj(mem_ws):
    return coredb.get_or_create_project(mem_ws.id, "mem-proj", "Memory Proj")


@pytest.fixture(scope="module")
def global_proj(mem_ws):
    return coredb.get_or_create_project(mem_ws.id, "global", "Global Proj")


@pytest.fixture(scope="module")
def seeded_vocab(mem_ws):
    coredb.add_vocabulary(mem_ws.id, "memory_type", "note")
    coredb.add_vocabulary(mem_ws.id, "memory_type", "decision")
    coredb.add_vocabulary(mem_ws.id, "memory_type", "feedback")
    coredb.add_vocabulary(mem_ws.id, "memory_type", "reflection")
    return mem_ws


@pytest.fixture(scope="module")
def server():
    return MemoryServer()


# ---------------------------------------------------------------------------
# Wikilink parser (unit tests, no DB needed)
# ---------------------------------------------------------------------------


class TestParseWikilinks:
    def test_simple_reference(self) -> None:
        assert _parse_wikilinks("See [[foo]] for details.") == ["foo"]

    def test_multiple_references(self) -> None:
        names = _parse_wikilinks("See [[foo]] and [[bar]].")
        assert set(names) == {"foo", "bar"}

    def test_no_references(self) -> None:
        assert _parse_wikilinks("No references here.") == []

    def test_skip_code_span(self) -> None:
        assert _parse_wikilinks("Use `[[skip]]` not this.") == []

    def test_mixed(self) -> None:
        names = _parse_wikilinks("See [[keep]] but `[[skip]]` and [[also_keep]].")
        assert "keep" in names
        assert "also_keep" in names
        assert "skip" not in names

    def test_extra_bracket_after_is_ok(self) -> None:
        assert _parse_wikilinks("[[valid]]]") == ["valid"]


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestMemorySchema:
    def test_memories_table_exists(self, pg) -> None:
        with psycopg.connect(pg) as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'memories'")
            assert cur.fetchone() is not None

    def test_partial_unique_index_exists(self, pg) -> None:
        with psycopg.connect(pg) as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM pg_indexes WHERE indexname = 'memories_name_active_unique'")
            assert cur.fetchone() is not None

    def test_updated_at_trigger(self, pg, mem_ws, mem_proj, seeded_vocab) -> None:
        with coredb._conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                """
                INSERT INTO memories (workspace_id, project_id, name, memory_type, body)
                VALUES (%s, %s, 'trigger-test', 'note', 'body')
                RETURNING id, created_at, updated_at
                """,
                (mem_ws.id, mem_proj.id),
            )
            row = cur.fetchone()
            mem_id = row["id"]

            cur.execute("UPDATE memories SET body = 'new body' WHERE id = %s", (mem_id,))

            cur.execute(
                "SELECT created_at, updated_at FROM memories WHERE id = %s",
                (mem_id,),
            )
            result = cur.fetchone()
            conn.commit()

            assert result["updated_at"] >= result["created_at"]


# ---------------------------------------------------------------------------
# add_memory tool
# ---------------------------------------------------------------------------


class TestAddMemory:
    def test_add_basic(self, pg, mem_ws, mem_proj, seeded_vocab, server) -> None:
        result = server._tool_add_memory(
            {
                "workspace": "mem-test-ws",
                "project": "mem-proj",
                "name": "test-note-1",
                "memory_type": "note",
                "body": "This is a test memory note.",
            }
        )
        assert "test-note-1" in result
        assert "note" in result

    def test_add_with_attributes(self, pg, mem_ws, mem_proj, seeded_vocab, server) -> None:
        result = server._tool_add_memory(
            {
                "workspace": "mem-test-ws",
                "project": "mem-proj",
                "name": "test-note-attr",
                "memory_type": "note",
                "body": "Body with attributes.",
                "attributes": {"priority": "high", "source": "test"},
            }
        )
        assert "test-note-attr" in result

    def test_add_to_global_project(self, pg, mem_ws, global_proj, seeded_vocab, server) -> None:
        result = server._tool_add_memory(
            {
                "workspace": "mem-test-ws",
                "project": "global",
                "name": "global-memory",
                "memory_type": "feedback",
                "body": "Cross-cutting feedback memory.",
            }
        )
        assert "global-memory" in result

    def test_add_writes_change_log(self, pg, mem_ws, mem_proj, seeded_vocab, server) -> None:
        server._tool_add_memory(
            {
                "workspace": "mem-test-ws",
                "project": "mem-proj",
                "name": "cl-test-mem",
                "memory_type": "note",
                "body": "Change log test.",
            }
        )
        since = datetime(2000, 1, 1, tzinfo=timezone.utc)
        rows = cl.changes_since(since, workspace_id=mem_ws.id, kind="memory")
        filed = [r for r in rows if r.event == "filed"]
        assert any(r.identifier == "cl-test-mem" for r in filed)

    def test_add_supersedes_existing(self, pg, mem_ws, mem_proj, seeded_vocab, server) -> None:
        server._tool_add_memory(
            {
                "workspace": "mem-test-ws",
                "project": "mem-proj",
                "name": "supersede-test",
                "memory_type": "note",
                "body": "Original version.",
            }
        )
        result = server._tool_add_memory(
            {
                "workspace": "mem-test-ws",
                "project": "mem-proj",
                "name": "supersede-test",
                "memory_type": "note",
                "body": "Updated version.",
            }
        )
        assert "superseded" in result.lower()

        with coredb._conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT id, name, active FROM memories WHERE project_id = %s AND name = %s",
                (mem_proj.id, "supersede-test"),
            )
            all_rows = cur.fetchall()
            assert len(all_rows) == 2, f"Expected 2 rows, got {len(all_rows)}: {all_rows}"
            active = [r for r in all_rows if r["active"]]
            inactive = [r for r in all_rows if not r["active"]]
            assert len(inactive) == 1, f"Expected 1 inactive, got: {all_rows}"
            assert len(active) == 1, f"Expected 1 active, got: {all_rows}"

    def test_invalid_memory_type(self, pg, mem_ws, mem_proj, server) -> None:
        try:
            server._tool_add_memory(
                {
                    "workspace": "mem-test-ws",
                    "project": "mem-proj",
                    "name": "bad-type",
                    "memory_type": "nonexistent",
                    "body": "Should fail.",
                }
            )
            assert False, "Should have raised"
        except ValueError as e:
            assert "not found" in str(e).lower()

    def test_wikilinks_auto_linked(self, pg, mem_ws, mem_proj, seeded_vocab, server) -> None:
        server._tool_add_memory(
            {
                "workspace": "mem-test-ws",
                "project": "mem-proj",
                "name": "wikilink-target",
                "memory_type": "note",
                "body": "I am the target.",
            }
        )
        server._tool_add_memory(
            {
                "workspace": "mem-test-ws",
                "project": "mem-proj",
                "name": "wikilink-source",
                "memory_type": "note",
                "body": "See [[wikilink-target]] for details.",
            }
        )

        nodes = lnk.trace_graph(
            kind="memory",
            workspace=mem_ws.id,
            project=mem_proj.id,
            identifier="wikilink-source",
            direction="dependencies",
            max_depth=1,
        )
        identifiers = {n.identifier for n in nodes}
        assert "wikilink-target" in identifiers


# ---------------------------------------------------------------------------
# get_memory tool
# ---------------------------------------------------------------------------


class TestGetMemory:
    def test_get_existing(self, pg, mem_ws, mem_proj, seeded_vocab, server) -> None:
        server._tool_add_memory(
            {
                "workspace": "mem-test-ws",
                "project": "mem-proj",
                "name": "get-test",
                "memory_type": "note",
                "body": "Gettable memory.",
            }
        )
        result = server._tool_get_memory(
            {"workspace": "mem-test-ws", "project": "mem-proj", "name": "get-test"}
        )
        assert "get-test" in result
        assert "Gettable memory." in result

    def test_get_nonexistent(self, pg, mem_ws, mem_proj, server) -> None:
        result = server._tool_get_memory(
            {"workspace": "mem-test-ws", "project": "mem-proj", "name": "no-such-mem"}
        )
        assert "not found" in result.lower()


# ---------------------------------------------------------------------------
# list_memories tool
# ---------------------------------------------------------------------------


class TestListMemories:
    def test_list_returns_memories(self, pg, mem_ws, mem_proj, seeded_vocab, server) -> None:
        result = server._tool_list_memories({"workspace": "mem-test-ws", "project": "mem-proj"})
        assert "memory" in result.lower() or "memories" in result.lower()

    def test_list_empty_project(self, pg, mem_ws, seeded_vocab, server) -> None:
        coredb.get_or_create_project(mem_ws.id, "empty-proj", "Empty")
        result = server._tool_list_memories({"workspace": "mem-test-ws", "project": "empty-proj"})
        assert "no memories" in result.lower()


# ---------------------------------------------------------------------------
# search_memory tool
# ---------------------------------------------------------------------------


class TestSearchMemory:
    def test_search_finds_relevant(self, pg, mem_ws, mem_proj, seeded_vocab, server) -> None:
        server._tool_add_memory(
            {
                "workspace": "mem-test-ws",
                "project": "mem-proj",
                "name": "search-target",
                "memory_type": "note",
                "body": "PostgreSQL connection pooling with psycopg.",
            }
        )
        result = server._tool_search_memory(
            {
                "workspace": "mem-test-ws",
                "project": "mem-proj",
                "query": "database connection pool",
            }
        )
        assert "search-target" in result

    def test_search_body_elided_by_default(
        self, pg, mem_ws, mem_proj, seeded_vocab, server
    ) -> None:
        server._tool_add_memory(
            {
                "workspace": "mem-test-ws",
                "project": "mem-proj",
                "name": "elision-test",
                "memory_type": "note",
                "body": "A" * 500,
            }
        )
        result = server._tool_search_memory(
            {
                "workspace": "mem-test-ws",
                "project": "mem-proj",
                "query": "memory search test",
                "include_body": False,
            }
        )
        # Body should not appear when include_body=False
        assert "AAAA" not in result

    def test_search_body_included_when_requested(
        self, pg, mem_ws, mem_proj, seeded_vocab, server
    ) -> None:
        unique_body = "UNIQUE_BODY_CONTENT_XYZ123"
        server._tool_add_memory(
            {
                "workspace": "mem-test-ws",
                "project": "mem-proj",
                "name": "body-include-test",
                "memory_type": "note",
                "body": unique_body,
            }
        )
        result = server._tool_search_memory(
            {
                "workspace": "mem-test-ws",
                "project": "mem-proj",
                "query": "unique body content",
                "include_body": True,
            }
        )
        assert unique_body[:200] in result

    def test_search_filter_by_memory_type(self, pg, mem_ws, mem_proj, seeded_vocab, server) -> None:
        server._tool_add_memory(
            {
                "workspace": "mem-test-ws",
                "project": "mem-proj",
                "name": "type-filter-decision",
                "memory_type": "decision",
                "body": "We decided to use PostgreSQL.",
            }
        )
        result = server._tool_search_memory(
            {
                "workspace": "mem-test-ws",
                "project": "mem-proj",
                "query": "PostgreSQL",
                "memory_type": "decision",
            }
        )
        assert "type-filter-decision" in result


# ---------------------------------------------------------------------------
# delete_memory tool
# ---------------------------------------------------------------------------


class TestDeleteMemory:
    def test_soft_delete(self, pg, mem_ws, mem_proj, seeded_vocab, server) -> None:
        server._tool_add_memory(
            {
                "workspace": "mem-test-ws",
                "project": "mem-proj",
                "name": "delete-me",
                "memory_type": "note",
                "body": "Soon to be deleted.",
            }
        )
        result = server._tool_delete_memory(
            {"workspace": "mem-test-ws", "project": "mem-proj", "name": "delete-me"}
        )
        assert "soft-deleted" in result.lower()

        # Should no longer be retrievable via get_memory (active only).
        result = server._tool_get_memory(
            {"workspace": "mem-test-ws", "project": "mem-proj", "name": "delete-me"}
        )
        assert "not found" in result.lower()

    def test_delete_writes_change_log(self, pg, mem_ws, mem_proj, seeded_vocab, server) -> None:
        server._tool_add_memory(
            {
                "workspace": "mem-test-ws",
                "project": "mem-proj",
                "name": "delete-cl-test",
                "memory_type": "note",
                "body": "For change_log delete.",
            }
        )
        server._tool_delete_memory(
            {"workspace": "mem-test-ws", "project": "mem-proj", "name": "delete-cl-test"}
        )
        since = datetime(2000, 1, 1, tzinfo=timezone.utc)
        rows = cl.changes_since(since, workspace_id=mem_ws.id, kind="memory")
        deleted = [r for r in rows if r.event == "deleted"]
        assert any(r.identifier == "delete-cl-test" for r in deleted)

    def test_delete_nonexistent(self, pg, mem_ws, mem_proj, server) -> None:
        result = server._tool_delete_memory(
            {"workspace": "mem-test-ws", "project": "mem-proj", "name": "ghost"}
        )
        assert "not found" in result.lower()


# ---------------------------------------------------------------------------
# trace_graph for memories
# ---------------------------------------------------------------------------


class TestTraceGraphMemory:
    def test_trace_graph_dependencies(self, pg, mem_ws, mem_proj, seeded_vocab, server) -> None:
        server._tool_add_memory(
            {
                "workspace": "mem-test-ws",
                "project": "mem-proj",
                "name": "tg-root",
                "memory_type": "note",
                "body": "Root note linking to [[tg-child]].",
            }
        )
        server._tool_add_memory(
            {
                "workspace": "mem-test-ws",
                "project": "mem-proj",
                "name": "tg-child",
                "memory_type": "note",
                "body": "Child note.",
            }
        )
        result = server._tool_trace_graph(
            {
                "workspace": "mem-test-ws",
                "project": "mem-proj",
                "name": "tg-root",
                "direction": "dependencies",
            }
        )
        assert "tg-child" in result


# ---------------------------------------------------------------------------
# Vocabulary reference check
# ---------------------------------------------------------------------------


class TestVocabReferenceCheck:
    def test_delete_referenced_memory_type(self, pg, mem_ws, mem_proj, seeded_vocab) -> None:
        with coredb._conn() as conn:
            conn.execute(
                """
                INSERT INTO memories (workspace_id, project_id, name, memory_type, body)
                VALUES (%s, %s, 'refcheck-mem', 'feedback', 'test')
                ON CONFLICT DO NOTHING
                """,
                (mem_ws.id, mem_proj.id),
            )
            conn.commit()

        with pytest.raises(ValueError, match="still referenced"):
            coredb.delete_vocabulary(mem_ws.id, "memory_type", "feedback")


# ---------------------------------------------------------------------------
# Reflections spike (Phase 3.6)
# ---------------------------------------------------------------------------


class TestReflectionsSpike:
    """Phase 3.6: store reflections as memories with memory_type='reflection'."""

    def test_store_reflection_as_memory(self, pg, mem_ws, mem_proj, seeded_vocab, server) -> None:
        reflection_body = """# Reflection 2026-05-20

## What worked
The phased implementation approach reduced risk.

## Gaps to flag
- [[BC-001]]: Need better error handling in embed module.
- [[BC-002]]: Connection pool sizing needs tuning.

## Sections
{"worked": ["phased approach"], "gaps": ["error handling", "pool tuning"]}
"""
        result = server._tool_add_memory(
            {
                "workspace": "mem-test-ws",
                "project": "mem-proj",
                "name": "reflection-2026-05-20",
                "memory_type": "reflection",
                "body": reflection_body,
                "attributes": {
                    "model": "glm-5.1",
                    "gaps_extracted_at": "2026-05-20T12:00:00Z",
                },
            }
        )
        assert "reflection-2026-05-20" in result

        # Retrieve and verify.
        result = server._tool_get_memory(
            {
                "workspace": "mem-test-ws",
                "project": "mem-proj",
                "name": "reflection-2026-05-20",
            }
        )
        assert "reflection" in result
        assert "Gaps to flag" in result

    def test_search_reflections_by_type(self, pg, mem_ws, mem_proj, seeded_vocab, server) -> None:
        result = server._tool_search_memory(
            {
                "workspace": "mem-test-ws",
                "project": "mem-proj",
                "query": "phased implementation gaps",
                "memory_type": "reflection",
            }
        )
        assert "reflection-2026-05-20" in result

    def test_reflection_gaps_have_wikilinks(self, pg, mem_ws, mem_proj, seeded_vocab) -> None:
        nodes = lnk.trace_graph(
            kind="memory",
            workspace=mem_ws.id,
            project=mem_proj.id,
            identifier="reflection-2026-05-20",
            direction="dependencies",
            max_depth=1,
        )
        identifiers = {n.identifier for n in nodes}
        assert "BC-001" in identifiers
        assert "BC-002" in identifiers
