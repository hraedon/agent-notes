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
from pathlib import Path

import pytest
from regista.testing import InMemoryRegista

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
    return Actor(actor_id="ac-test-agent", display_name="AC Test")


@pytest.fixture
def face() -> "RegistaFace":  # type: ignore[name-defined]  # noqa: F821
    from agent_notes.core.regista_face import RegistaFace

    return RegistaFace(InMemoryRegista())


def _actor_dict(actor: Actor) -> dict:
    return {
        "actor_id": actor.actor_id,
        "actor_kind": actor.actor_kind,
        "display_name": actor.display_name,
        "on_behalf_of": actor.on_behalf_of,
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
        state = outface.amend_breadcrumb(
            actor, wid, current_state="open", title="Updated"
        )

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
            _make_transition_op(actor, wid, "close_open", expected_state="open"),
            signer,
        )

        face.transition_breadcrumb(actor, wid, "close_open")
        assert face.get(wid).current_state == "closed"

        report = reconcile(_PROJECT, face=face, signer=signer)

        assert report.replayed == 0
        assert report.conflicts == 1
        assert len(report.conflict_details) == 1
        detail = report.conflict_details[0]
        assert detail["expected_state"] == "open"
        assert detail["actual_state"] == "closed"

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
        face.transition_breadcrumb(actor, wid, "close_open")

        enqueue(
            _PROJECT,
            _make_transition_op(actor, wid, "close_open", expected_state=None),
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
            outface.transition_breadcrumb(actor, wid, "close_open")

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
        face.transition_breadcrumb(actor, wid, "close_open")

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
        outface.transition_breadcrumb(actor, wid, "claim")

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
            _make_transition_op(actor, wid, "close_open", expected_state="open"),
            signer,
        )

        report2 = reconcile(_PROJECT, face=face, signer=signer)
        assert report2.replayed == 1
        assert face.get(wid).current_state == "closed"
        assert count_ops(_PROJECT) == 0


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
        import json as _json

        key_path = tmp_path / "hmac_keys.json"
        key_path.write_text(
            _json.dumps(
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

        dsn = os.environ.get("AGENT_NOTES_DSN")
        if not dsn:
            pytest.skip("AGENT_NOTES_DSN not set; skipping e2e")

        import regista

        from agent_notes.core.regista_face import RegistaFace

        project_name = f"an_outbox_{uuid.uuid4().hex[:8]}"

        try:
            reg = regista.Regista.create_project(dsn, project_name, str(key_path))
        except Exception as exc:
            pytest.skip(f"cannot create regista project: {exc}")

        try:
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

            import psycopg

            with psycopg.connect(dsn) as conn:
                conn.execute(f'DROP SCHEMA IF EXISTS "{project_name}" CASCADE')
                conn.commit()

            wid2, state2 = outface.create_breadcrumb(actor, title="Offline e2e")

            assert wid2 is None
            assert state2 == "open"
            assert outface.last_op_outboxed is True
            assert outface.pending_count() == 1
        finally:
            import psycopg

            try:
                with psycopg.connect(dsn) as conn:
                    conn.execute(
                        f'DROP SCHEMA IF EXISTS "{project_name}" CASCADE'
                    )
                    conn.commit()
            except Exception:
                pass


class _BusinessErrorFace:
    """A stand-in base face whose transition raises a regista business error."""

    def __init__(self, real_face) -> None:
        self._real = real_face
        self.ensure_workflow = real_face.ensure_workflow
        self.close = real_face.close
        self.get = real_face.get
        self.list = real_face.list
        self.history = real_face.history
        self.create_breadcrumb = real_face.create_breadcrumb
        self.comment = real_face.comment

    def transition_breadcrumb(self, *args, **kwargs):
        from regista._errors import RegistaError

        raise RegistaError("TRANSITION_NOT_ALLOWED: bogus for test")


class TestBusinessErrorSurfaces:
    """AC-1 guardrail: a regista business/validation error must NOT be swallowed
    into the outbox — only transport/unavailable errors are. Swallowing a
    business error would make the agent believe a write succeeded when regista
    rejected it (provenance corruption, dossier-006 D2)."""

    def test_business_error_raises_not_enqueues(
        self, outbox_env, signer, actor, face
    ):
        wid, _ = face.create_breadcrumb(actor, title="src")
        outface = OutboxAwareFace(_BusinessErrorFace(face), project=_PROJECT, signer=signer)
        with pytest.raises(Exception):
            outface.transition_breadcrumb(actor, wid, "claim")
        assert outface.last_op_outboxed is False
        assert outface.pending_count() == 0

