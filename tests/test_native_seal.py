"""Sealing chainless or stale ``work_items`` cache rows (Plan 025 Phase 1).

Regista write-through mirrored rows into the ``work_items`` cache without
writing ops. The write-through is gone (Plan 026 D1), but those rows are still
in production, and ``core/work_item/_seal.py`` seals each one before the next
native mutation folds it. These tests build such rows with direct SQL (a cache
row with no op chain, or a chain the cache has moved past) and check that
sealing, and the native writes that seal lazily, reproduce the cache.

The one-shot bulk backfill that drove these seals at the Plan 025 cutover was
deleted in Plan 026; its dry-run, import and reporting cases went with it.
"""

from __future__ import annotations

import threading
import uuid

import psycopg
import pytest

from agent_notes.core import db as coredb
from agent_notes.core import kernel
from agent_notes.core.db import _conn
from agent_notes.core.verifier import verify_cache, verify_entity
from agent_notes.core.work_item import _seal
from agent_notes.core.work_item_model import WorkItemModel
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")

SEALER = "service:test"


def _vec(x: float = 0.25) -> list[float]:
    return [x] * 768


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


@pytest.fixture
def project():
    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    return coredb.get_or_create_project(
        ws.id, slug="p25-proj", name="p25-proj", repo_root="/projects/p25-proj"
    )


def _cache_row(project, identifier: str) -> dict:
    with _conn() as conn:
        return dict(
            conn.execute(
                "SELECT * FROM work_items WHERE project_id = %s AND identifier = %s",
                (project.id, identifier),
            ).fetchone()
        )


def _write_cache_row(
    project,
    identifier: str,
    entity_id: str,
    *,
    status: str,
    title: str,
    kind: str = "todo",
    body: str = "",
    severity: str = "medium",
    external_refs: dict | None = None,
    diagnostic_keys: dict | None = None,
    embedding: list[float] | None = None,
    regista_work_item_id: str | None = None,
) -> dict:
    """Upsert a cache row directly, writing no ops (as the regista path did)."""
    with _conn() as conn:
        body_hash = kernel.store_blob(conn, body)
        conn.execute(
            """
            INSERT INTO work_items
                (entity_id, project_id, identifier, title, body_hash, kind, status,
                 severity, external_refs, diagnostic_keys, embedding,
                 frontmatter_version, regista_work_item_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s)
            ON CONFLICT (project_id, identifier) DO UPDATE SET
                title = EXCLUDED.title,
                body_hash = EXCLUDED.body_hash,
                kind = EXCLUDED.kind,
                status = EXCLUDED.status,
                severity = EXCLUDED.severity,
                external_refs = EXCLUDED.external_refs,
                diagnostic_keys = EXCLUDED.diagnostic_keys,
                embedding = COALESCE(EXCLUDED.embedding, work_items.embedding),
                regista_work_item_id = EXCLUDED.regista_work_item_id,
                updated_at = now()
            """,
            (
                entity_id,
                project.id,
                identifier,
                title,
                body_hash,
                kind,
                status,
                severity,
                psycopg.types.json.Jsonb(external_refs or {}),
                psycopg.types.json.Jsonb(diagnostic_keys or {}),
                _vec_literal(embedding) if embedding is not None else None,
                regista_work_item_id or str(uuid.uuid4()),
            ),
        )
        conn.commit()
    return _cache_row(project, identifier)


def _chainless_row(project, identifier: str, *, status: str = "in_progress") -> dict:
    """A regista-era row: cache only, no ops, entity_id derived the regista way."""
    wid = str(uuid.uuid4())
    entity_id = kernel._make_op_id(
        "work_item", "create", {"identifier": identifier, "regista_work_item_id": wid}, []
    )
    return _write_cache_row(
        project,
        identifier,
        entity_id,
        status=status,
        title=f"title {identifier}",
        kind="bug",
        body=f"body {identifier}",
        severity="high",
        external_refs={"pr": "1"},
        diagnostic_keys={"k": "v"},
        embedding=_vec(),
        regista_work_item_id=wid,
    )


