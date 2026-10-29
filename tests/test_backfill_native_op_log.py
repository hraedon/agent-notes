"""Plan 025 Phase 1: sealing regista-era work_items rows into the op-log.

Regista write-through mirrored rows into the ``work_items`` cache without
writing ops. These tests build such rows with the same projection call the
regista path used, then check that the backfill (and the lazy seal on the
native write path) make the op-log fold reproduce the cache.
"""

from __future__ import annotations

import json
import uuid

import psycopg
import pytest

from agent_notes.core import db as coredb
from agent_notes.core import kernel, projection
from agent_notes.core.db import _conn
from agent_notes.core.verifier import verify_cache
from agent_notes.core.work_item import _common, _seal
from agent_notes.core.work_item_model import WorkItemModel
from agent_notes.scripts import backfill_native_op_log as backfill
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


def _vec(x: float = 0.25) -> list[float]:
    return [x] * 768


@pytest.fixture
def project():
    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    return coredb.get_or_create_project(
        ws.id, slug="p25-proj", name="p25-proj", repo_root="/projects/p25-proj"
    )


def _regista_era_row(project, identifier: str, *, state: str = "in_progress") -> dict:
    """A row exactly as ``_regista.file_work_item`` left it: cache only, no ops."""
    wid = uuid.uuid4()
    entity_id = _common.entity_id_for_regista_create(identifier, wid)
    with _conn() as conn:
        row = projection.mirror_from_regista(
            conn,
            project_id=project.id,
            identifier=identifier,
            entity_id=entity_id,
            regista_work_item_id=wid,
            state=state,
            custom_fields={
                "title": f"title {identifier}",
                "description": f"body {identifier}",
                "severity": "high",
                "kind": "bug",
                "external_refs": {"pr": "1"},
                "diagnostic_keys": {"k": "v"},
            },
            embed=lambda _t: _vec(),
        )
        conn.commit()
    return _cache_row(project, row["identifier"])


def _op_count(entity_id: str | None = None) -> int:
    with _conn() as conn:
        if entity_id is None:
            return conn.execute("SELECT count(*) AS n FROM op_log").fetchone()["n"]
        return conn.execute(
            "SELECT count(*) AS n FROM op_log WHERE entity_id = %s", (entity_id,)
        ).fetchone()["n"]


def _cache_row(project, identifier: str) -> dict:
    with _conn() as conn:
        return dict(
            conn.execute(
                "SELECT * FROM work_items WHERE project_id = %s AND identifier = %s",
                (project.id, identifier),
            ).fetchone()
        )


class TestSeal:
    def test_chainless_row_drifts_and_dry_run_writes_nothing(self, project):
        row = _regista_era_row(project, "P25-001")
        with _conn() as conn:
            _folded, diffs = _seal.drift(conn, row)
        assert diffs == ["op_chain"]

        before = _op_count()
        report = backfill.seal_pass(project="p25-proj", apply=False, actor_id="service:test")
        assert report["per_project"]["p25-proj"]["no_op_chain"] >= 1
        assert "sealed" not in report["per_project"]["p25-proj"]
        assert _op_count() == before

    def test_apply_seals_and_is_idempotent(self, project):
        row = _regista_era_row(project, "P25-002", state="in_review")
        report = backfill.seal_pass(project="p25-proj", apply=True, actor_id="service:test")
        assert report["failures"] == []
        assert report["per_project"]["p25-proj"]["sealed"] >= 1

        with _conn() as conn:
            _folded, diffs = _seal.drift(conn, row)
            ops = kernel._get_entity_ops(conn, row["entity_id"])
        assert diffs == []
        # The create stub's op_id is the regista-era entity_id, so later
        # parent_op_ids=[entity_id] references resolve.
        assert [o["op_type"] for o in ops] == ["create", "snapshot"]
        assert ops[0]["op_id"] == row["entity_id"]
        assert verify_cache(entity_id=row["entity_id"]).ok()

        count = _op_count()
        again = backfill.seal_pass(project="p25-proj", apply=True, actor_id="service:test")
        assert again["per_project"]["p25-proj"].get("sealed", 0) == 0
        assert _op_count() == count

    def test_stale_chain_is_resealed(self, project):
        # Filed natively, then moved to regista and transitioned there.
        native = WorkItemModel.file_work_item(
            project_id=project.id,
            identifier="P25-003",
            title="native first",
            status="open",
            embedding=_vec(0.5),
        )
        with _conn() as conn:
            projection.mirror_from_regista(
                conn,
                project_id=project.id,
                identifier="P25-003",
                entity_id=native["entity_id"],
                regista_work_item_id=uuid.uuid4(),
                state="done",
                custom_fields={"title": "renamed in regista", "kind": "todo"},
            )
            conn.commit()
        row = _cache_row(project, "P25-003")
        with _conn() as conn:
            _folded, diffs = _seal.drift(conn, row)
        assert "status" in diffs and "title" in diffs

        backfill.seal_pass(project="p25-proj", apply=True, actor_id="service:test")
        with _conn() as conn:
            _folded, diffs = _seal.drift(conn, row)
        assert diffs == []
        # A later fold (here: a full rebuild) no longer reverts the row.
        with _conn() as conn:
            kernel.fold_all_work_items(conn)
            conn.commit()
        rebuilt = _cache_row(project, "P25-003")
        assert rebuilt["status"] == "done"
        assert rebuilt["title"] == "renamed in regista"


