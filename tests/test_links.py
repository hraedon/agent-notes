"""Integration tests for core/links.py against real Postgres."""

from __future__ import annotations

import pytest

from agent_notes.core import db as coredb
from agent_notes.core import links as lnk
from agent_notes.core.links import trace_graph


@pytest.fixture(scope="module")
def pg(ephemeral_db):
    return ephemeral_db


@pytest.fixture(scope="module")
def link_ws(pg):
    ws = coredb.get_or_create_workspace("links-test-ws", "Links Test WS")
    return ws


@pytest.fixture(scope="module")
def link_proj(link_ws):
    return coredb.get_or_create_project(link_ws.id, "links-proj", "Links Proj")


class TestAddLink:
    def test_add_link_writes_change_log(self, pg, link_ws, link_proj) -> None:
        from datetime import datetime, timezone

        from agent_notes.core import change_log as cl

        lnk.add_link(
            from_kind="breadcrumb",
            from_workspace=link_ws.id,
            from_project=link_proj.id,
            from_identifier="BC-LINKS-001",
            to_kind="memory",
            to_workspace=link_ws.id,
            to_project=link_proj.id,
            to_identifier="mem-links-001",
            relationship="relates_to",
        )
        since = datetime(2000, 1, 1, tzinfo=timezone.utc)
        rows = cl.changes_since(since, workspace_id=link_ws.id, kind="breadcrumb")
        link_added_rows = [r for r in rows if r.event == "link_added"]
        assert any(r.identifier == "BC-LINKS-001" for r in link_added_rows)

    def test_add_link_idempotent(self, pg, link_ws, link_proj) -> None:
        kwargs = dict(
            from_kind="bc",
            from_workspace=link_ws.id,
            from_project=link_proj.id,
            from_identifier="IDEM-A",
            to_kind="mem",
            to_workspace=link_ws.id,
            to_project=link_proj.id,
            to_identifier="IDEM-B",
            relationship="blocks",
        )
        lnk.add_link(**kwargs)
        lnk.add_link(**kwargs)  # ON CONFLICT DO NOTHING — no error


class TestRemoveLink:
    def test_remove_existing_returns_true(self, pg, link_ws, link_proj) -> None:
        kwargs = dict(
            from_kind="bc",
            from_workspace=link_ws.id,
            from_project=link_proj.id,
            from_identifier="REM-A",
            to_kind="bc",
            to_workspace=link_ws.id,
            to_project=link_proj.id,
            to_identifier="REM-B",
            relationship="blocks",
        )
        lnk.add_link(**kwargs)
        result = lnk.remove_link(**kwargs)
        assert result is True

    def test_remove_nonexistent_returns_false(self, pg, link_ws, link_proj) -> None:
        result = lnk.remove_link(
            from_kind="bc",
            from_workspace=link_ws.id,
            from_project=link_proj.id,
            from_identifier="GHOST-X",
            to_kind="bc",
            to_workspace=link_ws.id,
            to_project=link_proj.id,
            to_identifier="GHOST-Y",
            relationship="blocks",
        )
        assert result is False

    def test_remove_writes_change_log(self, pg, link_ws, link_proj) -> None:
        from datetime import datetime, timezone

        from agent_notes.core import change_log as cl

        kwargs = dict(
            from_kind="bc",
            from_workspace=link_ws.id,
            from_project=link_proj.id,
            from_identifier="REM-LOG-A",
            to_kind="bc",
            to_workspace=link_ws.id,
            to_project=link_proj.id,
            to_identifier="REM-LOG-B",
            relationship="relates_to",
        )
        lnk.add_link(**kwargs)
        lnk.remove_link(**kwargs)
        since = datetime(2000, 1, 1, tzinfo=timezone.utc)
        rows = cl.changes_since(since, workspace_id=link_ws.id, kind="bc")
        removed_rows = [r for r in rows if r.event == "link_removed"]
        assert any(r.identifier == "REM-LOG-A" for r in removed_rows)


class TestTraceGraph:
    def test_dependencies_direct(self, pg, link_ws, link_proj) -> None:
        # A → B → C
        for f, t in [("TG-A", "TG-B"), ("TG-B", "TG-C")]:
            lnk.add_link(
                from_kind="bc",
                from_workspace=link_ws.id,
                from_project=link_proj.id,
                from_identifier=f,
                to_kind="bc",
                to_workspace=link_ws.id,
                to_project=link_proj.id,
                to_identifier=t,
                relationship="blocks",
            )
        nodes = trace_graph(
            kind="bc",
            workspace=link_ws.id,
            project=link_proj.id,
            identifier="TG-A",
            direction="dependencies",
            max_depth=3,
        )
        identifiers = {n.identifier for n in nodes}
        assert "TG-B" in identifiers
        assert "TG-C" in identifiers

    def test_dependents_reverse(self, pg, link_ws, link_proj) -> None:
        # X → Y; ask dependents of Y → should return X
        lnk.add_link(
            from_kind="bc",
            from_workspace=link_ws.id,
            from_project=link_proj.id,
            from_identifier="DEP-X",
            to_kind="bc",
            to_workspace=link_ws.id,
            to_project=link_proj.id,
            to_identifier="DEP-Y",
            relationship="blocks",
        )
        nodes = trace_graph(
            kind="bc",
            workspace=link_ws.id,
            project=link_proj.id,
            identifier="DEP-Y",
            direction="dependents",
            max_depth=3,
        )
        identifiers = {n.identifier for n in nodes}
        assert "DEP-X" in identifiers

    def test_max_depth_respected(self, pg, link_ws, link_proj) -> None:
        # D1 → D2 → D3 → D4; max_depth=1 should only return D2
        for f, t in [("D1", "D2"), ("D2", "D3"), ("D3", "D4")]:
            lnk.add_link(
                from_kind="bc",
                from_workspace=link_ws.id,
                from_project=link_proj.id,
                from_identifier=f"DEPTH-{f}",
                to_kind="bc",
                to_workspace=link_ws.id,
                to_project=link_proj.id,
                to_identifier=f"DEPTH-{t}",
                relationship="relates_to",
            )
        nodes = trace_graph(
            kind="bc",
            workspace=link_ws.id,
            project=link_proj.id,
            identifier="DEPTH-D1",
            direction="dependencies",
            max_depth=1,
        )
        identifiers = {n.identifier for n in nodes}
        assert "DEPTH-D2" in identifiers
        assert "DEPTH-D3" not in identifiers

    def test_relationship_filter(self, pg, link_ws, link_proj) -> None:
        lnk.add_link(
            from_kind="bc",
            from_workspace=link_ws.id,
            from_project=link_proj.id,
            from_identifier="RF-A",
            to_kind="bc",
            to_workspace=link_ws.id,
            to_project=link_proj.id,
            to_identifier="RF-B",
            relationship="blocks",
        )
        lnk.add_link(
            from_kind="bc",
            from_workspace=link_ws.id,
            from_project=link_proj.id,
            from_identifier="RF-A",
            to_kind="bc",
            to_workspace=link_ws.id,
            to_project=link_proj.id,
            to_identifier="RF-C",
            relationship="relates_to",
        )
        nodes = trace_graph(
            kind="bc",
            workspace=link_ws.id,
            project=link_proj.id,
            identifier="RF-A",
            direction="dependencies",
            max_depth=3,
            relationship_kinds=["blocks"],
        )
        identifiers = {n.identifier for n in nodes}
        assert "RF-B" in identifiers
        assert "RF-C" not in identifiers
