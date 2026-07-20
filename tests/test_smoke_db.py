"""Smoke tests for core/db.py against a real ephemeral Postgres (decision 1b.6).

Requires Docker. Run with: make test (or pytest tests/test_smoke_db.py).
"""

from __future__ import annotations

import os
from pathlib import Path

import docker.errors
import psycopg
import pytest
from testcontainers.postgres import PostgresContainer

from agent_notes.core import db as coredb

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schema"


def _apply_schema(dsn: str) -> None:
    sql_files = sorted(SCHEMA_DIR.glob("*.sql"))
    for f in sql_files:
        sql = f.read_text(encoding="utf-8")
        with psycopg.connect(dsn) as conn:
            with conn.transaction():
                conn.execute(sql)


@pytest.fixture(scope="module")
def pg():
    # Check Docker availability BEFORE constructing the container: in current
    # testcontainers, PostgresContainer.__init__ eagerly builds a DockerClient,
    # which raises DockerException on a Docker-less host (e.g. windows-latest
    # CI) outside any try/except — erroring instead of skipping. Mirror the
    # conftest ephemeral_db fixture, which pings first and skips cleanly.
    try:
        docker.from_env().ping()
    except (docker.errors.DockerException, OSError) as exc:
        pytest.skip(f"Docker unavailable: {exc}")

    container = PostgresContainer("pgvector/pgvector:pg17")
    try:
        container.start()
    except Exception as exc:
        pytest.skip(f"Postgres test container could not start: {exc}")
    try:
        dsn = container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        _apply_schema(dsn)
        old = os.environ.get("AGENT_NOTES_DSN")
        os.environ["AGENT_NOTES_DSN"] = dsn
        coredb._pool = None
        yield dsn
        # Teardown
        if old is None:
            del os.environ["AGENT_NOTES_DSN"]
        else:
            os.environ["AGENT_NOTES_DSN"] = old
        if coredb._pool is not None:
            coredb._pool.close()
            coredb._pool = None
    finally:
        container.stop()


class TestWorkspaces:
    def test_seed_workspace_exists(self, pg: str) -> None:
        workspaces = coredb.list_workspaces()
        slugs = {w.slug for w in workspaces}
        assert "default" in slugs

    def test_get_or_create_workspace_idempotent(self, pg: str) -> None:
        w1 = coredb.get_or_create_workspace("test-ws", "Test Workspace")
        w2 = coredb.get_or_create_workspace("test-ws", "Test Workspace Renamed")
        assert w1.id == w2.id
        assert w2.name == "Test Workspace Renamed"

    def test_list_workspaces(self, pg: str) -> None:
        coredb.get_or_create_workspace("list-test", "List Test")
        workspaces = coredb.list_workspaces()
        assert any(w.slug == "list-test" for w in workspaces)


class TestProjects:
    def test_seed_global_project_exists(self, pg: str) -> None:
        default_ws = next(w for w in coredb.list_workspaces() if w.slug == "default")
        projects = coredb.list_projects(workspace_id=default_ws.id)
        slugs = {p.slug for p in projects}
        assert "global" in slugs

    def test_get_or_create_project_idempotent(self, pg: str) -> None:
        ws = coredb.get_or_create_workspace("proj-ws", "Proj WS")
        p1 = coredb.get_or_create_project(ws.id, "my-proj", "My Project", repo_root="/tmp/repo")
        p2 = coredb.get_or_create_project(ws.id, "my-proj", "My Project v2")
        assert p1.id == p2.id
        assert p2.name == "My Project v2"
        assert p2.repo_root == "/tmp/repo"

    def test_list_projects_filter_by_workspace(self, pg: str) -> None:
        ws = coredb.get_or_create_workspace("filter-ws", "Filter WS")
        coredb.get_or_create_project(ws.id, "alpha", "Alpha")
        coredb.get_or_create_project(ws.id, "beta", "Beta")
        projects = coredb.list_projects(workspace_id=ws.id)
        slugs = {p.slug for p in projects}
        assert "alpha" in slugs
        assert "beta" in slugs


