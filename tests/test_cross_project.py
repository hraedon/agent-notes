"""Integration tests for Plan 008 P3 — cross-project layer."""

from __future__ import annotations

import argparse
import json

import pytest

from agent_notes.core import cross_project as cp
from agent_notes.core import db as coredb
from agent_notes.core.work_item_model import WorkItemModel

# Import the fixture so pytest discovers it
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


@pytest.fixture
def default_project():
    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    proj = coredb.get_or_create_project(
        ws.id,
        slug="sf2",
        name="sf2",
        repo_root="/projects/sf2",
    )
    return proj


@pytest.fixture
def target_project():
    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    proj = coredb.get_or_create_project(
        ws.id,
        slug="target",
        name="Target Project",
        repo_root="/projects/target",
    )
    return proj


def _vec768():
    return [0.0] * 768


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_update_project_registry(self, default_project):
        cp.update_project_registry(
            default_project.id,
            log_location="/var/log/agent-notes/sf2.jsonl",
            wake_channel="http://127.0.0.1:8788/sf2",
        )
        projects = coredb.list_projects(workspace_id=default_project.workspace_id)
        proj = next(p for p in projects if p.slug == "sf2")
        assert proj.log_location == "/var/log/agent-notes/sf2.jsonl"
        assert proj.wake_channel == "http://127.0.0.1:8788/sf2"

    def test_get_or_create_project_with_registry(self, default_project):
        ws = coredb.get_or_create_workspace("reg-ws", "Registry Workspace")
        proj = coredb.get_or_create_project(
            ws.id,
            slug="reg-proj",
            name="Registry Project",
            repo_root="/projects/reg",
            log_location="/logs/reg.jsonl",
            wake_channel="http://wake/reg",
        )
        assert proj.log_location == "/logs/reg.jsonl"
        assert proj.wake_channel == "http://wake/reg"

    def test_resolve_project_returns_registry(self, default_project):
        cp.update_project_registry(
            default_project.id,
            log_location="/logs/sf2.jsonl",
            wake_channel="http://wake/sf2",
        )
        result = coredb.resolve_project("/projects/sf2")
        assert result["log_location"] == "/logs/sf2.jsonl"
        assert result["wake_channel"] == "http://wake/sf2"


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


class TestExportOps:
    def test_export_empty_project(self, default_project):
        jsonl = cp.export_ops_jsonl(default_project.id)
        assert jsonl == ""

    def test_export_work_item_ops(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-EXPORT-01",
            title="Export test",
            body="Body text",
            status="open",
            embedding=_vec768(),
        )
        jsonl = cp.export_ops_jsonl(default_project.id)
        assert jsonl.strip()
        lines = jsonl.strip().split("\n")
        assert len(lines) >= 1
        first = json.loads(lines[0])
        assert first["entity_type"] == "work_item"
        assert "op_id" in first
        assert "envelope" not in first.get("payload", {})

    def test_export_excludes_other_project(self, default_project, target_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-EXPORT-02",
            title="Local item",
            embedding=_vec768(),
        )
        WorkItemModel.file_work_item(
            project_id=target_project.id,
            identifier="WI-EXPORT-03",
            title="Other item",
            embedding=_vec768(),
        )
        jsonl = cp.export_ops_jsonl(default_project.id)
        lines = jsonl.strip().split("\n")
        ids = {json.loads(line)["payload"]["identifier"] for line in lines}
        assert "WI-EXPORT-02" in ids
        assert "WI-EXPORT-03" not in ids


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


class TestIngestOps:
    def test_ingest_empty(self):
        count = cp.ingest_jsonl_ops("", "foreign-proj")
        assert count == 0

    def test_ingest_single_op(self):
        op = {
            "op_id": "abc123",
            "entity_id": "ent456",
            "entity_type": "work_item",
            "op_type": "create",
            "lamport": 1,
            "actor_id": "test-actor",
            "payload": {
                "title": "Foreign item",
                "project_id": 1,
                "identifier": "WI-F-01",
                "status": "open",
            },
            "parent_op_ids": [],
        }
        jsonl = json.dumps(op) + "\n"
        count = cp.ingest_jsonl_ops(jsonl, "foreign-proj")
        assert count == 1

    def test_ingest_idempotent(self):
        op = {
            "op_id": "abc123",
            "entity_id": "ent456",
            "entity_type": "work_item",
            "op_type": "create",
            "lamport": 1,
            "actor_id": "test-actor",
            "payload": {
                "title": "Foreign item",
                "project_id": 1,
                "identifier": "WI-F-01",
                "status": "open",
            },
            "parent_op_ids": [],
        }
        jsonl = json.dumps(op) + "\n"
        cp.ingest_jsonl_ops(jsonl, "foreign-proj")
        count = cp.ingest_jsonl_ops(jsonl, "foreign-proj")
        assert count == 1  # still counts as 1 op processed

    def test_ingest_updates_freshness(self):
        op = {
            "op_id": "abc123",
            "entity_id": "ent456",
            "entity_type": "work_item",
            "op_type": "create",
            "lamport": 1,
            "payload": {
                "title": "Foreign item",
                "project_id": 1,
                "identifier": "WI-F-01",
                "status": "open",
            },
            "parent_op_ids": [],
        }
        cp.ingest_jsonl_ops(json.dumps(op) + "\n", "foreign-proj")
        fresh = cp.get_freshness("foreign-proj")
        assert fresh is not None
        assert fresh["source_project_slug"] == "foreign-proj"


