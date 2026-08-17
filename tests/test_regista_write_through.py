"""Tests for the regista write-through path behind AGENT_NOTES_REGISTA_WRITES."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from psycopg.rows import dict_row
from regista.testing import InMemoryRegista

from agent_notes.core import db as coredb
from agent_notes.core.db import _conn
from agent_notes.core.face_factory import reset_face, set_face_for_test
from agent_notes.core.regista_face import RegistaFace
from agent_notes.core.work_item._common import mirror_regista_snapshot
from agent_notes.core.work_item_model import WorkItemModel
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


@pytest.fixture
def default_project():
    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    return coredb.get_or_create_project(
        ws.id,
        slug="sf2",
        name="sf2",
        repo_root="/projects/sf2",
    )


@pytest.fixture
def hmac_key_path(tmp_path: Path):
    path = tmp_path / "keys.json"
    path.write_text(
        json.dumps(
            {
                "keys": [
                    {
                        "key_id": "test-key-001",
                        "secret": "dGhpcyBpcyBhIHRlc3Qgc2VjcmV0IGtleSBmb3Igc3Vic3RyYXRl",
                        "status": "active",
                    }
                ]
            }
        )
    )
    return str(path)


def _vec768():
    return [0.0] * 768


def _set_regista_env(dsn: str):
    os.environ["AGENT_NOTES_REGISTA_DSN"] = dsn
    os.environ["AGENT_NOTES_REGISTA_WRITES"] = "1"
    os.environ["AGENT_NOTES_REGISTA_PROJECT"] = "test_project"
    os.environ["AGENT_NOTES_REGISTA_HMAC_KEY_PATH"] = os.devnull
    os.environ["AGENT_NOTES_ACTOR_ID"] = "test-agent"
    # WI-062: agent-kind writes refuse without a declared lineage.
    os.environ["AGENT_NOTES_MODEL_LINEAGE"] = "glm"


def _clear_regista_env():
    for key in (
        "AGENT_NOTES_REGISTA_DSN",
        "AGENT_NOTES_REGISTA_WRITES",
        "AGENT_NOTES_REGISTA_PROJECT",
        "AGENT_NOTES_REGISTA_HMAC_KEY_PATH",
        "AGENT_NOTES_ACTOR_ID",
        # NOT AGENT_NOTES_MODEL_LINEAGE: conftest's hermetic fixture sets a
        # session-wide default (WI-062) and this helper pops from os.environ
        # directly, so removing it here would leak into every later test.
    ):
        os.environ.pop(key, None)


class TestRegistaWriteThrough:
    def test_file_amend_start_close_review_round_trip(
        self, default_project, hmac_key_path, monkeypatch
    ):
        # Plan 010 WI-3/WI-5: canonical lifecycle. file(open) → amend → start →
        # close(submit_for_review→in_review) → adversarial_pass → accept(done) →
        # reopen(open). The agent cannot reach `done` alone (Invariant G); it
        # requires the cross-lineage review gate.
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "test-agent")
        monkeypatch.setenv("AGENT_NOTES_PRINCIPAL_ID", "test@example.com")
        monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")

        reg = InMemoryRegista(hmac_key_path=hmac_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)

        try:
            wi = WorkItemModel.file_work_item(
                project_id=default_project.id,
                identifier="WI-REG-01",
                title="Original title",
                body="Original body",
                kind="bug",
                status="open",
                severity="high",
                embedding=_vec768(),
            )
            assert wi["identifier"] == "WI-REG-01"
            assert wi["status"] == "open"
            assert wi["regista_work_item_id"] is not None

            regista_id = wi["regista_work_item_id"]
            history = face.history(regista_id)
            assert len(history) >= 1
            listed = face.list(current_states=["open"])
            assert any(str(item.work_item_id) == str(regista_id) for item in listed)

            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute(
                    "SELECT * FROM work_items WHERE project_id = %s AND identifier = %s",
                    (default_project.id, "WI-REG-01"),
                )
                local = cur.fetchone()
            assert local is not None
            assert local["status"] == "open"
            assert local["title"] == "Original title"
            assert str(local["regista_work_item_id"]) == str(regista_id)

            # amend is a non-state event (lifecycle stays open).
            updated = WorkItemModel.update_work_item(
                project_id=default_project.id,
                identifier="WI-REG-01",
                title="Amended title",
            )
            assert updated["title"] == "Amended title"
            assert updated["status"] == "open"

            # close → submit_for_review → in_review (NOT done; agent can't reach
            # done alone). close from `open` starts work first, then submits.
            closed = WorkItemModel.close_work_item(default_project.id, "WI-REG-01")
            assert closed["status"] == "in_review"
            assert closed["closed_at"] is None  # in_review is not terminal

            # A different-lineage reviewer does the adversarial pass.
            from agent_notes.core.actor import Actor

            reviewer = Actor(
                actor_id="reviewer-kimi",
                actor_kind="agent",
                role="agent",
                model_lineage="kimi",
            )
            face.transition_breadcrumb(
                reviewer,
                regista_id,
                "adversarial_pass",
                payload={"review_note": "LGTM — cross-lineage pass"},
            )
            assert face.get(regista_id).current_state == "in_human_review"

            # Final accept (relaxed gate: any actor may accept after the pass,
            # but must differ from the adversarial-pass identity).
            accepter = Actor(
                actor_id="accepter-opus",
                actor_kind="agent",
                role="agent",
                model_lineage="claude-opus",
            )
            face.transition_breadcrumb(
                accepter,
                regista_id,
                "accept",
                payload={"review_note": "accepted"},
            )
            done = face.get(regista_id)
            assert done.current_state == "done"

            # The review-gate transitions were done directly on the face (by
            # reviewer/accepter actors, not the filing agent), so mirror the
            # final canonical state into the local projection.
            with _conn() as conn:
                mirror_regista_snapshot(
                    conn,
                    {
                        "project_id": default_project.id,
                        "identifier": "WI-REG-01",
                        "entity_id": wi["entity_id"],
                    },
                    done,
                )
                conn.commit()

            # The projection reflects `done` (terminal → closed_at set).
            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute(
                    "SELECT status, closed_at FROM work_items WHERE identifier = %s",
                    ("WI-REG-01",),
                )
                row = cur.fetchone()
            assert row["status"] == "done"
            assert row["closed_at"] is not None

            # reopen: done → open.
            reopened = WorkItemModel.update_work_item(
                project_id=default_project.id,
                identifier="WI-REG-01",
                status="open",
            )
            assert reopened["status"] == "open"
            assert reopened["closed_at"] is None

            listed_done = face.list(current_states=["done"])
            assert not any(str(item.work_item_id) == str(regista_id) for item in listed_done)
        finally:
            reset_face()
            reg.close()

    def test_agent_close_cannot_reach_done_alone(self, default_project, hmac_key_path, monkeypatch):
        # Plan 010 WI-5 / Invariant G: an agent's close leaves the item in
        # in_review, NOT done. Reaching done requires the review gate.
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "test-agent")
        monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")

        reg = InMemoryRegista(hmac_key_path=hmac_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)

        try:
            WorkItemModel.file_work_item(
                project_id=default_project.id,
                identifier="WI-REG-G",
                title="Invariant G test",
                status="open",
                embedding=_vec768(),
            )
            closed = WorkItemModel.close_work_item(default_project.id, "WI-REG-G")
            assert closed["status"] == "in_review"
            assert closed["status"] != "done"
        finally:
            reset_face()
            reg.close()

    def test_refile_with_stale_local_projection_does_not_duplicate(
        self, default_project, hmac_key_path, monkeypatch
    ):
        # Plan 015 regression: the duplication bug. A caller's create-vs-update
        # decision is made against the LOCAL projection; when that projection is
        # stale relative to the remote SoT (here: row deleted to simulate a fresh
        # session / reset local DB), a re-file used to mint a duplicate in regista.
        # The idempotency guard must find the existing item by normalized
        # source_identifier and amend it in place — leaving regista with ONE item.
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "test-agent")
        monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")

        reg = InMemoryRegista(hmac_key_path=hmac_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)

        try:
            first = WorkItemModel.file_work_item(
                project_id=default_project.id,
                identifier="050",
                title="poll_hooks dead-letter",
                status="open",
                embedding=_vec768(),
            )
            wid_first = first["regista_work_item_id"]
            assert len(face.list()) == 1

            # Simulate the stale projection: drop the local row so the next file
            # is routed as a "create" again (the exact bug condition).
            with _conn() as conn:
                conn.execute(
                    "DELETE FROM work_items WHERE project_id = %s AND identifier = %s",
                    (default_project.id, "050"),
                )
                conn.commit()

            # Re-file the SAME breadcrumb, now under the BC- identifier format.
            second = WorkItemModel.file_work_item(
                project_id=default_project.id,
                identifier="BC-050",
                title="poll_hooks dead-letter (updated)",
                status="open",
                embedding=_vec768(),
            )

            # Regista still holds exactly ONE work-item, and it is the original.
            all_items = face.list()
            assert len(all_items) == 1, f"expected 1 item, got {len(all_items)} (duplicate!)"
            assert str(second["regista_work_item_id"]) == str(wid_first)
            assert face.get(wid_first).custom_fields["title"] == "poll_hooks dead-letter (updated)"
        finally:
            reset_face()
            reg.close()

    def test_claim_release_uses_regista_claims_not_lifecycle(
        self, default_project, hmac_key_path, monkeypatch
    ):
        # Plan 010 WI-2/WI-5: claim/release are regista claims (a lease axis),
        # NOT lifecycle transitions. `claimed` is no longer a lifecycle state —
        # the status stays `open`; the lease is recorded in work_item_leases.
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "test-agent")
        monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")  # WI-062

        reg = InMemoryRegista(hmac_key_path=hmac_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)

        try:
            wi = WorkItemModel.file_work_item(
                project_id=default_project.id,
                identifier="WI-REG-02",
                title="Lease test",
                status="open",
                embedding=_vec768(),
            )
            entity_id = wi["entity_id"]

            claimed = WorkItemModel.claim_work_item(
                project_id=default_project.id,
                identifier="WI-REG-02",
                actor_id="legacy-actor",
                ttl_seconds=300,
            )
            # Lifecycle does NOT move to 'claimed' — it stays 'open'.
            assert claimed["status"] == "open"

            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute("SELECT * FROM work_item_leases WHERE entity_id = %s", (entity_id,))
                lease = cur.fetchone()
            assert lease is not None
            # The lease actor is the env-resolved agent actor, not the legacy param.
            assert lease["actor_id"] != "legacy-actor"

            released = WorkItemModel.release_work_item(
                project_id=default_project.id,
                identifier="WI-REG-02",
                actor_id="legacy-actor",
            )
            # Lifecycle still 'open' (release does not move state either).
            assert released["status"] == "open"

            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute("SELECT 1 FROM work_item_leases WHERE entity_id = %s", (entity_id,))
                assert cur.fetchone() is None
        finally:
            reset_face()
            reg.close()

    def test_heartbeat_uses_regista_claim(self, default_project, hmac_key_path, monkeypatch):
        # Plan 010 WI-2/WI-5: heartbeat extends the regista claim; the local
        # lease row is a projection mirror of the authoritative claim.
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "test-agent")
        monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")  # WI-062

        reg = InMemoryRegista(hmac_key_path=hmac_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)

        try:
            wi = WorkItemModel.file_work_item(
                project_id=default_project.id,
                identifier="WI-REG-03",
                title="Heartbeat test",
                status="open",
                embedding=_vec768(),
            )
            entity_id = wi["entity_id"]
            WorkItemModel.claim_work_item(default_project.id, "WI-REG-03")

            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute("SELECT * FROM work_item_leases WHERE entity_id = %s", (entity_id,))
                before = cur.fetchone()

            WorkItemModel.heartbeat_work_item(
                project_id=default_project.id,
                identifier="WI-REG-03",
                actor_id="legacy-actor",
                ttl_seconds=600,
            )

            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute("SELECT * FROM work_item_leases WHERE entity_id = %s", (entity_id,))
                after = cur.fetchone()
            assert after["expires_at"] > before["expires_at"]
            assert after["heartbeat_count"] == before["heartbeat_count"] + 1
        finally:
            reset_face()
            reg.close()


class TestClaimLineage:
    """WI-253: the three claim face methods must propagate actor_metadata
    (which carries model_lineage) onto their claim events, mirroring every
    other face method. Pre-fix the claim_acquired / claim_heartbeat /
    claim_released events drop actor_metadata — so model_lineage is None and
    the WI-248 live benefit is not realized in the CLI path.
    """

    def test_claim_heartbeat_release_propagate_model_lineage(
        self, default_project, hmac_key_path, monkeypatch
    ):
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "test-agent")
        monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "longcat")

        reg = InMemoryRegista(hmac_key_path=hmac_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)

        try:
            wi = WorkItemModel.file_work_item(
                project_id=default_project.id,
                identifier="WI-REG-LINEAGE",
                title="Lineage test",
                status="open",
                embedding=_vec768(),
            )
            regista_id = wi["regista_work_item_id"]

            # claim → claim_acquired event must carry model_lineage
            WorkItemModel.claim_work_item(default_project.id, "WI-REG-LINEAGE")
            events = face.history(regista_id)
            acquired = [e for e in events if e.transition == "claim_acquired"]
            assert len(acquired) == 1
            assert acquired[0].actor_metadata is not None
            assert acquired[0].actor_metadata.get("model_lineage") == "longcat"

            # heartbeat → claim_heartbeat event must carry model_lineage
            WorkItemModel.heartbeat_work_item(default_project.id, "WI-REG-LINEAGE")
            events = face.history(regista_id)
            heartbeat = [e for e in events if e.transition == "claim_heartbeat"]
            assert len(heartbeat) == 1
            assert heartbeat[0].actor_metadata is not None
            assert heartbeat[0].actor_metadata.get("model_lineage") == "longcat"

            # release → claim_released event must carry model_lineage
            WorkItemModel.release_work_item(default_project.id, "WI-REG-LINEAGE")
            events = face.history(regista_id)
            released = [e for e in events if e.transition == "claim_released"]
            assert len(released) == 1
            assert released[0].actor_metadata is not None
            assert released[0].actor_metadata.get("model_lineage") == "longcat"
        finally:
            reset_face()
            reg.close()

    def test_per_invocation_lineage_override_beats_env_on_regista_path(
        self, default_project, hmac_key_path, monkeypatch
    ):
        """WI-068: ``--model-lineage`` takes effect on the REGISTA lease path.

        The env path is pinned above; this pins the per-invocation override —
        with a lineage set in the env, an explicit ``model_lineage`` must win
        on claim / heartbeat / release, so the flag added in WI-068 is not a
        native-path-only courtesy. The claim *identity* stays the env-resolved
        actor (Plan 010 WI-2/WI-5) — only the declaration is overridable.
        """
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "test-agent")
        monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")

        reg = InMemoryRegista(hmac_key_path=hmac_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)

        try:
            wi = WorkItemModel.file_work_item(
                project_id=default_project.id,
                identifier="WI-REG-LIN-OVR",
                title="Lineage override test",
                status="open",
                embedding=_vec768(),
            )
            regista_id = wi["regista_work_item_id"]

            WorkItemModel.claim_work_item(
                default_project.id, "WI-REG-LIN-OVR", model_lineage="kimi"
            )
            WorkItemModel.heartbeat_work_item(
                default_project.id, "WI-REG-LIN-OVR", model_lineage="kimi"
            )
            WorkItemModel.release_work_item(
                default_project.id, "WI-REG-LIN-OVR", model_lineage="kimi"
            )

            events = face.history(regista_id)
            for transition in ("claim_acquired", "claim_heartbeat", "claim_released"):
                matching = [e for e in events if e.transition == transition]
                assert len(matching) == 1, transition
                assert matching[0].actor_metadata is not None
                assert matching[0].actor_metadata.get("model_lineage") == "kimi", transition
                # Identity is still the env-resolved actor, not re-declared.
                assert matching[0].actor_id == "test-agent"
        finally:
            reset_face()
            reg.close()


class TestMigrateToRegista:
    def test_migration_creates_regista_items_and_records_id(
        self, default_project, hmac_key_path, monkeypatch
    ):
        monkeypatch.delenv("AGENT_NOTES_REGISTA_WRITES", raising=False)
        monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")  # WI-062
        reset_face()

        legacy = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-MIG-01",
            title="Migrate me",
            body="Migration body",
            status="closed",
            embedding=_vec768(),
        )
        assert legacy.get("regista_work_item_id") is None

        monkeypatch.setenv("AGENT_NOTES_REGISTA_DSN", "postgresql://unused")
        monkeypatch.setenv("AGENT_NOTES_REGISTA_PROJECT", "test_project")
        monkeypatch.setenv("AGENT_NOTES_REGISTA_HMAC_KEY_PATH", hmac_key_path)
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "test-agent")
        monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")  # WI-062

        import regista as regista_module

        from agent_notes.scripts import migrate_to_regista

        reg = InMemoryRegista(project="test_project", hmac_key_path=hmac_key_path)

        class _PatchedRegista:
            def __init__(self, dsn, project, key_path, *, require_ssl=False):
                self._reg = reg

            def __getattr__(self, name):
                return getattr(self._reg, name)

        monkeypatch.setattr(regista_module, "Regista", _PatchedRegista)

        try:
            code = migrate_to_regista._run_migration("sf2", apply=True)
            assert code == 0

            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute(
                    "SELECT regista_work_item_id, status FROM work_items WHERE identifier = %s",
                    ("WI-MIG-01",),
                )
                row = cur.fetchone()
            assert row is not None
            assert row["regista_work_item_id"] is not None
            # Plan 010 WI-4: closed → done (canonical terminal) via close_from_open.
            assert row["status"] == "done"

            face = RegistaFace(reg)
            item = face.get(row["regista_work_item_id"])
            assert item is not None
            assert item.current_state == "done"
            assert item.custom_fields["title"] == "Migrate me"
        finally:
            reg.close()
            reset_face()
            _clear_regista_env()

    def test_dry_run_does_not_create_regista_items(
        self, default_project, hmac_key_path, monkeypatch
    ):
        monkeypatch.delenv("AGENT_NOTES_REGISTA_WRITES", raising=False)
        monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")  # WI-062
        reset_face()

        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-DRY-01",
            title="Dry run",
            status="open",
            embedding=_vec768(),
        )

        monkeypatch.setenv("AGENT_NOTES_REGISTA_DSN", "postgresql://unused")
        monkeypatch.setenv("AGENT_NOTES_REGISTA_HMAC_KEY_PATH", hmac_key_path)
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "test-agent")
        monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")  # WI-062

        from agent_notes.scripts import migrate_to_regista

        code = migrate_to_regista._run_migration("sf2", apply=False)
        assert code == 0

        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT regista_work_item_id FROM work_items WHERE identifier = %s",
                ("WI-DRY-01",),
            )
            assert cur.fetchone()["regista_work_item_id"] is None

        _clear_regista_env()
        reset_face()


class TestLegacyPathUnchanged:
    def test_legacy_path_still_writes_op_log(self, default_project, monkeypatch):
        _clear_regista_env()
        reset_face()
        monkeypatch.delenv("AGENT_NOTES_REGISTA_WRITES", raising=False)
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "test-agent")
        monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")  # WI-062

        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-LEG-01",
            title="Legacy item",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]

        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute("SELECT * FROM op_log WHERE entity_id = %s", (entity_id,))
            ops = cur.fetchall()
        assert len(ops) >= 1
        assert any(op["op_type"] == "create" for op in ops)

        _clear_regista_env()
        reset_face()