class TestVocabulary:
    def test_add_and_list_vocabulary(self, pg: str) -> None:
        ws = coredb.get_or_create_workspace("vocab-ws", "Vocab WS")
        coredb.add_vocabulary(ws.id, "bc_status", "open", is_terminal=False, is_open=True)
        coredb.add_vocabulary(ws.id, "bc_status", "resolved", is_terminal=True, is_open=False)
        vocabs = coredb.list_vocabulary(ws.id, kind_namespace="bc_status")
        names = {v.name for v in vocabs}
        assert "open" in names
        assert "resolved" in names

    def test_archive_vocabulary(self, pg: str) -> None:
        ws = coredb.get_or_create_workspace("arch-ws", "Archive WS")
        coredb.add_vocabulary(ws.id, "bc_kind", "bug")
        coredb.archive_vocabulary(ws.id, "bc_kind", "bug")
        active = coredb.list_vocabulary(ws.id, kind_namespace="bc_kind", include_archived=False)
        archived = coredb.list_vocabulary(ws.id, kind_namespace="bc_kind", include_archived=True)
        assert not any(v.name == "bug" for v in active)
        assert any(v.name == "bug" for v in archived)

    def test_delete_vocabulary(self, pg: str) -> None:
        ws = coredb.get_or_create_workspace("del-ws", "Delete WS")
        coredb.add_vocabulary(ws.id, "bc_kind", "task")
        coredb.delete_vocabulary(ws.id, "bc_kind", "task")
        vocabs = coredb.list_vocabulary(ws.id, kind_namespace="bc_kind", include_archived=True)
        assert not any(v.name == "task" for v in vocabs)

    def test_vocabulary_is_terminal_column(self, pg: str) -> None:
        ws = coredb.get_or_create_workspace("term-ws", "Terminal WS")
        coredb.add_vocabulary(
            ws.id, "bc_status", "wont_fix", is_terminal=True, is_open=False, sort_order=200
        )
        vocabs = coredb.list_vocabulary(ws.id, kind_namespace="bc_status")
        v = next(x for x in vocabs if x.name == "wont_fix")
        assert v.is_terminal is True
        assert v.is_open is False
        assert v.sort_order == 200


class TestVocabularyReferences:
    """Reference-check semantics for delete_vocabulary (decision 9).

    Covers the ``bc_*`` -> ``wi_*`` namespace mapping in
    ``_check_vocab_references``: deleting a legacy ``bc_kind`` entry must be
    blocked when a work_item references the mapped ``wi_kind``.
    """

    @staticmethod
    def _seeded_ws(prefix: str):
        ws = coredb.get_or_create_workspace(prefix, prefix)
        # work_item vocabularies that file_work_item validates against
        coredb.add_vocabulary(ws.id, "wi_kind", "bug", is_terminal=False, is_open=True)
        coredb.add_vocabulary(ws.id, "wi_status", "open", is_terminal=False, is_open=True)
        coredb.add_vocabulary(ws.id, "wi_severity", "medium", is_terminal=False, is_open=True)
        proj = coredb.get_or_create_project(ws.id, f"{prefix}-proj", f"{prefix} Proj")
        return ws, proj

    def test_delete_wi_kind_referenced_raises(self, pg: str) -> None:
        from agent_notes.core.work_item_model import WorkItemModel

        ws, proj = self._seeded_ws("ref-wi-kind")
        WorkItemModel.file_work_item(
            project_id=proj.id,
            identifier="WI-VOC-01",
            title="Vocab ref",
            kind="bug",
            status="open",
            severity="medium",
        )
        with pytest.raises(ValueError, match="referenced"):
            coredb.delete_vocabulary(ws.id, "wi_kind", "bug")

    def test_delete_bc_kind_referenced_raises(self, pg: str) -> None:
        """bc_kind is a legacy alias mapped to wi_kind — deleting it must still
        be blocked when a work_item references the kind (bc->wi mapping)."""
        from agent_notes.core.work_item_model import WorkItemModel

        ws, proj = self._seeded_ws("ref-bc-kind")
        # add the legacy bc_kind entry so the delete target genuinely exists
        coredb.add_vocabulary(ws.id, "bc_kind", "bug", is_terminal=False, is_open=True)
        WorkItemModel.file_work_item(
            project_id=proj.id,
            identifier="WI-VOC-02",
            title="Vocab ref bc",
            kind="bug",
            status="open",
            severity="medium",
        )
        with pytest.raises(ValueError, match="referenced"):
            coredb.delete_vocabulary(ws.id, "bc_kind", "bug")

    def test_delete_wi_status_referenced_raises(self, pg: str) -> None:
        from agent_notes.core.work_item_model import WorkItemModel

        ws, proj = self._seeded_ws("ref-wi-status")
        WorkItemModel.file_work_item(
            project_id=proj.id,
            identifier="WI-VOC-03",
            title="Vocab ref status",
            kind="bug",
            status="open",
            severity="medium",
        )
        with pytest.raises(ValueError, match="referenced"):
            coredb.delete_vocabulary(ws.id, "wi_status", "open")