# ---------------------------------------------------------------------------
# Fold / cache
# ---------------------------------------------------------------------------


class TestFoldCrossProject:
    def test_fold_single_create(self):
        op = {
            "op_id": "abc123",
            "entity_id": "ent456",
            "entity_type": "work_item",
            "op_type": "create",
            "lamport": 1,
            "payload": {
                "title": "Foreign item",
                "project_id": 1,
                "identifier": "WI-F-01",
                "status": "open",
                "body_hash": "hash1",
            },
            "parent_op_ids": [],
        }
        cp.ingest_jsonl_ops(json.dumps(op) + "\n", "foreign-proj")
        result = cp.rebuild_cross_project_cache()
        assert result == 1

        wi = cp.get_cross_project_work_item("foreign-proj", "WI-F-01")
        assert wi is not None
        assert wi["title"] == "Foreign item"
        assert wi["status"] == "open"

    def test_fold_status_change(self):
        ops = [
            {
                "op_id": "abc123",
                "entity_id": "ent456",
                "entity_type": "work_item",
                "op_type": "create",
                "lamport": 1,
                "payload": {
                    "title": "Foreign item",
                    "project_id": 1,
                    "identifier": "WI-F-02",
                    "status": "open",
                },
                "parent_op_ids": [],
            },
            {
                "op_id": "abc124",
                "entity_id": "ent456",
                "entity_type": "work_item",
                "op_type": "close",
                "lamport": 2,
                "payload": {"reason": "done"},
                "parent_op_ids": ["abc123"],
            },
        ]
        jsonl = "\n".join(json.dumps(op) for op in ops) + "\n"
        cp.ingest_jsonl_ops(jsonl, "foreign-proj")
        cp.rebuild_cross_project_cache()

        wi = cp.get_cross_project_work_item("foreign-proj", "WI-F-02")
        assert wi is not None
        assert wi["status"] == "closed"

    def test_list_cross_project_work_items(self):
        op = {
            "op_id": "abc123",
            "entity_id": "ent456",
            "entity_type": "work_item",
            "op_type": "create",
            "lamport": 1,
            "payload": {
                "title": "Foreign item",
                "project_id": 1,
                "identifier": "WI-F-03",
                "status": "open",
            },
            "parent_op_ids": [],
        }
        cp.ingest_jsonl_ops(json.dumps(op) + "\n", "foreign-proj")
        cp.rebuild_cross_project_cache()

        items = cp.list_cross_project_work_items(source_project_slug="foreign-proj")
        assert len(items) == 1
        assert items[0]["identifier"] == "WI-F-03"


# ---------------------------------------------------------------------------
# Cross-repo links
# ---------------------------------------------------------------------------


class TestCrossRepoLinks:
    def test_add_cross_repo_link(self, default_project):
        link = cp.add_cross_repo_link(
            from_project_id=default_project.id,
            from_identifier="WI-LOCAL-01",
            to_project_slug="other-proj",
            to_identifier="WI-OTHER-01",
            relationship="blocks",
        )
        assert link["from_project_id"] == default_project.id
        assert link["from_identifier"] == "WI-LOCAL-01"
        assert link["to_project_slug"] == "other-proj"
        assert link["to_identifier"] == "WI-OTHER-01"

    def test_remove_cross_repo_link(self, default_project):
        cp.add_cross_repo_link(
            from_project_id=default_project.id,
            from_identifier="WI-LOCAL-02",
            to_project_slug="other-proj",
            to_identifier="WI-OTHER-02",
        )
        deleted = cp.remove_cross_repo_link(
            from_project_id=default_project.id,
            from_identifier="WI-LOCAL-02",
            to_project_slug="other-proj",
            to_identifier="WI-OTHER-02",
        )
        assert deleted is True

    def test_remove_missing_link(self, default_project):
        deleted = cp.remove_cross_repo_link(
            from_project_id=default_project.id,
            from_identifier="WI-LOCAL-NO",
            to_project_slug="other-proj",
            to_identifier="WI-OTHER-NO",
        )
        assert deleted is False


