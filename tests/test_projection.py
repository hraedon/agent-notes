"""Tests for the local projection mirror (Plan 009 P1)."""

from __future__ import annotations

import uuid

import pytest
from psycopg.rows import dict_row

from agent_notes.core import db as coredb
from agent_notes.core import projection
from agent_notes.core.db import _conn
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


def _vec768():
    return [0.01] * 768


@pytest.fixture
def default_project():
    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    return coredb.get_or_create_project(
        ws.id,
        slug="sf2",
        name="sf2",
        repo_root="/projects/sf2",
    )


class TestStateToStatus:
    def test_maps_known_states(self):
        # canonical v2 states
        assert projection.state_to_status("open") == "open"
        assert projection.state_to_status("in_progress") == "in_progress"
        assert projection.state_to_status("blocked") == "blocked"
        assert projection.state_to_status("deferred") == "deferred"
        assert projection.state_to_status("in_review") == "in_review"
        assert projection.state_to_status("in_human_review") == "in_human_review"
        assert projection.state_to_status("done") == "done"
        # legacy breadcrumb v1 states (pre-migration items)
        assert projection.state_to_status("claimed") == "claimed"
        assert projection.state_to_status("closed") == "closed"

    def test_unknown_state_raises(self):
        with pytest.raises(ValueError, match="unknown regista work-item state"):
            projection.state_to_status("invalid")


class TestMirrorFromRegista:
    def test_inserts_then_updates_projection_row(self, default_project):
        wid = uuid.uuid4()
        entity_id = f"wi-{wid}"
        custom_fields = {
            "title": "Title",
            "description": "Body",
            "severity": "high",
            "kind": "bug",
            "external_refs": {"ref": "x"},
            "diagnostic_keys": {"key": "y"},
        }
        with _conn() as conn:
            row1 = projection.mirror_from_regista(
                conn,
                project_id=default_project.id,
                identifier="WI-101",
                entity_id=entity_id,
                regista_work_item_id=wid,
                state="open",
                custom_fields=custom_fields,
                embed=None,
                actor_id="actor-1",
            )
            assert row1["identifier"] == "WI-101"
            assert row1["status"] == "open"
            assert row1["title"] == "Title"
            assert row1["severity"] == "high"
            assert row1["kind"] == "bug"
            assert row1["regista_work_item_id"] == wid
            assert row1["pending_sync"] is False
            conn.commit()

        with _conn() as conn:
            updated_custom_fields = dict(custom_fields)
            updated_custom_fields["title"] = "New title"
            updated_custom_fields["description"] = "New body"
            row2 = projection.mirror_from_regista(
                conn,
                project_id=default_project.id,
                identifier="WI-101",
                entity_id=entity_id,
                regista_work_item_id=wid,
                state="claimed",
                custom_fields=updated_custom_fields,
                embed=None,
                actor_id="actor-1",
            )
            assert row2["id"] == row1["id"]
            assert row2["status"] == "claimed"
            assert row2["title"] == "New title"
            conn.commit()

    def test_sets_embedding_via_embed_fn(self, default_project):
        wid = uuid.uuid4()
        entity_id = f"wi-{wid}"
        with _conn() as conn:
            projection.mirror_from_regista(
                conn,
                project_id=default_project.id,
                identifier="WI-102",
                entity_id=entity_id,
                regista_work_item_id=wid,
                state="open",
                custom_fields={"title": "T", "description": "B"},
                embed=lambda text: _vec768(),
                actor_id="actor-1",
            )
            conn.commit()

        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT embedding FROM work_items WHERE identifier = %s",
                ("WI-102",),
            )
            row = cur.fetchone()
        assert row["embedding"] is not None
        assert len(row["embedding"]) > 0

    def test_pending_sync_column(self):
        ws = coredb.get_or_create_workspace("syncflag", "Sync Flag Workspace")
        project = coredb.get_or_create_project(ws.id, slug="syncflag", name="syncflag")
        wid = uuid.uuid4()
        entity_id = f"wi-{wid}"
        with _conn() as conn:
            projection.mirror_from_regista(
                conn,
                project_id=project.id,
                identifier="WI-103",
                entity_id=entity_id,
                regista_work_item_id=wid,
                state="open",
                custom_fields={"title": "T", "description": "B"},
                pending_sync=True,
                actor_id="actor-1",
            )
            conn.commit()

        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT pending_sync FROM work_items WHERE identifier = %s",
                ("WI-103",),
            )
            assert cur.fetchone()["pending_sync"] is True

    def test_closed_state_sets_closed_at(self, default_project):
        wid = uuid.uuid4()
        entity_id = f"wi-{wid}"
        with _conn() as conn:
            projection.mirror_from_regista(
                conn,
                project_id=default_project.id,
                identifier="WI-104",
                entity_id=entity_id,
                regista_work_item_id=wid,
                state="closed",
                custom_fields={"title": "T", "description": "B"},
                actor_id="actor-1",
            )
            conn.commit()

        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT closed_at FROM work_items WHERE identifier = %s",
                ("WI-104",),
            )
            assert cur.fetchone()["closed_at"] is not None

    def test_reopen_clears_closed_at(self, default_project):
        wid = uuid.uuid4()
        entity_id = f"wi-{wid}"
        with _conn() as conn:
            projection.mirror_from_regista(
                conn,
                project_id=default_project.id,
                identifier="WI-105",
                entity_id=entity_id,
                regista_work_item_id=wid,
                state="closed",
                custom_fields={"title": "T", "description": "B"},
                actor_id="actor-1",
            )
            projection.mirror_from_regista(
                conn,
                project_id=default_project.id,
                identifier="WI-105",
                entity_id=entity_id,
                regista_work_item_id=wid,
                state="open",
                custom_fields={"title": "T", "description": "B"},
                actor_id="actor-1",
            )
            conn.commit()

        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT closed_at FROM work_items WHERE identifier = %s",
                ("WI-105",),
            )
            assert cur.fetchone()["closed_at"] is None