class TestLinks:
    def test_add_and_remove_link(self, pg: str) -> None:
        from agent_notes.core import links

        ws = coredb.get_or_create_workspace("link-ws", "Link WS")
        p = coredb.get_or_create_project(ws.id, "link-proj", "Link Proj")
        links.add_link(
            from_kind="breadcrumb",
            from_workspace=ws.id,
            from_project=p.id,
            from_identifier="BC-001",
            to_kind="memory",
            to_workspace=ws.id,
            to_project=p.id,
            to_identifier="mem-foo",
            relationship="relates_to",
        )
        removed = links.remove_link(
            from_kind="breadcrumb",
            from_workspace=ws.id,
            from_project=p.id,
            from_identifier="BC-001",
            to_kind="memory",
            to_workspace=ws.id,
            to_project=p.id,
            to_identifier="mem-foo",
            relationship="relates_to",
        )
        assert removed is True

    def test_remove_nonexistent_link(self, pg: str) -> None:
        from agent_notes.core import links

        ws = coredb.get_or_create_workspace("link-ws2", "Link WS2")
        p = coredb.get_or_create_project(ws.id, "link-proj2", "Link Proj2")
        removed = links.remove_link(
            from_kind="breadcrumb",
            from_workspace=ws.id,
            from_project=p.id,
            from_identifier="X",
            to_kind="memory",
            to_workspace=ws.id,
            to_project=p.id,
            to_identifier="Y",
            relationship="relates_to",
        )
        assert removed is False

    def test_add_link_idempotent(self, pg: str) -> None:
        from agent_notes.core import links

        ws = coredb.get_or_create_workspace("link-ws3", "Link WS3")
        p = coredb.get_or_create_project(ws.id, "link-proj3", "Link Proj3")
        kwargs = dict(
            from_kind="bc",
            from_workspace=ws.id,
            from_project=p.id,
            from_identifier="A",
            to_kind="mem",
            to_workspace=ws.id,
            to_project=p.id,
            to_identifier="B",
            relationship="blocks",
        )
        links.add_link(**kwargs)
        links.add_link(**kwargs)  # second call is ON CONFLICT DO NOTHING — no error


class TestChangeLog:
    def test_changes_since_returns_rows(self, pg: str) -> None:
        from datetime import datetime, timezone

        ws = coredb.get_or_create_workspace("cl-ws", "ChangeLog WS")
        p = coredb.get_or_create_project(ws.id, "cl-proj", "ChangeLog Proj")
        with psycopg.connect(pg) as conn:
            conn.execute(
                "INSERT INTO change_log (kind, workspace_id, project_id, identifier, event) "
                "VALUES (%s, %s, %s, %s, %s)",
                ("breadcrumb", ws.id, p.id, "BC-001", "filed"),
            )
            conn.commit()
        since = datetime(2000, 1, 1, tzinfo=timezone.utc)
        rows = coredb.changes_since(since, workspace_id=ws.id)
        assert len(rows) >= 1
        assert rows[0]["event"] == "filed"


class TestSetupIdempotent:
    def test_rerun_migration_is_noop(self, pg: str) -> None:
        """Running the schema again against an already-set-up DB must not fail."""
        _apply_schema(pg)
        workspaces = coredb.list_workspaces()
        assert any(w.slug == "default" for w in workspaces)
