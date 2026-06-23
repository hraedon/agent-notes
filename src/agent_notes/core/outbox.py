"""Centralized outbox for offline-tolerant regista writes (Plan 009 §6).

When regista is unreachable, write operations are captured as signed DSSE
envelopes in a per-project, per-session JSONL file.  ``reconcile`` replays
them in order, verifying every signature and blocking on conflicts.

Location: ``$XDG_STATE_HOME/regista/outbox/<project>/<session>.jsonl``
(default ``~/.local/state/regista/outbox/``); env override
``AGENT_NOTES_OUTBOX_DIR``.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import psycopg
from psycopg_pool import PoolTimeout

from agent_notes.core.actor import Actor
from agent_notes.core.envelope import (
    LocalKeySigner,
    make_envelope,
    parse_envelope,
)

_PAYLOAD_TYPE = "agent-notes-v1/outbox-op"

_TRANSPORT_ERRORS: tuple[type[Exception], ...] = (
    psycopg.OperationalError,
    psycopg.errors.UndefinedTable,
    psycopg.errors.InvalidSchemaName,
    PoolTimeout,
    ConnectionResetError,
    ConnectionRefusedError,
    OSError,
)

_TERMINAL_PREFIX = "close_"
_TERMINAL_TRANSITIONS = frozenset({"reopen"})


class OutboxPendingError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class OutboxEntry:
    session: str
    client_seq: int
    envelope: dict
    raw_line: str


def outbox_dir() -> Path:
    override = os.environ.get("AGENT_NOTES_OUTBOX_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "regista" / "outbox"


def _session_id() -> str:
    existing = os.environ.get("AGENT_NOTES_SESSION")
    if existing:
        return existing
    new_id = uuid.uuid4().hex
    os.environ["AGENT_NOTES_SESSION"] = new_id
    return new_id


def outbox_path(project: str, session: str) -> Path:
    path = outbox_dir() / project / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                count += 1
    return count


def enqueue(project: str, op: dict, signer: LocalKeySigner) -> dict:
    session = _session_id()
    path = outbox_path(project, session)
    client_seq = _count_lines(path) + 1
    op = {**op, "client_seq": client_seq}
    envelope = make_envelope(_PAYLOAD_TYPE, op, signer=signer)
    line = json.dumps(envelope) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())
    return envelope


def _is_session_file(path: Path) -> bool:
    name = path.name
    return name.endswith(".jsonl") and ".rejected." not in name and ".conflicts." not in name


def _session_files(project_dir: Path) -> list[Path]:
    return sorted(p for p in project_dir.glob("*.jsonl") if _is_session_file(p))


def read_all(project: str) -> list[OutboxEntry]:
    project_dir = outbox_dir() / project
    if not project_dir.is_dir():
        return []
    entries: list[OutboxEntry] = []
    for path in _session_files(project_dir):
        session = path.stem
        line_no = 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                line_no += 1
                try:
                    envelope = json.loads(stripped)
                except (ValueError, TypeError):
                    envelope = {}
                    client_seq = line_no
                else:
                    try:
                        payload = parse_envelope(envelope)
                        client_seq = int(payload.get("client_seq", 0))
                    except (ValueError, KeyError, TypeError):
                        client_seq = line_no
                entries.append(
                    OutboxEntry(
                        session=session,
                        client_seq=client_seq,
                        envelope=envelope if isinstance(envelope, dict) else {},
                        raw_line=stripped,
                    )
                )
    entries.sort(key=lambda e: (e.session, e.client_seq))
    return entries


def remove_ops(project: str, session: str, client_seqs: set[int]) -> None:
    path = outbox_path(project, session)
    if not path.exists():
        return
    kept: list[str] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                envelope = json.loads(stripped)
                payload = parse_envelope(envelope)
                seq = int(payload.get("client_seq", 0))
            except (ValueError, KeyError, TypeError):
                kept.append(stripped)
                continue
            if seq not in client_seqs:
                kept.append(stripped)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for line in kept:
            f.write(line + "\n")
    os.replace(tmp, path)


def count_ops(project: str) -> int:
    project_dir = outbox_dir() / project
    if not project_dir.is_dir():
        return 0
    count = 0
    for path in _session_files(project_dir):
        count += _count_lines(path)
    return count


def count_sidecar(project: str, suffix: str) -> int:
    project_dir = outbox_dir() / project
    if not project_dir.is_dir():
        return 0
    count = 0
    for path in project_dir.glob(f"*.{suffix}"):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    count += 1
    return count


def list_projects() -> list[str]:
    root = outbox_dir()
    if not root.is_dir():
        return []
    return sorted(
        d.name for d in root.iterdir() if d.is_dir() and any(_session_files(d))
    )


_SIGNER: LocalKeySigner | None = None


def get_signer() -> LocalKeySigner:
    global _SIGNER
    if _SIGNER is None:
        _SIGNER = LocalKeySigner()
    return _SIGNER


def _actor_to_dict(actor: Actor) -> dict:
    return {
        "actor_id": actor.actor_id,
        "actor_kind": actor.actor_kind,
        "display_name": actor.display_name,
        "on_behalf_of": actor.on_behalf_of,
        "role": actor.role,
    }


def dict_to_actor(d: dict) -> Actor:
    return Actor(
        actor_id=d["actor_id"],
        actor_kind=d.get("actor_kind", "agent"),
        display_name=d.get("display_name", ""),
        on_behalf_of=d.get("on_behalf_of"),
        role=d.get("role", "agent"),
    )


class OutboxAwareFace:
    """Wraps a RegistaFace with an offline-tolerant outbox (AC-1).

    Write methods try the base face; on transport-level unreachability they
    enqueue a signed op to the outbox and return a non-authoritative
    placeholder.  The real state resolves at reconcile time.

    ``last_op_outboxed`` is set after each call: True when the op went to
    the outbox, False when it hit regista live.
    """

    def __init__(
        self,
        base_face: Any,
        *,
        project: str,
        signer: LocalKeySigner | None = None,
        unreachable_probe: Callable[[], bool] | None = None,
    ) -> None:
        self._base = base_face
        self._project = project
        self._signer = signer or get_signer()
        self._unreachable_probe = unreachable_probe
        self.last_op_outboxed: bool = False

    def ensure_workflow(self) -> None:
        self._base.ensure_workflow()

    def close(self) -> None:
        self._base.close()

    def get(self, work_item_id: Any) -> Any:
        return self._base.get(work_item_id)

    def list(self, *, current_states: list[str] | None = None, page_size: int = 100) -> list[Any]:
        return self._base.list(current_states=current_states, page_size=page_size)

    def history(self, work_item_id: Any) -> list[Any]:
        return self._base.history(work_item_id)

    def pending_count(self) -> int:
        return count_ops(self._project)

    def _is_unreachable(self) -> bool:
        return self._unreachable_probe is not None and self._unreachable_probe()

    def _enqueue_op(
        self,
        op_type: str,
        actor: Actor,
        work_item_id: Any,
        expected_state: str | None,
        **kwargs: Any,
    ) -> None:
        args: dict[str, Any] = {"actor": _actor_to_dict(actor), **kwargs}
        op = {
            "op": op_type,
            "work_item_id": str(work_item_id) if work_item_id is not None else None,
            "args": args,
            "expected_state": expected_state,
        }
        enqueue(self._project, op, self._signer)
        self.last_op_outboxed = True
        if work_item_id is not None:
            self._maybe_set_pending(work_item_id, True)

    @staticmethod
    def _maybe_set_pending(work_item_id: Any, pending: bool) -> None:
        try:
            from agent_notes.core import projection
            from agent_notes.core.db import _conn

            with _conn() as conn:
                projection.set_pending_sync(conn, work_item_id, pending)
                conn.commit()
        except Exception:
            pass

    @staticmethod
    def _maybe_clear_pending(work_item_id: Any) -> None:
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

    def create_breadcrumb(
        self,
        actor: Actor,
        *,
        title: str,
        description: str = "",
        severity: str = "medium",
        kind: str = "todo",
        external_refs: dict | None = None,
        diagnostic_keys: dict | None = None,
        source_identifier: str | None = None,
    ) -> tuple[Any, str]:
        kwargs = dict(
            title=title,
            description=description,
            severity=severity,
            kind=kind,
            external_refs=external_refs,
            diagnostic_keys=diagnostic_keys,
            source_identifier=source_identifier,
        )
        if self._is_unreachable():
            self._enqueue_op("create", actor, None, None, **kwargs)
            return None, "open"
        try:
            result = self._base.create_breadcrumb(actor, **kwargs)
            self.last_op_outboxed = False
            self._maybe_clear_pending(result[0])
            return result
        except _TRANSPORT_ERRORS:
            self._enqueue_op("create", actor, None, None, **kwargs)
            return None, "open"

    def amend_breadcrumb(
        self,
        actor: Actor,
        work_item_id: Any,
        *,
        current_state: str,
        title: str | None = None,
        description: str | None = None,
        severity: str | None = None,
        kind: str | None = None,
        external_refs: dict | None = None,
        diagnostic_keys: dict | None = None,
        payload: dict | None = None,
    ) -> str:
        kwargs = dict(
            current_state=current_state,
            title=title,
            description=description,
            severity=severity,
            kind=kind,
            external_refs=external_refs,
            diagnostic_keys=diagnostic_keys,
            payload=payload,
        )
        if self._is_unreachable():
            self._enqueue_op("amend", actor, work_item_id, current_state, **kwargs)
            return current_state
        try:
            result = self._base.amend_breadcrumb(actor, work_item_id, **kwargs)
            self.last_op_outboxed = False
            self._maybe_clear_pending(work_item_id)
            return result
        except _TRANSPORT_ERRORS:
            self._enqueue_op("amend", actor, work_item_id, current_state, **kwargs)
            return current_state

    def transition_breadcrumb(
        self,
        actor: Actor,
        work_item_id: Any,
        transition_name: str,
        *,
        payload: dict | None = None,
        custom_fields: dict | None = None,
        expected_event_seq: int | None = None,
    ) -> str:
        if transition_name.startswith(_TERMINAL_PREFIX) or transition_name in _TERMINAL_TRANSITIONS:
            n = self.pending_count()
            if n > 0:
                raise OutboxPendingError(
                    f"{n} op(s) pending sync; run 'agent-notes outbox reconcile' "
                    f"before marking work done"
                )
        kwargs = dict(
            transition_name=transition_name,
            payload=payload,
            custom_fields=custom_fields,
            expected_event_seq=expected_event_seq,
        )
        if self._is_unreachable():
            self._enqueue_op("transition", actor, work_item_id, None, **kwargs)
            return ""
        try:
            result = self._base.transition_breadcrumb(
                actor,
                work_item_id,
                transition_name,
                payload=payload,
                custom_fields=custom_fields,
                expected_event_seq=expected_event_seq,
            )
            self.last_op_outboxed = False
            self._maybe_clear_pending(work_item_id)
            return result
        except _TRANSPORT_ERRORS:
            self._enqueue_op("transition", actor, work_item_id, None, **kwargs)
            return ""

    def comment(self, actor: Actor, work_item_id: Any, body: str) -> None:
        kwargs = dict(body=body)
        if self._is_unreachable():
            self._enqueue_op("comment", actor, work_item_id, None, **kwargs)
            return None
        try:
            self._base.comment(actor, work_item_id, body)
            self.last_op_outboxed = False
            self._maybe_clear_pending(work_item_id)
            return None
        except _TRANSPORT_ERRORS:
            self._enqueue_op("comment", actor, work_item_id, None, **kwargs)
            return None