# ---------------------------------------------------------------------------
# Cross-project ready query (blockers via derived index)
# ---------------------------------------------------------------------------


class TestCrossProjectReady:
    def test_ready_excludes_cross_repo_blocked(self, default_project):
        # Create a local work item
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-LOCAL-BLOCKED",
            title="Blocked by foreign",
            status="open",
            embedding=_vec768(),
        )

        # Add a cross-repo link saying it's blocked by a foreign item
        cp.add_cross_repo_link(
            from_project_id=default_project.id,
            from_identifier="WI-LOCAL-BLOCKED",
            to_project_slug="foreign-proj",
            to_identifier="WI-FOREIGN-BLOCKER",
        )

        # Ingest the foreign blocker as open
        op = {
            "op_id": "foreign1",
            "entity_id": "foreign-ent",
            "entity_type": "work_item",
            "op_type": "create",
            "lamport": 1,
            "payload": {
                "title": "Foreign blocker",
                "project_id": 1,
                "identifier": "WI-FOREIGN-BLOCKER",
                "status": "open",
            },
            "parent_op_ids": [],
        }
        cp.ingest_jsonl_ops(json.dumps(op) + "\n", "foreign-proj")
        cp.rebuild_cross_project_cache()

        # The local item should NOT be ready (blocked by foreign)
        ready = WorkItemModel.ready_work_items(project_id=default_project.id)
        ids = {r["identifier"] for r in ready}
        assert "WI-LOCAL-BLOCKED" not in ids

    def test_ready_includes_when_foreign_blocker_closed(self, default_project):
        # Create a local work item
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-LOCAL-UNBLOCKED",
            title="Unblocked by foreign close",
            status="open",
            embedding=_vec768(),
        )

        cp.add_cross_repo_link(
            from_project_id=default_project.id,
            from_identifier="WI-LOCAL-UNBLOCKED",
            to_project_slug="foreign-proj",
            to_identifier="WI-FOREIGN-CLOSED",
        )

        # Ingest the foreign blocker as closed
        ops = [
            {
                "op_id": "foreign1",
                "entity_id": "foreign-ent",
                "entity_type": "work_item",
                "op_type": "create",
                "lamport": 1,
                "payload": {
                    "title": "Foreign blocker",
                    "project_id": 1,
                    "identifier": "WI-FOREIGN-CLOSED",
                    "status": "open",
                },
                "parent_op_ids": [],
            },
            {
                "op_id": "foreign2",
                "entity_id": "foreign-ent",
                "entity_type": "work_item",
                "op_type": "close",
                "lamport": 2,
                "payload": {"reason": "done"},
                "parent_op_ids": ["foreign1"],
            },
        ]
        cp.ingest_jsonl_ops("\n".join(json.dumps(op) for op in ops) + "\n", "foreign-proj")
        cp.rebuild_cross_project_cache()

        # The local item SHOULD be ready (foreign blocker is closed)
        ready = WorkItemModel.ready_work_items(project_id=default_project.id)
        ids = {r["identifier"] for r in ready}
        assert "WI-LOCAL-UNBLOCKED" in ids


# ---------------------------------------------------------------------------
# Reverse-edge map (wake routing)
# ---------------------------------------------------------------------------


class TestReverseEdges:
    def test_get_blocked_by(self, default_project):
        cp.add_cross_repo_link(
            from_project_id=default_project.id,
            from_identifier="WI-LOCAL-A",
            to_project_slug="foreign-proj",
            to_identifier="WI-FOREIGN-X",
        )
        cp.add_cross_repo_link(
            from_project_id=default_project.id,
            from_identifier="WI-LOCAL-B",
            to_project_slug="foreign-proj",
            to_identifier="WI-FOREIGN-X",
        )

        blocked = cp.get_blocked_by("foreign-proj", "WI-FOREIGN-X")
        identifiers = {b["from_identifier"] for b in blocked}
        assert "WI-LOCAL-A" in identifiers
        assert "WI-LOCAL-B" in identifiers

    def test_get_cross_project_blockers(self, default_project):
        cp.add_cross_repo_link(
            from_project_id=default_project.id,
            from_identifier="WI-LOCAL-C",
            to_project_slug="foreign-proj",
            to_identifier="WI-FOREIGN-Y",
        )

        blockers = cp.get_cross_project_blockers(default_project.id, "WI-LOCAL-C")
        assert len(blockers) == 1
        assert blockers[0]["to_project_slug"] == "foreign-proj"
        assert blockers[0]["to_identifier"] == "WI-FOREIGN-Y"


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


