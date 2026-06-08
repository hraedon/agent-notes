"""Tests for the breadcrumb → work-item migration script (Plan 008 Tier A #2)."""

from __future__ import annotations

import pytest

from agent_notes.core import db as coredb
from agent_notes.core.breadcrumbs_model import BreadcrumbModel
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


class TestMigrateBreadcrumbs:
    """Tests for breadcrumb → work-item migration.

    All tests share the session-scoped ephemeral_db fixture. Each test creates
    a unique workspace+project so migrations don't interfere.
    """

    _test_counter = 0

    def _seed_breadcrumb(self):
        TestMigrateBreadcrumbs._test_counter += 1
        n = TestMigrateBreadcrumbs._test_counter
        ws = coredb.get_or_create_workspace(f"mig-ws-{n}", f"Migration WS {n}")
        proj = coredb.get_or_create_project(
            ws.id,
            slug=f"mig-proj-{n}",
            name=f"Migration Proj {n}",
            repo_root="/tmp",
        )
        coredb.add_vocabulary(ws.id, "bc_kind", "bug")
        coredb.add_vocabulary(ws.id, "bc_status", "new")
        coredb.add_vocabulary(ws.id, "bc_status", "open")
        coredb.add_vocabulary(ws.id, "bc_status", "resolved")
        coredb.add_vocabulary(ws.id, "bc_severity", "medium")
        # Seed work-item vocabularies too
        coredb.add_vocabulary(ws.id, "wi_kind", "bug")
        coredb.add_vocabulary(ws.id, "wi_status", "open")
        coredb.add_vocabulary(ws.id, "wi_status", "closed", is_terminal=True, is_open=False)
        coredb.add_vocabulary(ws.id, "wi_status", "claimed")
        coredb.add_vocabulary(ws.id, "wi_status", "deferred", is_terminal=True, is_open=False)
        coredb.add_vocabulary(ws.id, "wi_severity", "medium")
        return ws, proj

    def test_migrate_single_breadcrumb(self):
        ws, proj = self._seed_breadcrumb()
        bc = BreadcrumbModel.file_breadcrumb(
            project_id=proj.id,
            title="Test bug",
            body="Something is broken",
            kind="bug",
            status="new",
            severity="medium",
        )

        from agent_notes.core.db import _conn
        from agent_notes.scripts.migrate_breadcrumbs_to_work_items import (
            _migrate_breadcrumbs,
        )

        with _conn() as conn:
            count = _migrate_breadcrumbs(conn)
            assert count >= 1
            conn.commit()

        # Verify the work item exists in the cache.
        from agent_notes.core.work_item_model import WorkItemModel

        wi = WorkItemModel.get_work_item(proj.id, bc["identifier"])
        assert wi is not None
        assert wi["title"] == "Test bug"
        assert wi["kind"] == "bug"
        assert wi["status"] == "open"  # new → open
        assert wi["identifier"] == bc["identifier"]

    def test_migrate_maps_resolved_to_closed(self):
        ws, proj = self._seed_breadcrumb()
        bc = BreadcrumbModel.file_breadcrumb(
            project_id=proj.id,
            title="Resolved bug",
            body="Fixed",
            kind="bug",
            status="resolved",
            severity="medium",
        )

        from agent_notes.core.db import _conn
        from agent_notes.scripts.migrate_breadcrumbs_to_work_items import (
            _migrate_breadcrumbs,
        )

        with _conn() as conn:
            count = _migrate_breadcrumbs(conn)
            assert count >= 1
            conn.commit()

        from agent_notes.core.work_item_model import WorkItemModel

        wi = WorkItemModel.get_work_item(proj.id, bc["identifier"])
        assert wi["status"] == "closed"  # resolved → closed
        assert wi["closed_at"] is not None

    def test_migrate_is_idempotent(self):
        ws, proj = self._seed_breadcrumb()
        BreadcrumbModel.file_breadcrumb(
            project_id=proj.id,
            title="Dup test",
            body="Body",
            kind="bug",
            status="new",
            severity="medium",
        )

        from agent_notes.core.db import _conn
        from agent_notes.scripts.migrate_breadcrumbs_to_work_items import (
            _migrate_breadcrumbs,
        )

        with _conn() as conn:
            count1 = _migrate_breadcrumbs(conn)
            assert count1 >= 1
            conn.commit()

        with _conn() as conn:
            count2 = _migrate_breadcrumbs(conn)
            assert count2 == 0  # Already migrated
            conn.commit()

    def test_migrate_links(self):
        ws, proj = self._seed_breadcrumb()
        bc1 = BreadcrumbModel.file_breadcrumb(
            project_id=proj.id,
            title="First",
            body="Body",
            kind="bug",
            status="new",
            severity="medium",
        )
        bc2 = BreadcrumbModel.file_breadcrumb(
            project_id=proj.id,
            title="Second",
            body="Body",
            kind="bug",
            status="new",
            severity="medium",
        )

        from agent_notes.core.links import add_link

        add_link(
            from_kind="breadcrumb",
            from_workspace=ws.id,
            from_project=proj.id,
            from_identifier=bc1["identifier"],
            to_kind="breadcrumb",
            to_workspace=ws.id,
            to_project=proj.id,
            to_identifier=bc2["identifier"],
            relationship="blocks",
        )

        from agent_notes.core.db import _conn
        from agent_notes.scripts.migrate_breadcrumbs_to_work_items import (
            _migrate_breadcrumbs,
            _migrate_links,
        )

        with _conn() as conn:
            _migrate_breadcrumbs(conn)
            links_count = _migrate_links(conn)
            conn.commit()

        assert links_count >= 2  # Both from and to kinds updated

        # Verify links are now work_item kind.
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT from_kind, to_kind FROM links WHERE from_project = %s",
                (proj.id,),
            )
            rows = cur.fetchall()
            for r in rows:
                assert r["from_kind"] == "work_item"
                assert r["to_kind"] == "work_item"

    def test_migrate_status_blocked(self):
        ws, proj = self._seed_breadcrumb()
        coredb.add_vocabulary(ws.id, "bc_status", "blocked")
        bc = BreadcrumbModel.file_breadcrumb(
            project_id=proj.id,
            title="Blocked",
            body="Blocked by something",
            kind="bug",
            status="blocked",
            severity="medium",
        )

        from agent_notes.core.db import _conn
        from agent_notes.scripts.migrate_breadcrumbs_to_work_items import (
            _migrate_breadcrumbs,
        )

        with _conn() as conn:
            count = _migrate_breadcrumbs(conn)
            assert count >= 1
            conn.commit()

        from agent_notes.core.work_item_model import WorkItemModel

        wi = WorkItemModel.get_work_item(proj.id, bc["identifier"])
        assert wi["status"] == "open"  # blocked → open
