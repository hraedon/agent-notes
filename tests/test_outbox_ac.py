"""Acceptance tests for the outbox + reconcile (Plan 009 §6.6).

AC-1: write never surfaces failure — unreachable → enqueue, return success.
AC-2: hand-edited line is rejected loudly — no regista write, sidecar exists.
AC-3: concurrent change blocks — conflict detected, op preserved, terminal
      transitions gated while outbox is non-empty.

Uses InMemoryRegista (no Postgres needed for the load-bearing AC tests).
One optional postgres-marked e2e test exercises a real Regista project.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from regista import canonical_workflow_yaml
from regista.testing import make_v6_keyset, open_v6_epoch

from agent_notes.core.actor import Actor
from agent_notes.core.envelope import LocalKeySigner, verify_envelope
from agent_notes.core.outbox import (
    OutboxAwareFace,
    OutboxPendingError,
    count_ops,
    enqueue,
    outbox_dir,
    outbox_path,
    read_all,
)
from agent_notes.core.reconcile import reconcile
from tests.conftest import provision_v6_regista, shape_valid_delegation

_PROJECT = "ac_test"


@pytest.fixture
def outbox_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "outbox"
    monkeypatch.setenv("AGENT_NOTES_OUTBOX_DIR", str(d))
    monkeypatch.setenv("AGENT_NOTES_SESSION", uuid.uuid4().hex)
    return d


@pytest.fixture
def signer(tmp_path: Path) -> LocalKeySigner:
    return LocalKeySigner(str(tmp_path / "signing.key"))


@pytest.fixture
def actor() -> Actor:
    return Actor(actor_id="agent:ac-test-agent", actor_kind="agent", display_name="AC Test")


@pytest.fixture
def face(tmp_path: Path) -> "RegistaFace":  # type: ignore[name-defined]  # noqa: F821
    from agent_notes.core.regista_face import RegistaFace

    return RegistaFace(provision_v6_regista(tmp_path / "v6_keys.json", project=_PROJECT))


def _actor_dict(actor: Actor) -> dict:
    return {
        "actor_id": actor.actor_id,
        "actor_kind": actor.actor_kind,
        "display_name": actor.display_name,
        "action_delegation_credentials": list(actor.action_delegation_credentials),
        "role": actor.role,
    }


def _make_create_op(actor: Actor, title: str = "AC test") -> dict:
    return {
        "op": "create",
        "work_item_id": None,
        "args": {
            "actor": _actor_dict(actor),
            "title": title,
            "description": "",
            "severity": "medium",
            "kind": "todo",
            "external_refs": None,
            "diagnostic_keys": None,
            "source_identifier": None,
        },
        "expected_state": None,
    }


def _make_transition_op(
    actor: Actor, wid, transition_name: str, expected_state: str | None = None
) -> dict:
    return {
        "op": "transition",
        "work_item_id": str(wid),
        "args": {
            "actor": _actor_dict(actor),
            "transition_name": transition_name,
            "payload": None,
            "custom_fields": None,
            "expected_event_seq": None,
        },
        "expected_state": expected_state,
    }


class TestAC1UnreachableEnqueues:
    """AC-1: the agent never sees 'regista unreachable'."""

    def test_create_returns_success_and_enqueues(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
    ) -> None:
        outface = OutboxAwareFace(
            face, project=_PROJECT, signer=signer, unreachable_probe=lambda: True
        )
        wid, state = outface.create_breadcrumb(actor, title="Offline create")

        assert state == "open"
        assert wid is None
        assert outface.last_op_outboxed is True

        entries = read_all(_PROJECT)
        assert len(entries) == 1
        assert outface.pending_count() == 1

        payload = verify_envelope(entries[0].envelope, signer.public_key())
        assert payload["op"] == "create"
        assert payload["args"]["title"] == "Offline create"
        assert payload["client_seq"] == 1

    def test_amend_returns_current_state_and_enqueues(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
    ) -> None:
        wid, _ = face.create_breadcrumb(actor, title="Live create")
        outface = OutboxAwareFace(
            face, project=_PROJECT, signer=signer, unreachable_probe=lambda: True
        )
        state = outface.amend_breadcrumb(actor, wid, current_state="open", title="Updated")

        assert state == "open"
        assert outface.last_op_outboxed is True
        assert outface.pending_count() == 1

    def test_comment_enqueues(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
    ) -> None:
        wid, _ = face.create_breadcrumb(actor, title="Live create")
        outface = OutboxAwareFace(
            face, project=_PROJECT, signer=signer, unreachable_probe=lambda: True
        )
        outface.comment(actor, wid, "offline comment")

        assert outface.last_op_outboxed is True
        assert outface.pending_count() == 1

    def test_live_write_sets_outboxed_false(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
    ) -> None:
        outface = OutboxAwareFace(
            face, project=_PROJECT, signer=signer, unreachable_probe=lambda: False
        )
        wid, state = outface.create_breadcrumb(actor, title="Live create")

        assert wid is not None
        assert state == "open"
        assert outface.last_op_outboxed is False
        assert outface.pending_count() == 0


class TestAC2HandEditRejected:
    """AC-2: a hand-edited or unsigned line is rejected loudly."""

    def test_flipped_payload_byte_rejected(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
    ) -> None:
        enqueue(_PROJECT, _make_create_op(actor, title="Original"), signer)

        session = os.environ["AGENT_NOTES_SESSION"]
        path = outbox_path(_PROJECT, session)
        content = json.loads(path.read_text().strip())
        payload = content["payload"]
        flipped = payload[:10] + ("A" if payload[10] != "A" else "B") + payload[11:]
        content["payload"] = flipped
        path.write_text(json.dumps(content) + "\n")

        report = reconcile(_PROJECT, face=face, signer=signer)

        assert report.replayed == 0
        assert report.rejected == 1
        assert len(face.list()) == 0

        sidecar = path.parent / f"{session}.rejected.jsonl"
        assert sidecar.exists()
        sidecar_content = json.loads(sidecar.read_text().strip())
        assert sidecar_content["client_seq"] == 1

    def test_empty_signatures_rejected(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
    ) -> None:
        enqueue(_PROJECT, _make_create_op(actor, title="NoSig"), signer)

        session = os.environ["AGENT_NOTES_SESSION"]
        path = outbox_path(_PROJECT, session)
        content = json.loads(path.read_text().strip())
        content["signatures"] = []
        path.write_text(json.dumps(content) + "\n")

        report = reconcile(_PROJECT, face=face, signer=signer)

        assert report.replayed == 0
        assert report.rejected == 1
        assert len(face.list()) == 0

    def test_wrong_key_rejected(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
        tmp_path: Path,
    ) -> None:
        other_signer = LocalKeySigner(str(tmp_path / "other.key"))
        enqueue(_PROJECT, _make_create_op(actor, title="WrongKey"), other_signer)

        report = reconcile(_PROJECT, face=face, signer=signer)

        assert report.replayed == 0
        assert report.rejected == 1
        assert len(face.list()) == 0

    def test_report_bool_false_when_all_rejected(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
    ) -> None:
        enqueue(_PROJECT, _make_create_op(actor), signer)

        session = os.environ["AGENT_NOTES_SESSION"]
        path = outbox_path(_PROJECT, session)
        content = json.loads(path.read_text().strip())
        content["signatures"] = []
        path.write_text(json.dumps(content) + "\n")

        report = reconcile(_PROJECT, face=face, signer=signer)
        assert not report
        assert report.rejected == 1


class TestAC3ConcurrentChangeConflict:
    """AC-3: a concurrent change blocks the offline op."""

    def test_state_mismatch_blocks_transition(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
    ) -> None:
        wid, _ = face.create_breadcrumb(actor, title="Conflict item")

        enqueue(
            _PROJECT,
            _make_transition_op(actor, wid, "close_from_open", expected_state="open"),
            signer,
        )

        face.transition_breadcrumb(actor, wid, "close_from_open")
        assert face.get(wid).current_state == "done"

        report = reconcile(_PROJECT, face=face, signer=signer)

        assert report.replayed == 0
        assert report.conflicts == 1
        assert len(report.conflict_details) == 1
        detail = report.conflict_details[0]
        assert detail["expected_state"] == "open"
        assert detail["actual_state"] == "done"

        assert count_ops(_PROJECT) == 1

        session = os.environ["AGENT_NOTES_SESSION"]
        sidecar = outbox_dir() / _PROJECT / f"{session}.conflicts.jsonl"
        assert sidecar.exists()

    def test_transition_error_treated_as_conflict(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
    ) -> None:
        wid, _ = face.create_breadcrumb(actor, title="Item")
        face.transition_breadcrumb(actor, wid, "close_from_open")

        enqueue(
            _PROJECT,
            _make_transition_op(actor, wid, "close_from_open", expected_state=None),
            signer,
        )

        report = reconcile(_PROJECT, face=face, signer=signer)

        assert report.replayed == 0
        assert report.conflicts == 1
        assert count_ops(_PROJECT) == 1

    def test_terminal_transition_blocked_with_pending(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
    ) -> None:
        outface = OutboxAwareFace(
            face, project=_PROJECT, signer=signer, unreachable_probe=lambda: True
        )
        outface.create_breadcrumb(actor, title="Pending offline op")
        assert outface.pending_count() == 1

        wid, _ = face.create_breadcrumb(actor, title="Live item")

        with pytest.raises(OutboxPendingError, match="pending sync"):
            outface.transition_breadcrumb(actor, wid, "close_from_open")

    def test_reopen_blocked_with_pending(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
    ) -> None:
        outface = OutboxAwareFace(
            face, project=_PROJECT, signer=signer, unreachable_probe=lambda: True
        )
        outface.create_breadcrumb(actor, title="Pending offline op")

        wid, _ = face.create_breadcrumb(actor, title="Live item")
        face.transition_breadcrumb(actor, wid, "close_from_open")

        with pytest.raises(OutboxPendingError, match="pending sync"):
            outface.transition_breadcrumb(actor, wid, "reopen")

    def test_nonterminal_transition_allowed_with_pending(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
    ) -> None:
        outface = OutboxAwareFace(
            face, project=_PROJECT, signer=signer, unreachable_probe=lambda: True
        )
        outface.create_breadcrumb(actor, title="Pending offline op")

        wid, _ = face.create_breadcrumb(actor, title="Live item")
        outface.transition_breadcrumb(actor, wid, "start")

        assert outface.last_op_outboxed is True
        assert outface.pending_count() == 2


class TestPositiveReconcile:
    """Enqueue offline, reconcile with a reachable face → item created."""

    def test_create_replayed_and_removed(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
    ) -> None:
        enqueue(_PROJECT, _make_create_op(actor, title="Replayed"), signer)
        assert count_ops(_PROJECT) == 1

        report = reconcile(_PROJECT, face=face, signer=signer)

        assert report.replayed == 1
        assert report.rejected == 0
        assert report.conflicts == 0
        assert bool(report) is True

        assert count_ops(_PROJECT) == 0

        items = face.list()
        assert len(items) == 1
        assert items[0].custom_fields["title"] == "Replayed"

    def test_amend_replayed(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
    ) -> None:
        wid, _ = face.create_breadcrumb(actor, title="Original")

        op = {
            "op": "amend",
            "work_item_id": str(wid),
            "args": {
                "actor": _actor_dict(actor),
                "current_state": "open",
                "title": "Amended",
                "description": None,
                "severity": None,
                "kind": None,
                "external_refs": None,
                "diagnostic_keys": None,
                "payload": None,
            },
            "expected_state": "open",
        }
        enqueue(_PROJECT, op, signer)

        report = reconcile(_PROJECT, face=face, signer=signer)

        assert report.replayed == 1
        assert count_ops(_PROJECT) == 0

        wi = face.get(wid)
        assert wi.custom_fields["title"] == "Amended"

    def test_comment_replayed(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
    ) -> None:
        wid, _ = face.create_breadcrumb(actor, title="Comment target")

        op = {
            "op": "comment",
            "work_item_id": str(wid),
            "args": {
                "actor": _actor_dict(actor),
                "body": "offline comment",
            },
            "expected_state": None,
        }
        enqueue(_PROJECT, op, signer)

        report = reconcile(_PROJECT, face=face, signer=signer)

        assert report.replayed == 1
        assert count_ops(_PROJECT) == 0

        events = face.history(wid)
        bodies = [e.payload.get("body") for e in events if e.transition == "comment"]
        assert "offline comment" in bodies

    def test_multiple_ops_replayed_in_order(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
    ) -> None:
        enqueue(_PROJECT, _make_create_op(actor, title="Multi"), signer)

        report = reconcile(_PROJECT, face=face, signer=signer)

        assert report.replayed == 1
        items = face.list()
        assert len(items) == 1

        wid = items[0].work_item_id

        enqueue(
            _PROJECT,
            _make_transition_op(actor, wid, "close_from_open", expected_state="open"),
            signer,
        )

        report2 = reconcile(_PROJECT, face=face, signer=signer)
        assert report2.replayed == 1
        assert face.get(wid).current_state == "done"
        assert count_ops(_PROJECT) == 0

    def test_transition_replay_preserves_event_id_and_producer(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        face,
    ) -> None:
        reviewer = Actor(
            actor_id="agent:reviewer",
            actor_kind="agent",
            display_name="Reviewer",
        )
        wid, _ = face.create_breadcrumb(reviewer, title="Transition target")
        event_id = uuid.uuid4()
        next_event_seq = face.get(wid).next_event_seq
        outface = OutboxAwareFace(
            face, project=_PROJECT, signer=signer, unreachable_probe=lambda: True
        )

        outface.transition_breadcrumb(
            reviewer,
            wid,
            "start",
            payload={"review_artifact_digest": "sha256:test"},
            event_id=event_id,
            expected_event_seq=next_event_seq,
        )

        queued = verify_envelope(read_all(_PROJECT)[0].envelope, signer.public_key())
        assert queued["args"]["event_id"] == str(event_id)
        assert "model_lineage" not in queued["args"]["actor"]

        report = reconcile(_PROJECT, face=face, signer=signer)
        assert report.replayed == 1
        events = face.read_events_since(wid, next_event_seq - 1)
        assert len(events) == 1
        assert events[0].event_id == event_id
        assert events[0].payload["review_artifact_digest"] == "sha256:test"
        assert events[0].canonical_envelope is not None


class TestRegistaReconciliationSurface:
    def test_event_id_cas_and_history_since(self, actor: Actor, face) -> None:
        wid, _ = face.create_breadcrumb(actor, title="Reconcile target")
        before = face.get(wid)
        event_id = uuid.uuid4()

        state = face.transition_breadcrumb(
            actor,
            wid,
            "start",
            payload={"review_artifact_digest": "sha256:artifact"},
            event_id=event_id,
            expected_event_seq=before.next_event_seq,
        )

        assert state == "in_progress"
        events = face.read_events_since(wid, before.last_event_seq)
        assert len(events) == 1
        assert events[0].event_id == event_id
        assert events[0].event_seq == before.next_event_seq
        assert events[0].payload["review_artifact_digest"] == "sha256:artifact"

    def test_lost_transition_response_reconciles_exact_committed_event(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
    ) -> None:
        import psycopg

        class CommitThenDrop:
            def __init__(self, base) -> None:
                self._base = base

            def __getattr__(self, name):
                return getattr(self._base, name)

            def transition_breadcrumb(self, *args, **kwargs):
                self._base.transition_breadcrumb(*args, **kwargs)
                raise psycopg.OperationalError("simulated lost response")

        wid, _ = face.create_breadcrumb(actor, title="Lost response target")
        event_id = uuid.uuid4()
        next_event_seq = face.get(wid).next_event_seq
        outface = OutboxAwareFace(
            CommitThenDrop(face),
            project=_PROJECT,
            signer=signer,
        )

        state = outface.transition_breadcrumb(
            actor,
            wid,
            "start",
            payload={"review_artifact_digest": "sha256:lost-response"},
            event_id=event_id,
            expected_event_seq=next_event_seq,
        )

        assert state == ""
        assert count_ops(_PROJECT) == 1
        assert face.get(wid).current_state == "in_progress"

        report = reconcile(_PROJECT, face=face, signer=signer)
        assert report.replayed == 1
        assert report.conflicts == 0
        assert count_ops(_PROJECT) == 0
        matching = [event for event in face.history(wid) if event.event_id == event_id]
        assert len(matching) == 1

    def test_reconciliation_tolerates_additive_authoritative_metadata(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
    ) -> None:
        class AugmentingReadFace:
            def __init__(self, base) -> None:
                self._base = base

            def __getattr__(self, name):
                return getattr(self._base, name)

            def read_events_since(self, *args, **kwargs):
                return [
                    replace(
                        event,
                        payload={**(event.payload or {}), "store_added": True},
                        actor_metadata={
                            **(event.actor_metadata or {}),
                            "store_added": True,
                        },
                    )
                    for event in self._base.read_events_since(*args, **kwargs)
                ]

        wid, _ = face.create_breadcrumb(actor, title="Additive metadata target")
        event_id = uuid.uuid4()
        next_event_seq = face.get(wid).next_event_seq
        outface = OutboxAwareFace(
            face, project=_PROJECT, signer=signer, unreachable_probe=lambda: True
        )
        outface.transition_breadcrumb(
            actor,
            wid,
            "start",
            payload={"review_artifact_digest": "sha256:stable"},
            event_id=event_id,
            expected_event_seq=next_event_seq,
        )
        face.transition_breadcrumb(
            actor,
            wid,
            "start",
            payload={"review_artifact_digest": "sha256:stable"},
            event_id=event_id,
            expected_event_seq=next_event_seq,
        )

        report = reconcile(_PROJECT, face=AugmentingReadFace(face), signer=signer)
        assert report.replayed == 1
        assert report.conflicts == 0
        assert count_ops(_PROJECT) == 0

    def test_different_event_at_expected_sequence_remains_conflicted(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
    ) -> None:
        wid, _ = face.create_breadcrumb(actor, title="Collision target")
        queued_event_id = uuid.uuid4()
        next_event_seq = face.get(wid).next_event_seq
        outface = OutboxAwareFace(
            face, project=_PROJECT, signer=signer, unreachable_probe=lambda: True
        )
        outface.transition_breadcrumb(
            actor,
            wid,
            "start",
            payload={"review_artifact_digest": "sha256:queued"},
            event_id=queued_event_id,
            expected_event_seq=next_event_seq,
        )

        face.transition_breadcrumb(
            actor,
            wid,
            "start",
            payload={"review_artifact_digest": "sha256:other"},
            event_id=uuid.uuid4(),
            expected_event_seq=next_event_seq,
        )
        report = reconcile(_PROJECT, face=face, signer=signer)

        assert report.replayed == 0
        assert report.conflicts == 1
        assert report.conflict_details[0]["reason"] == "event_id_mismatch"
        assert count_ops(_PROJECT) == 1

    def test_same_event_id_with_different_payload_remains_conflicted(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
    ) -> None:
        wid, _ = face.create_breadcrumb(actor, title="Payload binding target")
        event_id = uuid.uuid4()
        next_event_seq = face.get(wid).next_event_seq
        outface = OutboxAwareFace(
            face, project=_PROJECT, signer=signer, unreachable_probe=lambda: True
        )
        outface.transition_breadcrumb(
            actor,
            wid,
            "start",
            payload={"review_artifact_digest": "sha256:queued"},
            event_id=event_id,
            expected_event_seq=next_event_seq,
        )
        face.transition_breadcrumb(
            actor,
            wid,
            "start",
            payload={"review_artifact_digest": "sha256:other"},
            event_id=event_id,
            expected_event_seq=next_event_seq,
        )

        report = reconcile(_PROJECT, face=face, signer=signer)
        assert report.replayed == 0
        assert report.conflicts == 1
        assert report.conflict_details[0]["reason"] == "payload_mismatch"
        assert count_ops(_PROJECT) == 1

    def test_delegated_op_against_undelegated_event_remains_conflicted(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
    ) -> None:
        """Authorization provenance binds the op, not just the event id.

        The same transition (same event_id, sequence, actor, payload) is
        queued as a delegated authorization and actually committed as a
        direct one. That is not "already applied": the committed event
        authorizes a different principal chain, and replaying the delegated
        op on top of it would launder the authorization. Reconcile must
        conflict with ``delegation_mismatch`` and preserve the op.
        """
        wid, _ = face.create_breadcrumb(actor, title="Delegation binding target")
        event_id = uuid.uuid4()
        next_event_seq = face.get(wid).next_event_seq
        payload = {"review_artifact_digest": "sha256:delegated"}
        delegated_actor = replace(
            actor,
            action_delegation_credentials=(
                # Shape-valid but cryptographically inert: the reconcile-side
                # hash comparison runs before any chain verification would.
                shape_valid_delegation(subject_principal_id=actor.actor_id),
            ),
        )
        outface = OutboxAwareFace(
            face, project=_PROJECT, signer=signer, unreachable_probe=lambda: True
        )
        outface.transition_breadcrumb(
            delegated_actor,
            wid,
            "start",
            payload=payload,
            event_id=event_id,
            expected_event_seq=next_event_seq,
        )

        # The transition actually committed under direct authorization (no
        # credentials) — same event identity, different authorization chain.
        face.transition_breadcrumb(
            actor,
            wid,
            "start",
            payload=payload,
            event_id=event_id,
            expected_event_seq=next_event_seq,
        )

        report = reconcile(_PROJECT, face=face, signer=signer)
        assert report.replayed == 0
        assert report.conflicts == 1
        assert report.conflict_details[0]["reason"] == "delegation_mismatch"
        assert count_ops(_PROJECT) == 1
        # Sanity: the expected-credentials side really did hash the queued
        # document (the comparison was delegation-vs-none, not none-vs-none).
        committed = [event for event in face.history(wid) if event.event_id == event_id][0]
        envelope = json.loads(committed.canonical_envelope)
        assert not envelope.get("authorization", {}).get("credentials")


class TestReportOutput:
    def test_to_dict_is_json_serializable(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
    ) -> None:
        enqueue(_PROJECT, _make_create_op(actor), signer)
        report = reconcile(_PROJECT, face=face, signer=signer)

        d = report.to_dict()
        json.dumps(d, default=str)

        assert d["replayed"] == 1
        assert d["rejected"] == 0
        assert d["conflicts"] == 0

    def test_summary_string(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
    ) -> None:
        enqueue(_PROJECT, _make_create_op(actor), signer)
        report = reconcile(_PROJECT, face=face, signer=signer)

        s = report.summary()
        assert "Replayed: 1" in s


class TestE2EPostgres:
    """Optional e2e against real Postgres — live write succeeds, then DB
    goes unreachable (pool closed) mid-run → op lands in outbox.

    Skipped if Postgres is not available.
    """

    @pytest.mark.postgres
    def test_live_write_then_pool_closed_enqueues(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        tmp_path: Path,
    ) -> None:
        keyset = make_v6_keyset(
            tmp_path,
            principals=("agent:ac-test-agent",),
            filename="v6_keys.json",
        )
        key_path = Path(keyset.path)

        # regista's migrations target Postgres 15, but agent-notes' ephemeral
        # testcontainer is pgvector/pgvector:pg17 (see tests/conftest.py), so
        # reusing AGENT_NOTES_DSN makes create_project fail on the pg17
        # container. Point instead at regista's own pg15 test DB spun up by
        # /projects/regista/docker-compose.test.yml. The default mirrors the
        # DSN constant used across /projects/regista/tests/*.py; the env var
        # allows overriding the host/port in CI.
        dsn = os.environ.get(
            "REGISTA_TEST_DSN",
            "postgresql://regista_test:regista_test@localhost:5432/regista_test",
        )

        import psycopg

        try:
            psycopg.connect(dsn, connect_timeout=2).close()
        except Exception:
            pytest.skip(
                "regista pg15 test DB not available; run: "
                "cd /projects/regista && docker compose -f "
                "docker-compose.test.yml up -d"
            )

        import regista

        from agent_notes.core.regista_face import RegistaFace

        project_name = f"an_outbox_{uuid.uuid4().hex[:8]}"
        reg = None

        # Single outer try/finally so the schema is always dropped and the
        # regista connection pool is always closed — even if create_project
        # fails partway (which would otherwise leak the an_outbox_<uuid>
        # schema) or an assertion aborts the body.
        try:
            try:
                reg = regista.Regista.create_project(dsn, project_name, str(key_path))
            except Exception as exc:
                pytest.skip(f"cannot create regista project: {exc}")

            open_v6_epoch(
                reg,
                keyset,
                principals=("agent:ac-test-agent",),
            )
            reg.register_workflow(canonical_workflow_yaml())

            face = RegistaFace(reg)
            outface = OutboxAwareFace(
                face,
                project=project_name,
                signer=signer,
                unreachable_probe=lambda: False,
            )

            wid, state = outface.create_breadcrumb(actor, title="Live e2e")
            assert wid is not None
            assert state == "open"
            assert outface.last_op_outboxed is False

            with psycopg.connect(dsn) as conn:
                conn.execute(f'DROP SCHEMA IF EXISTS "{project_name}" CASCADE')
                conn.commit()

            wid2, state2 = outface.create_breadcrumb(actor, title="Offline e2e")

            assert wid2 is None
            assert state2 == "open"
            assert outface.last_op_outboxed is True
            assert outface.pending_count() == 1
        finally:
            try:
                with psycopg.connect(dsn) as conn:
                    conn.execute(f'DROP SCHEMA IF EXISTS "{project_name}" CASCADE')
                    conn.commit()
            except Exception:
                pass
            if reg is not None:
                try:
                    reg.close()
                except Exception:
                    pass


class TestFindByIdentifierTransport:
    """AC-1 guardrail for the idempotency lookup: an inability to look the
    key up must NOT read as not-found — that translation is what duplicates
    an existing item (the create path treats None as "mint a new one")."""

    def test_unreachable_lookup_raises_not_returns_none(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
    ) -> None:
        outface = OutboxAwareFace(
            face, project=_PROJECT, signer=signer, unreachable_probe=lambda: True
        )

        with pytest.raises(ConnectionRefusedError, match="cannot decide create-vs-update"):
            outface.find_by_source_identifier("BC-500")

    def test_transport_failure_mid_lookup_propagates_not_returns_none(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
    ) -> None:
        import psycopg

        class FindDropsConnection:
            def __init__(self, base) -> None:
                self._base = base

            def __getattr__(self, name):
                return getattr(self._base, name)

            def find_by_source_identifier(self, *args, **kwargs):
                raise psycopg.OperationalError("simulated lookup transport failure")

        outface = OutboxAwareFace(FindDropsConnection(face), project=_PROJECT, signer=signer)

        with pytest.raises(psycopg.OperationalError):
            outface.find_by_source_identifier("BC-500")

    def test_no_duplicate_create_after_a_refused_lookup(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
    ) -> None:
        """End-to-end consequence: when the lookup cannot run, the create
        never happens either — the item count cannot fork."""
        wid, _ = face.create_breadcrumb(actor, title="Existing", source_identifier="BC-500")
        outface = OutboxAwareFace(
            face, project=_PROJECT, signer=signer, unreachable_probe=lambda: True
        )

        with pytest.raises(ConnectionRefusedError):
            outface.find_by_source_identifier("BC-500")
        # Nothing was enqueued or created by the refused guard.
        assert outface.pending_count() == 0
        assert len(face.list()) == 1

    def test_completed_lookup_still_returns_none_for_a_genuine_miss(
        self,
        outbox_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face,
    ) -> None:
        outface = OutboxAwareFace(
            face, project=_PROJECT, signer=signer, unreachable_probe=lambda: False
        )
        assert outface.find_by_source_identifier("never-filed") is None


class _BusinessErrorFace:
    """A stand-in base face whose transition raises a regista business error."""

    def __init__(self, real_face) -> None:
        self._real = real_face
        self.close = real_face.close
        self.get = real_face.get
        self.list = real_face.list
        self.history = real_face.history
        self.create_breadcrumb = real_face.create_breadcrumb
        self.comment = real_face.comment
        self.acquire_claim = real_face.acquire_claim
        self.heartbeat_claim = real_face.heartbeat_claim
        self.release_claim = real_face.release_claim

    def transition_breadcrumb(self, *args, **kwargs):
        from regista._errors import RegistaError

        raise RegistaError("TRANSITION_NOT_ALLOWED: bogus for test")


class TestBusinessErrorSurfaces:
    """AC-1 guardrail: a regista business/validation error must NOT be swallowed
    into the outbox — only transport/unavailable errors are. Swallowing a
    business error would make the agent believe a write succeeded when regista
    rejected it (provenance corruption, dossier-006 D2)."""

    def test_business_error_raises_not_enqueues(self, outbox_env, signer, actor, face):
        wid, _ = face.create_breadcrumb(actor, title="src")
        outface = OutboxAwareFace(_BusinessErrorFace(face), project=_PROJECT, signer=signer)
        with pytest.raises(Exception):
            outface.transition_breadcrumb(actor, wid, "start")
        assert outface.last_op_outboxed is False
        assert outface.pending_count() == 0
