"""Integration tests for the memory model layer (Phase 3).

Tests the model functions against ephemeral Postgres:
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
from unittest.mock import patch

import psycopg
import pytest
from psycopg.rows import dict_row

from agent_notes.core import change_log as cl
from agent_notes.core import db as coredb
from agent_notes.core import links as lnk
from agent_notes.core.memory_model import (
    add_memory,
    delete_memory,
    get_memory,
    list_memories,
    search_memory,
)
from agent_notes.core.memory_model import (
    parse_wikilinks as _parse_wikilinks,
)


def _fake_embed(text, task="document"):
    return [0.0] * 768


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
# add_memory
# ---------------------------------------------------------------------------


class TestAddMemory:
    def test_add_basic(self, pg, mem_ws, mem_proj, seeded_vocab) -> None:
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            row = add_memory(
                workspace_id=mem_ws.id,
                project_id=mem_proj.id,
                name="test-note-1",
                memory_type="note",
                body="This is a test memory note.",
                embedding=_fake_embed("test"),
            )
        assert row["name"] == "test-note-1"
        assert row["memory_type"] == "note"

    def test_add_with_attributes(self, pg, mem_ws, mem_proj, seeded_vocab) -> None:
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            row = add_memory(
                workspace_id=mem_ws.id,
                project_id=mem_proj.id,
                name="test-note-attr",
                memory_type="note",
                body="Body with attributes.",
                attributes={"priority": "high", "source": "test"},
                embedding=_fake_embed("test"),
            )
        assert row["name"] == "test-note-attr"
        assert row["attributes"]["priority"] == "high"

    def test_add_to_global_project(self, pg, mem_ws, global_proj, seeded_vocab) -> None:
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            row = add_memory(
                workspace_id=mem_ws.id,
                project_id=global_proj.id,
                name="global-memory",
                memory_type="feedback",
                body="Cross-cutting feedback memory.",
                embedding=_fake_embed("test"),
            )
        assert row["name"] == "global-memory"

    def test_add_writes_change_log(self, pg, mem_ws, mem_proj, seeded_vocab) -> None:
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            add_memory(
                workspace_id=mem_ws.id,
                project_id=mem_proj.id,
                name="cl-test-mem",
                memory_type="note",
                body="Change log test.",
                embedding=_fake_embed("test"),
            )
        since = datetime(2000, 1, 1, tzinfo=timezone.utc)
        rows = cl.changes_since(since, workspace_id=mem_ws.id, kind="memory")
        filed = [r for r in rows if r.event == "filed"]
        assert any(r.identifier == "cl-test-mem" for r in filed)

    def test_add_supersedes_existing(self, pg, mem_ws, mem_proj, seeded_vocab) -> None:
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            add_memory(
                workspace_id=mem_ws.id,
                project_id=mem_proj.id,
                name="supersede-test",
                memory_type="note",
                body="Original version.",
                embedding=_fake_embed("test"),
            )
            add_memory(
                workspace_id=mem_ws.id,
                project_id=mem_proj.id,
                name="supersede-test",
                memory_type="note",
                body="Updated version.",
                embedding=_fake_embed("test"),
            )

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

    def test_invalid_memory_type(self, pg, mem_ws, mem_proj) -> None:
        with pytest.raises(ValueError, match="not found"):
            add_memory(
                workspace_id=mem_ws.id,
                project_id=mem_proj.id,
                name="bad-type",
                memory_type="nonexistent",
                body="Should fail.",
            )

    def test_wikilinks_auto_linked(self, pg, mem_ws, mem_proj, seeded_vocab) -> None:
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            add_memory(
                workspace_id=mem_ws.id,
                project_id=mem_proj.id,
                name="wikilink-target",
                memory_type="note",
                body="I am the target.",
                embedding=_fake_embed("test"),
            )
            add_memory(
                workspace_id=mem_ws.id,
                project_id=mem_proj.id,
                name="wikilink-source",
                memory_type="note",
                body="See [[wikilink-target]] for details.",
                embedding=_fake_embed("test"),
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
# get_memory
# ---------------------------------------------------------------------------


class TestGetMemory:
    def test_get_existing(self, pg, mem_ws, mem_proj, seeded_vocab) -> None:
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            add_memory(
                workspace_id=mem_ws.id,
                project_id=mem_proj.id,
                name="get-test",
                memory_type="note",
                body="Gettable memory.",
                embedding=_fake_embed("test"),
            )
        result = get_memory(workspace_id=mem_ws.id, project_id=mem_proj.id, name="get-test")
        assert result is not None
        assert result["name"] == "get-test"
        assert "Gettable memory." in result["body"]

    def test_get_nonexistent(self, pg, mem_ws, mem_proj) -> None:
        result = get_memory(workspace_id=mem_ws.id, project_id=mem_proj.id, name="no-such-mem")
        assert result is None


# ---------------------------------------------------------------------------
# list_memories
# ---------------------------------------------------------------------------


class TestListMemories:
    def test_list_returns_memories(self, pg, mem_ws, mem_proj, seeded_vocab) -> None:
        rows = list_memories(workspace_id=mem_ws.id, project_id=mem_proj.id)
        assert len(rows) > 0

    def test_list_empty_project(self, pg, mem_ws, seeded_vocab) -> None:
        empty_proj = coredb.get_or_create_project(mem_ws.id, "empty-proj", "Empty")
        rows = list_memories(workspace_id=mem_ws.id, project_id=empty_proj.id)
        assert len(rows) == 0


# ---------------------------------------------------------------------------
# search_memory
# ---------------------------------------------------------------------------


class TestSearchMemory:
    def test_search_finds_relevant(self, pg, mem_ws, mem_proj, seeded_vocab) -> None:
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            add_memory(
                workspace_id=mem_ws.id,
                project_id=mem_proj.id,
                name="search-target",
                memory_type="note",
                body="PostgreSQL connection pooling with psycopg.",
                embedding=_fake_embed("test"),
            )
        rows = search_memory(
            workspace_id=mem_ws.id,
            query_vec=_fake_embed("database connection pool"),
            project_id=mem_proj.id,
        )
        names = {r["name"] for r in rows}
        assert "search-target" in names

    def test_search_body_elided_by_default(self, pg, mem_ws, mem_proj, seeded_vocab) -> None:
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            add_memory(
                workspace_id=mem_ws.id,
                project_id=mem_proj.id,
                name="elision-test",
                memory_type="note",
                body="A" * 500,
                embedding=_fake_embed("test"),
            )
        rows = search_memory(
            workspace_id=mem_ws.id,
            query_vec=_fake_embed("memory search test"),
            project_id=mem_proj.id,
        )
        for r in rows:
            if r["name"] == "elision-test":
                assert r["body"] == "" or r["body"] is None

    def test_search_filter_by_memory_type(self, pg, mem_ws, mem_proj, seeded_vocab) -> None:
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            add_memory(
                workspace_id=mem_ws.id,
                project_id=mem_proj.id,
                name="type-filter-decision",
                memory_type="decision",
                body="We decided to use PostgreSQL.",
                embedding=_fake_embed("test"),
            )
        rows = search_memory(
            workspace_id=mem_ws.id,
            query_vec=_fake_embed("PostgreSQL"),
            project_id=mem_proj.id,
            memory_type="decision",
        )
        names = {r["name"] for r in rows}
        assert "type-filter-decision" in names


# ---------------------------------------------------------------------------
# delete_memory
# ---------------------------------------------------------------------------


class TestDeleteMemory:
    def test_soft_delete(self, pg, mem_ws, mem_proj, seeded_vocab) -> None:
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            add_memory(
                workspace_id=mem_ws.id,
                project_id=mem_proj.id,
                name="delete-me",
                memory_type="note",
                body="Soon to be deleted.",
                embedding=_fake_embed("test"),
            )
        result = delete_memory(workspace_id=mem_ws.id, project_id=mem_proj.id, name="delete-me")
        assert result is not None

        # Should no longer be retrievable via get_memory (active only).
        result = get_memory(workspace_id=mem_ws.id, project_id=mem_proj.id, name="delete-me")
        assert result is None

    def test_delete_writes_change_log(self, pg, mem_ws, mem_proj, seeded_vocab) -> None:
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            add_memory(
                workspace_id=mem_ws.id,
                project_id=mem_proj.id,
                name="delete-cl-test",
                memory_type="note",
                body="For change_log delete.",
                embedding=_fake_embed("test"),
            )
        delete_memory(workspace_id=mem_ws.id, project_id=mem_proj.id, name="delete-cl-test")
        since = datetime(2000, 1, 1, tzinfo=timezone.utc)
        rows = cl.changes_since(since, workspace_id=mem_ws.id, kind="memory")
        deleted = [r for r in rows if r.event == "deleted"]
        assert any(r.identifier == "delete-cl-test" for r in deleted)

    def test_delete_nonexistent(self, pg, mem_ws, mem_proj) -> None:
        result = delete_memory(workspace_id=mem_ws.id, project_id=mem_proj.id, name="ghost")
        assert result is None


# ---------------------------------------------------------------------------
# trace_graph for memories
# ---------------------------------------------------------------------------


class TestTraceGraphMemory:
    def test_trace_graph_dependencies(self, pg, mem_ws, mem_proj, seeded_vocab) -> None:
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            add_memory(
                workspace_id=mem_ws.id,
                project_id=mem_proj.id,
                name="tg-root",
                memory_type="note",
                body="Root note linking to [[tg-child]].",
                embedding=_fake_embed("test"),
            )
            add_memory(
                workspace_id=mem_ws.id,
                project_id=mem_proj.id,
                name="tg-child",
                memory_type="note",
                body="Child note.",
                embedding=_fake_embed("test"),
            )
        nodes = lnk.trace_graph(
            kind="memory",
            workspace=mem_ws.id,
            project=mem_proj.id,
            identifier="tg-root",
            direction="dependencies",
            max_depth=1,
        )
        identifiers = {n.identifier for n in nodes}
        assert "tg-child" in identifiers


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

    def test_store_reflection_as_memory(self, pg, mem_ws, mem_proj, seeded_vocab) -> None:
        reflection_body = """# Reflection 2026-05-20