class TestPendingSyncQueries:
    def test_count_pending_and_find_local(self):
        ws = coredb.get_or_create_workspace("pending", "Pending Workspace")
        project = coredb.get_or_create_project(ws.id, slug="pending", name="pending")
        wid = uuid.uuid4()
        entity_id = f"wi-{wid}"
        with _conn() as conn:
            projection.mirror_from_regista(
                conn,
                project_id=project.id,
                identifier="WI-PENDING",
                entity_id=entity_id,
                regista_work_item_id=wid,
                state="open",
                custom_fields={"title": "T", "description": "B"},
                pending_sync=True,
                actor_id="actor-1",
            )
            conn.commit()

        with _conn() as conn:
            assert projection.count_pending(conn) >= 1
            assert projection.count_pending(conn, project_id=project.id) == 1
            found = projection.find_local_for_regista(conn, wid)
            assert found is not None
            assert found["identifier"] == "WI-PENDING"
            assert found["pending_sync"] is True

    def test_count_pending_filters_other_projects(self):
        ws = coredb.get_or_create_workspace("pending2", "Pending Workspace 2")
        project = coredb.get_or_create_project(ws.id, slug="pending2", name="pending2")
        ws_other = coredb.get_or_create_workspace("other2", "Other Workspace 2")
        other_project = coredb.get_or_create_project(ws_other.id, slug="other2", name="other2")

        wid = uuid.uuid4()
        entity_id = f"wi-{wid}"
        with _conn() as conn:
            projection.mirror_from_regista(
                conn,
                project_id=project.id,
                identifier="WI-A",
                entity_id=entity_id,
                regista_work_item_id=wid,
                state="open",
                custom_fields={"title": "T", "description": "B"},
                pending_sync=True,
                actor_id="actor-1",
            )
            conn.commit()

        with _conn() as conn:
            assert projection.count_pending(conn, project_id=other_project.id) == 0
            assert projection.count_pending(conn, project_id=project.id) == 1
            assert projection.count_pending(conn) >= 1