class TestCliCrossProjectP3:
    def test_cli_export_ops(self, default_project, capsys):
        from agent_notes.cli.work_items import cmd_wi_export_ops

        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-CLI-EXPORT",
            title="CLI export",
            embedding=_vec768(),
        )
        ns = argparse.Namespace(
            workspace=None,
            project=None,
            path="/projects/sf2",
            json=True,
        )
        assert cmd_wi_export_ops(ns) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["project"] == "sf2"
        assert len(data["ops"]) >= 1

    def test_cli_ingest_ops(self, default_project, capsys):
        from agent_notes.cli.work_items import cmd_wi_ingest_ops

        op = {
            "op_id": "cli-op-1",
            "entity_id": "cli-ent",
            "entity_type": "work_item",
            "op_type": "create",
            "lamport": 1,
            "payload": {
                "title": "CLI ingested",
                "project_id": 1,
                "identifier": "WI-CLI-INGEST",
                "status": "open",
            },
            "parent_op_ids": [],
        }
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps(op) + "\n")
            path = f.name

        ns = argparse.Namespace(
            json=True,
            source_project="cli-foreign",
            file=path,
        )
        assert cmd_wi_ingest_ops(ns) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["ingested"] == 1

    def test_cli_rebuild_cache(self, capsys):
        from agent_notes.cli.work_items import cmd_wi_rebuild_cache

        ns = argparse.Namespace(json=True)
        assert cmd_wi_rebuild_cache(ns) == 0
        data = json.loads(capsys.readouterr().out)
        assert "rebuilt" in data

    def test_cli_ingest_ops_stdin(self, default_project, capsys, monkeypatch):
        from agent_notes.cli.work_items import cmd_wi_ingest_ops

        op = {
            "op_id": "stdin-op-1",
            "entity_id": "stdin-ent",
            "entity_type": "work_item",
            "op_type": "create",
            "lamport": 1,
            "payload": {
                "title": "Stdin ingested",
                "project_id": 1,
                "identifier": "WI-STDIN",
                "status": "open",
            },
            "parent_op_ids": [],
        }
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(op) + "\n"))
        ns = argparse.Namespace(
            json=True,
            source_project="stdin-foreign",
            file=None,
        )
        assert cmd_wi_ingest_ops(ns) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["ingested"] == 1


# ---------------------------------------------------------------------------
# Round-trip: export → ingest → rebuild
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_export_then_ingest(self, default_project, target_project):
        # File a work item in the target project
        WorkItemModel.file_work_item(
            project_id=target_project.id,
            identifier="WI-ROUND-01",
            title="Round-trip item",
            body="Body text",
            status="open",
            embedding=_vec768(),
        )

        # Export target project's ops
        jsonl = cp.export_ops_jsonl(target_project.id)
        assert jsonl.strip()

        # Ingest them as if coming from "target-proj"
        count = cp.ingest_jsonl_ops(jsonl, "target-proj")
        assert count >= 1

        # Rebuild cache
        rebuilt = cp.rebuild_cross_project_cache()
        assert rebuilt >= 1

        # Verify the foreign work item is visible
        wi = cp.get_cross_project_work_item("target-proj", "WI-ROUND-01")
        assert wi is not None
        assert wi["title"] == "Round-trip item"
        assert wi["status"] == "open"

    def test_cross_project_link_blocks_via_cache(self, default_project, target_project):
        # Create local work item
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-LOCAL-BLOCK",
            title="Blocked by target",
            status="open",
            embedding=_vec768(),
        )

        # Create target work item
        WorkItemModel.file_work_item(
            project_id=target_project.id,
            identifier="WI-TARGET-BLOCKER",
            title="Target blocker",
            status="open",
            embedding=_vec768(),
        )

        # Export target ops and ingest
        jsonl = cp.export_ops_jsonl(target_project.id)
        cp.ingest_jsonl_ops(jsonl, target_project.slug)
        cp.rebuild_cross_project_cache()

        # Add cross-repo link
        cp.add_cross_repo_link(
            from_project_id=default_project.id,
            from_identifier="WI-LOCAL-BLOCK",
            to_project_slug=target_project.slug,
            to_identifier="WI-TARGET-BLOCKER",
        )

        # Local item should NOT be ready
        ready = WorkItemModel.ready_work_items(project_id=default_project.id)
        ids = {r["identifier"] for r in ready}
        assert "WI-LOCAL-BLOCK" not in ids

        # Close the target item via local close
        WorkItemModel.close_work_item(target_project.id, "WI-TARGET-BLOCKER")

        # Re-export and re-ingest
        jsonl = cp.export_ops_jsonl(target_project.id)
        cp.ingest_jsonl_ops(jsonl, target_project.slug)
        cp.rebuild_cross_project_cache()

        # Local item SHOULD now be ready
        ready = WorkItemModel.ready_work_items(project_id=default_project.id)
        ids = {r["identifier"] for r in ready}
        assert "WI-LOCAL-BLOCK" in ids