## What worked
The phased implementation approach reduced risk.

## Gaps to flag
- [[BC-001]]: Need better error handling in embed module.
- [[BC-002]]: Connection pool sizing needs tuning.

## Sections
{"worked": ["phased approach"], "gaps": ["error handling", "pool tuning"]}
"""
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            row = add_memory(
                workspace_id=mem_ws.id,
                project_id=mem_proj.id,
                name="reflection-2026-05-20",
                memory_type="reflection",
                body=reflection_body,
                attributes={
                    "model": "glm-5.1",
                    "gaps_extracted_at": "2026-05-20T12:00:00Z",
                },
                embedding=_fake_embed("test"),
            )
        assert row["name"] == "reflection-2026-05-20"

        # Retrieve and verify.
        result = get_memory(
            workspace_id=mem_ws.id,
            project_id=mem_proj.id,
            name="reflection-2026-05-20",
        )
        assert result is not None
        assert result["memory_type"] == "reflection"
        assert "Gaps to flag" in result["body"]

    def test_search_reflections_by_type(self, pg, mem_ws, mem_proj, seeded_vocab) -> None:
        rows = search_memory(
            workspace_id=mem_ws.id,
            query_vec=_fake_embed("phased implementation gaps"),
            project_id=mem_proj.id,
            memory_type="reflection",
        )
        names = {r["name"] for r in rows}
        assert "reflection-2026-05-20" in names

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