def _seal_row(row: dict) -> list[str]:
    with _conn() as conn:
        locked = _seal.lock_row(conn, row["project_id"], row["identifier"])
        diffs = _seal.seal(conn, locked, SEALER)
        conn.commit()
    return diffs


def _op_count(entity_id: str) -> int:
    with _conn() as conn:
        return conn.execute(
            "SELECT count(*) AS n FROM op_log WHERE entity_id = %s", (entity_id,)
        ).fetchone()["n"]


class TestSeal:
    def test_chainless_row_drifts(self, project):
        row = _chainless_row(project, "P25-001")
        with _conn() as conn:
            _folded, diffs = _seal.drift(conn, row)
        assert diffs == ["op_chain"]
        assert _op_count(row["entity_id"]) == 0

    def test_seal_reproduces_the_row_and_is_idempotent(self, project):
        row = _chainless_row(project, "P25-002", status="in_review")
        assert _seal_row(row) == ["op_chain"]

        with _conn() as conn:
            _folded, diffs = _seal.drift(conn, row)
            ops = kernel._get_entity_ops(conn, row["entity_id"])
        assert diffs == []
        # The create stub's op_id is the regista-era entity_id, so later
        # parent_op_ids=[entity_id] references resolve.
        assert [o["op_type"] for o in ops] == ["create", "snapshot"]
        assert ops[0]["op_id"] == row["entity_id"]
        assert verify_cache(entity_id=row["entity_id"]).ok()

        count = _op_count(row["entity_id"])
        assert _seal_row(row) == []
        assert _op_count(row["entity_id"]) == count

    def test_stale_chain_is_resealed(self, project):
        # Filed natively, then the cache moved on without ops (regista era).
        native = WorkItemModel.file_work_item(
            project_id=project.id,
            identifier="P25-003",
            title="native first",
            status="open",
            embedding=_vec(0.5),
        )
        row = _write_cache_row(
            project,
            "P25-003",
            native["entity_id"],
            status="done",
            title="renamed in regista",
        )
        with _conn() as conn:
            _folded, diffs = _seal.drift(conn, row)
        assert "status" in diffs and "title" in diffs

        _seal_row(row)
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


class TestSealEdgeShapes:
    """Shapes found in production by the first bulk seal."""

    def test_tombstoned_entity_with_a_resurrected_cache_row_seals(self, project):
        native = WorkItemModel.file_work_item(
            project_id=project.id,
            identifier="P25-020",
            title="deleted natively",
            status="open",
            embedding=_vec(),
        )
        assert WorkItemModel.delete_work_item(project.id, "P25-020")
        # Regista re-mirrored the natively deleted item: the cache row returns.
        row = _write_cache_row(
            project,
            "P25-020",
            native["entity_id"],
            status="done",
            title="back from regista",
            kind="bug",
            embedding=_vec(),
        )
        with _conn() as conn:
            # Native close after the delete, as on adcs-lens WI-029.
            kernel.commit_op(
                conn,
                entity_id=row["entity_id"],
                entity_type="work_item",
                op_type="close",
                payload={"reason": "manual_close"},
                parent_op_ids=[row["entity_id"]],
                actor_id="human:test",
            )
            conn.commit()
            assert kernel.fold_work_item_state(conn, row["entity_id"]) is None

        _seal_row(row)

        with _conn() as conn:
            _folded, diffs = _seal.drift(conn, row)
        assert diffs == []
        with _conn() as conn:
            kernel.fold_all_work_items(conn)
            conn.commit()
        assert _cache_row(project, "P25-020")["status"] == "done"
        assert verify_entity(row["entity_id"]).ok()
        assert verify_cache(entity_id=row["entity_id"]).ok()

    def test_entity_id_that_is_not_an_op_id_seals_without_dangling_parents(self, project):
        entity_id = "ab" * 32  # predates the current derivation: no op has this id
        row = _write_cache_row(
            project,
            "P25-022",
            entity_id,
            status="in_progress",
            title="legacy id",
            embedding=_vec(),
        )

        _seal_row(row)
        with _conn() as conn:
            ops = kernel._get_entity_ops(conn, entity_id)
            drift = _seal.drift(conn, row)[1]
        assert drift == []
        assert [o["op_type"] for o in ops] == ["create", "snapshot"]
        assert ops[1]["parent_op_ids"] == [ops[0]["op_id"]]
        assert ops[0]["op_id"] != entity_id
        assert verify_entity(entity_id).ok()

        # Later native ops chain onto entity_id; the verifier accepts that anchor.
        WorkItemModel.update_work_item(project.id, "P25-022", severity="low")
        assert verify_entity(entity_id).ok()


