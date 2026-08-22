"""Outbox reconcile — replay pending ops into regista (Plan 009 §6.4).

Walks the project's outbox in client_seq order, verifies every signature
(AC-2), applies each op to regista via the face, detects conflicts (AC-3),
and removes successfully replayed ops.  Rejected and conflicted ops are
written to sidecar files for human review.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from regista._errors import ErrorCode, RegistaError

from agent_notes.core import outbox
from agent_notes.core.envelope import verify_envelope

_CONFLICT_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.INVALID_TRANSITION,
        ErrorCode.CONCURRENT_MODIFICATION,
        ErrorCode.WORK_ITEM_NOT_FOUND,
    }
)


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    replayed: int
    rejected: int
    conflicts: int
    conflict_details: list[dict] = field(default_factory=list)
    rejected_details: list[dict] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.replayed > 0

    def summary(self) -> str:
        parts = [f"Replayed: {self.replayed}"]
        if self.rejected:
            parts.append(f"Rejected: {self.rejected}")
        if self.conflicts:
            parts.append(f"Conflicts: {self.conflicts}")
        return ", ".join(parts)

    def to_dict(self) -> dict:
        return {
            "replayed": self.replayed,
            "rejected": self.rejected,
            "conflicts": self.conflicts,
            "conflict_details": self.conflict_details,
            "rejected_details": self.rejected_details,
        }


def _sidecar_path(project: str, session: str, suffix: str) -> Path:
    return outbox.outbox_dir() / project / f"{session}.{suffix}"


def _write_sidecar(project: str, session: str, suffix: str, data: dict) -> None:
    path = _sidecar_path(project, session, suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(data, default=str) + "\n")
        f.flush()


def _maybe_clear_pending_sync(work_item_id: Any) -> None:
    if work_item_id is None:
        return
    try:
        from agent_notes.core import projection
        from agent_notes.core.db import _conn

        with _conn() as conn:
            projection.set_pending_sync(conn, work_item_id, False)
            conn.commit()
    except Exception:
        pass


def _maybe_clear_note_pending_sync(entity_id: Any) -> None:
    if entity_id is None:
        return
    try:
        from agent_notes.core.db import _conn

        with _conn() as conn:
            conn.execute(
                "UPDATE memories SET pending_sync = FALSE WHERE regista_note_id = %s",
                (str(entity_id),),
            )
            conn.commit()
    except Exception:
        pass


def _parse_wid(wid_str: str | None) -> uuid.UUID | None:
    if not wid_str:
        return None
    return uuid.UUID(wid_str)


def _reconcile_committed_transition(
    face: Any,
    wid: uuid.UUID | None,
    actor: Any,
    args: dict,
) -> tuple[bool, str | None]:
    """Recognize a transition whose commit response was lost.

    Returns ``(True, None)`` for an exact authoritative match,
    ``(False, reason)`` when another event occupies the expected sequence, and
    ``(False, None)`` when no event exists yet and replay should proceed.
    """
    event_id_raw = args.get("event_id")
    expected_seq = args.get("expected_event_seq")
    if wid is None or event_id_raw is None or expected_seq is None:
        return False, None
    try:
        event_id = uuid.UUID(str(event_id_raw))
        expected_seq = int(expected_seq)
    except (TypeError, ValueError):
        return False, "invalid_reconciliation_identity"
    if expected_seq < 1:
        return False, "invalid_reconciliation_sequence"

    events = face.read_events_since(wid, expected_seq - 1, limit=1)
    if not events:
        return False, None
    event = events[0]
    expected_payload = args.get("payload")
    expected_metadata = actor.actor_metadata()
    actual_payload = getattr(event, "payload", None)
    payload_matches = actual_payload == expected_payload
    if isinstance(expected_payload, dict) and isinstance(actual_payload, dict):
        payload_matches = all(
            key in actual_payload and actual_payload[key] == value
            for key, value in expected_payload.items()
        )
    actual_metadata = getattr(event, "actor_metadata", None) or {}
    metadata_matches = all(
        key in actual_metadata and actual_metadata[key] == value
        for key, value in expected_metadata.items()
    )
    import regista

    expected_credentials = [
        {
            "credential_id": str(document.get("credential_id", "")),
            "credential_hash": regista.action_delegation_hash(document),
        }
        for document in actor.action_delegation_credentials
    ]
    actual_credentials: list[dict[str, Any]] = []
    canonical_envelope = getattr(event, "canonical_envelope", None)
    if canonical_envelope is not None:
        try:
            envelope = json.loads(canonical_envelope)
        except (TypeError, ValueError, UnicodeDecodeError):
            envelope = None
        if isinstance(envelope, dict):
            authorization = envelope.get("authorization")
            if isinstance(authorization, dict) and isinstance(
                authorization.get("credentials"), list
            ):
                actual_credentials = authorization["credentials"]

    checks = (
        (getattr(event, "event_seq", None) == expected_seq, "event_seq_mismatch"),
        (getattr(event, "event_id", None) == event_id, "event_id_mismatch"),
        (getattr(event, "work_item_id", None) == wid, "work_item_mismatch"),
        (
            getattr(event, "transition", None) == args.get("transition_name"),
            "transition_mismatch",
        ),
        (getattr(event, "actor_id", None) == actor.actor_id, "actor_id_mismatch"),
        (getattr(event, "actor_kind", None) == actor.actor_kind, "actor_kind_mismatch"),
        (metadata_matches, "actor_metadata_mismatch"),
        (actual_credentials == expected_credentials, "delegation_mismatch"),
        (payload_matches, "payload_mismatch"),
    )
    for matched, reason in checks:
        if not matched:
            return False, reason
    return True, None


def reconcile(
    project: str,
    *,
    face: Any = None,
    signer: Any = None,
) -> ReconcileReport:
    if signer is None:
        signer = outbox.get_signer()
    if face is None:
        from agent_notes.core.face_factory import get_face

        face = get_face()
        if face is None:
            raise RuntimeError("no regista face available; pass face= explicitly")
        if hasattr(face, "_base"):
            face = face._base

    public_key = signer.public_key() if hasattr(signer, "public_key") else signer._public_key

    replayed = 0
    rejected = 0
    conflicts = 0
    conflict_details: list[dict] = []
    rejected_details: list[dict] = []

    entries = outbox.read_all(project)
    for entry in entries:
        try:
            payload = verify_envelope(entry.envelope, public_key)
        except Exception as exc:
            rejected += 1
            detail = {
                "session": entry.session,
                "client_seq": entry.client_seq,
                "reason": str(exc),
            }
            rejected_details.append(detail)
            _write_sidecar(project, entry.session, "rejected.jsonl", detail)
            continue

        op = payload
        op_type = op.get("op", "")
        wid = _parse_wid(op.get("work_item_id"))
        expected_state = op.get("expected_state")
        args = dict(op.get("args", {}))
        actor_dict = args.pop("actor", None)
        actor = outbox.dict_to_actor(actor_dict) if actor_dict else None

        if actor is None:
            rejected += 1
            detail = {
                "session": entry.session,
                "client_seq": entry.client_seq,
                "reason": "missing actor in op args",
            }
            rejected_details.append(detail)
            _write_sidecar(project, entry.session, "rejected.jsonl", detail)
            continue

        if op_type == "transition":
            already_applied, binding_conflict = _reconcile_committed_transition(
                face, wid, actor, args
            )
            if binding_conflict is not None:
                conflicts += 1
                detail = {
                    "session": entry.session,
                    "client_seq": entry.client_seq,
                    "op": op_type,
                    "work_item_id": str(wid) if wid else None,
                    "expected_state": expected_state,
                    "reason": binding_conflict,
                }
                conflict_details.append(detail)
                _write_sidecar(project, entry.session, "conflicts.jsonl", detail)
                continue
            if already_applied:
                outbox.remove_ops(project, entry.session, {entry.client_seq})
                _maybe_clear_pending_sync(wid)
                replayed += 1
                continue

        conflict = _check_conflict(face, wid, expected_state)
        if conflict is not None:
            conflicts += 1
            detail = {
                "session": entry.session,
                "client_seq": entry.client_seq,
                "op": op_type,
                "work_item_id": str(wid) if wid else None,
                "expected_state": expected_state,
                "actual_state": conflict,
                "reason": "state_mismatch",
            }
            conflict_details.append(detail)
            _write_sidecar(project, entry.session, "conflicts.jsonl", detail)
            continue

        try:
            _dispatch(face, op_type, actor, wid, args)
        except RegistaError as exc:
            if exc.code in _CONFLICT_CODES:
                conflicts += 1
                detail = {
                    "session": entry.session,
                    "client_seq": entry.client_seq,
                    "op": op_type,
                    "work_item_id": str(wid) if wid else None,
                    "expected_state": expected_state,
                    "reason": f"transition_error: {exc.code.name}",
                    "error": str(exc),
                }
                conflict_details.append(detail)
                _write_sidecar(project, entry.session, "conflicts.jsonl", detail)
                continue
            raise

        outbox.remove_ops(project, entry.session, {entry.client_seq})
        if op_type == "note_append":
            _maybe_clear_note_pending_sync(wid)
        else:
            _maybe_clear_pending_sync(wid)
        replayed += 1

    return ReconcileReport(
        replayed=replayed,
        rejected=rejected,
        conflicts=conflicts,
        conflict_details=conflict_details,
        rejected_details=rejected_details,
    )


def _check_conflict(face: Any, wid: uuid.UUID | None, expected_state: str | None) -> str | None:
    if wid is None or expected_state is None:
        return None
    try:
        wi = face.get(wid)
    except Exception:
        return None
    if wi is None:
        return None
    actual = getattr(wi, "current_state", None)
    if actual is not None and actual != expected_state:
        return actual
    return None


def _dispatch(
    face: Any,
    op_type: str,
    actor: Any,
    wid: uuid.UUID | None,
    args: dict,
) -> None:
    if op_type == "create":
        face.create_breadcrumb(actor, **args)
    elif op_type == "amend":
        face.amend_breadcrumb(actor, wid, **args)
    elif op_type == "transition":
        transition_name = args.pop("transition_name")
        event_id = args.get("event_id")
        if event_id is not None:
            args["event_id"] = uuid.UUID(str(event_id))
        face.transition_breadcrumb(actor, wid, transition_name, **args)
    elif op_type == "comment":
        body = args.pop("body")
        face.comment(actor, wid, body)
    elif op_type == "note_append":
        transition = args.pop("transition")
        payload = args.pop("payload", None)
        face.append_note(actor, wid, transition=transition, payload=payload)
    else:
        raise ValueError(f"unknown op type: {op_type!r}")