# ---------------------------------------------------------------------------
# WI-068 (B1): the cross-project verbs are lineage-gated authored writes —
# their ops must land with the RESOLVED actor, never actor_id=None.
# ---------------------------------------------------------------------------


class TestCrossProjectLineageGate:
    @staticmethod
    def _op_actor(entity_id: str, op_type: str) -> str | None:
        from psycopg.rows import dict_row

        from agent_notes.core.db import _conn

        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT actor_id FROM op_log WHERE entity_id = %s AND op_type = %s",
                (entity_id, op_type),
            )
            row = cur.fetchone()
        return row["actor_id"] if row else None

    def test_request_stamps_ambient_actor(self, default_project):
        """Cross-project requests use the canonical ambient actor."""
        req = WorkItemModel.request_work_item(
            project_id=default_project.id,
            target_project_slug="target",
            title="cross-project request",
        )
        assert self._op_actor(req["entity_id"], "request") == "agent:worker"

    def test_request_resolves_env_actor_when_none_passed(self, default_project):
        """No explicit identity: the op carries the env-RESOLVED actor, not
        NULL (the anonymous-write defect this closes)."""
        from agent_notes.core.actor import load_actor_config, resolve_actor

        req = WorkItemModel.request_work_item(
            project_id=default_project.id,
            target_project_slug="target",
            title="anonymous no more",
        )
        # Session conftest declares the lineage; the actor_id resolves to the
        # env-resolved default rather than committing NULL.
        expected = resolve_actor(load_actor_config()).actor_id
        stamped = self._op_actor(req["entity_id"], "request")
        assert stamped is not None
        assert stamped == expected

    def test_wait_stamps_resolved_actor(self, default_project):
        wait = WorkItemModel.wait_on_work_item(
            project_id=default_project.id,
            target_project_slug="target",
            target_identifier="WI-9",
        )
        assert self._op_actor(wait["entity_id"], "wait") == "agent:worker"

    def test_link_cross_stamps_resolved_actor(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-LINKSRC",
            title="link source",
            status="open",
            embedding=_vec768(),
        )
        result = WorkItemModel.add_cross_project_link(
            from_project_id=default_project.id,
            from_identifier="WI-LINKSRC",
            to_project_slug="foreign-project",
            to_identifier="WI-9",
        )
        # add_cross_project_link's return has no entity_id; find the add_link op.
        from psycopg.rows import dict_row

        from agent_notes.core.db import _conn

        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT actor_id, payload FROM op_log WHERE op_type = 'add_link' "
                "ORDER BY lamport DESC LIMIT 1",
            )
            row = cur.fetchone()
        assert row is not None
        assert row["payload"]["from_identifier"] == result["from_identifier"] == "WI-LINKSRC"
        assert row["actor_id"] == "agent:worker"

    def test_link_cross_change_log_row_carries_resolved_actor(
        self, default_project, target_project
    ):
        """WI-069: when the target project is local, the mirrored ``links``
        write's ``link_added`` change_log row is attributed to the
        lineage-gated actor — not NULL, even though the verb was already
        gated (WI-068 refused the write; this makes the audit row name who
        performed it)."""
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-LINKSRC-CL",
            title="link source (change_log attribution)",
            status="open",
            embedding=_vec768(),
        )
        WorkItemModel.add_cross_project_link(
            from_project_id=default_project.id,
            from_identifier="WI-LINKSRC-CL",
            to_project_slug="target",  # local project → the links mirror fires
            to_identifier="WI-T1",
        )

        from psycopg.rows import dict_row

        from agent_notes.core.db import _conn

        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT actor FROM change_log WHERE event = 'link_added' AND identifier = %s",
                ("WI-LINKSRC-CL",),
            )
            rows = cur.fetchall()
        assert rows, "the mirrored add_link must write a link_added change_log row"
        assert all(r["actor"] == "agent:worker" for r in rows)