class TestLazySealOnNativeWrites:
    def test_update_on_chainless_row_keeps_cached_state(self, project):
        _chainless_row(project, "P25-010", status="in_progress")
        out = WorkItemModel.update_work_item(project.id, "P25-010", severity="low")
        assert out["severity"] == "low"
        assert out["status"] == "in_progress"
        assert out["title"] == "title P25-010"
        assert out["external_refs"] == {"pr": "1"}
        row = _cache_row(project, "P25-010")
        assert row["embedding"] is not None
        assert row["diagnostic_keys"] == {"k": "v"}
        assert verify_cache(entity_id=row["entity_id"]).ok()

    def test_close_on_chainless_row_reaches_done(self, project):
        _chainless_row(project, "P25-011", status="in_progress")
        assert WorkItemModel.close_work_item(project.id, "P25-011")["status"] == "done"

    def test_claim_close_on_chainless_open_row(self, project):
        _chainless_row(project, "P25-012", status="open")
        assert WorkItemModel.claim_work_item(project.id, "P25-012")["status"] == "claimed"
        assert WorkItemModel.close_work_item(project.id, "P25-012")["status"] == "done"


class TestSealRaces:
    """A seal must never write a cache row that another writer has superseded.

    The test lets writer B commit a title change after the sealing path has
    started. Without a row lock the sealer folds B's op, compares it with the
    row it read before B committed, and seals the stale title over B's edit.
    """

    @pytest.fixture
    def race_project(self):
        ws = coredb.get_or_create_workspace("default", "Default Workspace")
        return coredb.get_or_create_project(
            ws.id, slug="p25-race", name="p25-race", repo_root="/projects/p25-race"
        )

    @staticmethod
    def _rename_in_thread(project, identifier):
        errors: list[BaseException] = []

        def run():
            try:
                WorkItemModel.update_work_item(project.id, identifier, title="renamed by B")
            except BaseException as exc:  # surfaced by the test
                errors.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        return thread, errors

    def test_concurrent_native_write_is_not_reverted(self, race_project, monkeypatch):
        WorkItemModel.file_work_item(
            project_id=race_project.id,
            identifier="RACE-001",
            title="original",
            status="open",
            embedding=_vec(0.5),
        )
        real_drift = _seal.drift
        state: dict = {}

        def drift(conn, row):
            # Run writer B (bounded wait) the first time the sealer checks drift.
            if row["identifier"] == "RACE-001" and "thread" not in state:
                thread, errors = self._rename_in_thread(race_project, "RACE-001")
                state["thread"], state["errors"] = thread, errors
                # A locked row makes B wait for us; give up after a bound.
                thread.join(timeout=2.0)
            return real_drift(conn, row)

        monkeypatch.setattr(_seal, "drift", drift)

        WorkItemModel.update_work_item(race_project.id, "RACE-001", severity="low")
        state["thread"].join(timeout=30)
        assert not state["thread"].is_alive()
        assert state["errors"] == []

        row = _cache_row(race_project, "RACE-001")
        assert row["title"] == "renamed by B"
        assert row["severity"] == "low"
        with _conn() as conn:
            kernel.fold_all_work_items(conn)
            conn.commit()
        assert _cache_row(race_project, "RACE-001")["title"] == "renamed by B"