class TestLazySealOnNativeWrites:
    def test_update_on_chainless_row_keeps_cached_state(self, project):
        _regista_era_row(project, "P25-010", state="in_progress")
        out = WorkItemModel.update_work_item(project.id, "P25-010", severity="low")
        assert out["severity"] == "low"
        assert out["status"] == "in_progress"
        assert out["title"] == "title P25-010"
        assert out["external_refs"] == {"pr": "1"}
        row = _cache_row(project, "P25-010")
        assert row["embedding"] is not None
        assert verify_cache(entity_id=row["entity_id"]).ok()

    def test_close_on_chainless_row_reaches_done(self, project):
        _regista_era_row(project, "P25-011", state="in_progress")
        assert WorkItemModel.close_work_item(project.id, "P25-011")["status"] == "done"

    def test_claim_close_on_chainless_open_row(self, project):
        _regista_era_row(project, "P25-012", state="open")
        assert WorkItemModel.claim_work_item(project.id, "P25-012")["status"] == "claimed"
        assert WorkItemModel.close_work_item(project.id, "P25-012")["status"] == "done"


class TestImportUnmirrored:
    @pytest.fixture
    def regista_schema(self, ephemeral_db, project):  # noqa: F811
        """A minimal stand-in for a regista project schema, in the same DB."""
        with psycopg.connect(ephemeral_db, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS p25_proj CASCADE")
            conn.execute("CREATE SCHEMA p25_proj")
            conn.execute(
                """
                CREATE TABLE p25_proj.work_items_current (
                    work_item_id uuid PRIMARY KEY,
                    workflow_name text,
                    work_item_type text,
                    current_state text,
                    custom_fields jsonb,
                    last_event_at timestamptz DEFAULT now()
                )
                """
            )
        yield ephemeral_db
        with psycopg.connect(ephemeral_db, autocommit=True) as conn:
            conn.execute("DROP SCHEMA p25_proj CASCADE")

    @staticmethod
    def _add(dsn, sid, *, state="open", workflow="canonical", wtype="breadcrumb"):
        wid = uuid.uuid4()
        fields = {"source_identifier": sid, "title": f"t {sid}", "description": "d"}
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO p25_proj.work_items_current "
                "(work_item_id, workflow_name, work_item_type, current_state, custom_fields) "
                "VALUES (%s, %s, %s, %s, %s)",
                (wid, workflow, wtype, state, json.dumps(fields)),
            )
        return str(wid)

    def test_import_dry_run_then_apply(self, project, regista_schema):
        _regista_era_row(project, "P25-020")  # occupies the identifier locally
        new_wid = self._add(regista_schema, "P25-021", state="in_progress")
        self._add(regista_schema, "P25-020")  # collides with the local row
        self._add(regista_schema, "P25-022")
        self._add(regista_schema, None)
        self._add(regista_schema, "TOOL-1", workflow="cairn_agent_actions", wtype="tool_call")

        kwargs = dict(project="p25-proj", actor_id="service:test", regista_dsn=regista_schema)
        dry = backfill.import_pass(apply=False, skip={"P25-022"}, **kwargs)
        assert dry["summary"] == {
            "import": 1,
            "skip_identifier_in_use": 1,
            "skip_requested": 1,
            "skip_no_source_identifier": 1,
        }
        assert WorkItemModel.get_work_item(project.id, "P25-021") is None

        applied = backfill.import_pass(apply=True, skip={"P25-022"}, **kwargs)
        assert applied["summary"]["imported"] == 1
        wi = _cache_row(project, "P25-021")
        assert wi["status"] == "in_progress"
        assert str(wi["regista_work_item_id"]) == new_wid
        assert verify_cache(entity_id=wi["entity_id"]).ok()

        rerun = backfill.import_pass(apply=True, skip={"P25-022"}, **kwargs)
        assert "imported" not in rerun["summary"]
