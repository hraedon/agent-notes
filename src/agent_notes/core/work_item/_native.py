"""Native op-log write path for work items (degrade mode — no regista face).

These functions implement the work-item mutations against the local op-log
and ``work_items`` cache directly. They are used when ``face_factory.get_face()``
returns ``None`` (regista writes disabled, or coordinator-absent). Each
preserves the behavior of the original ``WorkItemModel`` native branches.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_notes.core import face_factory, kernel
from agent_notes.core.change_log import write_change
from agent_notes.core.db import _conn
from agent_notes.core.lifecycle import (
    is_terminal,
)
from agent_notes.core.lifecycle import (
    transition_for as _lifecycle_transition_for,
)

from . import _common

KIND = "work_item"
ENTITY_TYPE = "work_item"

_REVIEW_TRANSITION_TO_STATUS: dict[str, str] = {
    "adversarial_pass": "in_human_review",
    "accept": "done",
    "reject": "in_progress",
    "request_changes": "in_progress",
}


def file_work_item(
    project_id: int,
    identifier: str | None,
    title: str,
    body: str,
    kind: str,
    status: str,
    severity: str,
    external_refs: dict | None,
    diagnostic_keys: dict | None,
    embedding: Any | None,
    frontmatter_version: int,
    actor_id: str | None,
    model_lineage: str | None = None,
) -> dict:
    # WI-062: refuse before any op is written, not after.
    face_factory.assert_declared_lineage(actor_id, model_lineage, operation="work-item file")
    with _conn() as conn:
        workspace_id = _common.resolve_workspace_for_project(conn, project_id)
        _common.validate_vocab(conn, workspace_id, "wi_kind", kind)
        _common.validate_vocab(conn, workspace_id, "wi_status", status)
        _common.validate_vocab(conn, workspace_id, "wi_severity", severity)

        if identifier is None:
            cur = conn.cursor(row_factory=psycopg.rows.tuple_row)
            cur.execute("SELECT allocate_work_item_identifier(%s)", (project_id,))
            identifier = cur.fetchone()[0]

        # Store body as content-addressed blob.
        body_hash = kernel.store_blob(conn, body)

        # Build the create op payload.
        payload = {
            "project_id": project_id,
            "identifier": identifier,
            "title": title,
            "body_hash": body_hash,
            "kind": kind,
            "status": status,
            "severity": severity,
            "external_refs": external_refs or {},
            "diagnostic_keys": diagnostic_keys or {},
            "embedding": embedding,
            "frontmatter_version": frontmatter_version,
            "model_lineage": model_lineage,
        }

        # The entity_id is the hash of the create op itself.
        entity_id = kernel._make_op_id(ENTITY_TYPE, "create", payload, [])

        # Commit the create op.
        op = kernel.commit_op(
            conn,
            entity_id=entity_id,
            entity_type=ENTITY_TYPE,
            op_type="create",
            payload=payload,
            actor_id=actor_id,
        )

        # Fold into cache.
        folded = kernel.fold_work_item(conn, entity_id)
        if folded is None:
            raise RuntimeError("fold_work_item returned None after create op")

        # Write change_log for backward compatibility.
        write_change(
            conn,
            kind=KIND,
            workspace_id=workspace_id,
            project_id=project_id,
            identifier=identifier,
            event="filed",
            payload={"title": title, "kind": kind, "status": status},
        )

        # Emit event for the post-commit hook surface.
        kernel.emit_event(
            conn,
            op_id=op["op_id"],
            event_type="item.created",
            payload={
                "entity_id": entity_id,
                "identifier": identifier,
                "project_id": project_id,
            },
        )

        conn.commit()
        return dict(folded)


def update_work_item(
    conn: psycopg.Connection,
    project_id: int,
    identifier: str,
    title: str | None = None,
    body: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    severity: str | None = None,
    external_refs: dict | None = None,
    diagnostic_keys: dict | None = None,
    embedding: Any | None = None,
    frontmatter_version: int | None = None,
    actor_id: str | None = None,
    model_lineage: str | None = None,
    force: bool = False,
) -> dict:
    """Apply field / status changes to a work item on the caller's transaction.

    Takes the connection so a caller can compose several updates atomically
    (WI-020): it writes ops, folds the cache, and writes the change_log row but
    never commits — the caller owns the transaction boundary.
    """
    face_factory.assert_declared_lineage(actor_id, model_lineage, operation="work-item update")
    workspace_id = _common.resolve_workspace_for_project(conn, project_id)
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        "SELECT * FROM work_items WHERE project_id = %s AND identifier = %s",
        (project_id, identifier),
    )
    old = cur.fetchone()
    if old is None:
        raise ValueError(f"Work item not found: {identifier!r} in project {project_id}")

    entity_id = old["entity_id"]
    old_body_hash = old["body_hash"]
    old_body = kernel.get_blob(conn, old_body_hash) or ""

    if kind is not None:
        _common.validate_vocab(conn, workspace_id, "wi_kind", kind)
    if status is not None:
        _common.validate_vocab(conn, workspace_id, "wi_status", status)
    if severity is not None:
        _common.validate_vocab(conn, workspace_id, "wi_severity", severity)

    # Build payload for the set_field op, diffing each field against
    # the item's current values so an unchanged re-import writes no op
    # (WI-008/WI-009 — op-log bloat on breadcrumb re-sync).
    payload: dict = {}
    if title is not None and title != old["title"]:
        payload["title"] = title
    if body is not None:
        body_hash = kernel.store_blob(conn, body)
        if body_hash != old["body_hash"]:
            payload["body_hash"] = body_hash
    if kind is not None and kind != old["kind"]:
        payload["kind"] = kind
    if severity is not None and severity != old["severity"]:
        payload["severity"] = severity
    if external_refs is not None and old.get("external_refs") != external_refs:
        payload["external_refs"] = external_refs
    if diagnostic_keys is not None and old.get("diagnostic_keys") != diagnostic_keys:
        payload["diagnostic_keys"] = diagnostic_keys
    if embedding is not None and not _common.embedding_equal(embedding, old.get("embedding")):
        payload["embedding"] = embedding
    if frontmatter_version is not None and frontmatter_version != old.get("frontmatter_version"):
        payload["frontmatter_version"] = frontmatter_version
    if model_lineage is not None:
        payload["model_lineage"] = model_lineage

    # If status changed, write a separate set_status op.
    status_changed = False
    if status is not None and status != old["status"]:
        # Plan 013 WI-5: pre-flight transition check on the native path.
        # Both paths (regista + native) now reject the same illegal
        # transitions. ``force=True`` is the explicit admin escape hatch.
        if not force:
            transition = _lifecycle_transition_for(old["status"], status)
            # Plan 014 WI-2: degrade mode cannot run the cross-lineage
            # review gate, so it must not *complete* work unilaterally.
            # The only gate transition into a terminal state is `accept`
            # (in_human_review → done); block it off-regista. `close_from_open`
            # (the review-exempt won't-fix/duplicate dismissal) stays allowed,
            # matching the regista path.
            if transition == "accept":
                raise ValueError(
                    f"Cannot accept {identifier!r} to 'done' in degrade mode: "
                    "completion requires regista's cross-lineage review gate. "
                    "Use force=True only for admin/repair."
                )
        status_op = kernel.commit_op(
            conn,
            entity_id=entity_id,
            entity_type=ENTITY_TYPE,
            op_type="set_status",
            payload={"status": status},
            parent_op_ids=[old["entity_id"]],  # simplistic parent chaining
            actor_id=actor_id,
        )
        kernel.emit_event(
            conn,
            op_id=status_op["op_id"],
            event_type="item.status_changed",
            payload={
                "entity_id": entity_id,
                "identifier": identifier,
                "old_status": old["status"],
                "new_status": status,
            },
        )
        status_changed = True

    # Write the set_field op if any non-status fields changed.
    if payload:
        op = kernel.commit_op(
            conn,
            entity_id=entity_id,
            entity_type=ENTITY_TYPE,
            op_type="set_field",
            payload=payload,
            parent_op_ids=[entity_id],
            actor_id=actor_id,
        )
        kernel.emit_event(
            conn,
            op_id=op["op_id"],
            event_type="item.updated",
            payload={
                "entity_id": entity_id,
                "identifier": identifier,
                "fields": list(payload.keys()),
            },
        )

    # Nothing changed (no status op, no set_field op): skip the fold
    # and change_log entirely, returning the current state as-is
    # (WI-008/WI-009).
    if not payload and not status_changed:
        return dict(old)

    # Fold into cache.
    folded = kernel.fold_work_item(conn, entity_id)
    if folded is None:
        raise RuntimeError("fold_work_item returned None after update op")

    # Backward-compat change_log — only when something actually changed.
    # ``folded`` is the work_items cache row (no ``body`` column — only
    # ``body_hash``), so resolve the new body from the blob for the diff,
    # mirroring the regista path (``_update_change_log_payload``).
    new_body = kernel.get_blob(conn, folded["body_hash"]) or ""
    cl_payload: dict = {}
    for field in ("title", "kind", "status", "severity"):
        old_val = old.get(field)
        new_val = folded.get(field)
        if old_val != new_val:
            cl_payload[field] = {"from": old_val, "to": new_val}
    if old_body != new_body:
        cl_payload["body"] = {"from": old_body, "to": new_body}
    if external_refs is not None and old.get("external_refs") != external_refs:
        cl_payload["external_refs"] = external_refs
    if diagnostic_keys is not None and old.get("diagnostic_keys") != diagnostic_keys:
        cl_payload["diagnostic_keys"] = diagnostic_keys

    if cl_payload:
        write_change(
            conn,
            kind=KIND,
            workspace_id=workspace_id,
            project_id=project_id,
            identifier=identifier,
            event="updated",
            payload=cl_payload,
        )

    return dict(folded)


def close_work_item_native_deferred(
    project_id: int,
    identifier: str,
    old: dict,
    actor_id: str | None,
    model_lineage: str | None = None,
) -> dict:
    """Native (degrade) close that defers to ``in_review`` (Plan 014 A(b)).

    Mirrors ``_close_work_item_regista``'s state handling, but drives the
    op-log via ``update_work_item`` (with the Plan 013 WI-5 transition
    pre-flight) instead of a regista face. Reaching a terminal state is not
    possible here — completion requires regista's review gate.
    """
    state = old["status"]
    canonical = "done" if state == "closed" else state
    if canonical in ("in_review", "in_human_review"):
        return dict(old)  # already awaiting review — no-op
    if canonical == "done":
        raise ValueError(f"Work item {identifier!r} is already done; reopen first")
    if canonical == "blocked":
        raise ValueError(f"Work item {identifier!r} is blocked; unblock before closing")
    if canonical == "deferred":
        raise ValueError(f"Work item {identifier!r} is deferred; resume before closing")
    if canonical == "claimed":
        raise ValueError(
            f"Work item {identifier!r} is in legacy 'claimed' state; "
            "migrate to the canonical workflow first"
        )
    # open or in_progress → defer to in_review (never terminal). Both steps run
    # on a single connection and commit once, so the close is atomic (WI-020):
    # the item can no longer be stranded in ``in_progress`` by a crash between
    # the two transitions.
    with _conn() as conn:
        if canonical == "open":
            update_work_item(
                conn,
                project_id=project_id,
                identifier=identifier,
                status="in_progress",
                actor_id=actor_id,
                model_lineage=model_lineage,
            )
        result = update_work_item(
            conn,
            project_id=project_id,
            identifier=identifier,
            status="in_review",
            actor_id=actor_id,
            model_lineage=model_lineage,
        )
        conn.commit()
    return result


def close_work_item_force(
    project_id: int,
    identifier: str,
    actor_id: str | None,
    model_lineage: str | None = None,
) -> dict:
    """Native force-close: writes the legacy terminal ``close`` op (admin/repair)."""
    face_factory.assert_declared_lineage(actor_id, model_lineage, operation="work-item close")
    with _conn() as conn:
        workspace_id = _common.resolve_workspace_for_project(conn, project_id)
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT * FROM work_items WHERE project_id = %s AND identifier = %s",
            (project_id, identifier),
        )
        old = cur.fetchone()
        if old is None:
            raise ValueError(f"Work item not found: {identifier!r} in project {project_id}")

        entity_id = old["entity_id"]
        force_close_payload: dict = {"reason": "manual_close"}
        if model_lineage is not None:
            force_close_payload["model_lineage"] = model_lineage
        op = kernel.commit_op(
            conn,
            entity_id=entity_id,
            entity_type=ENTITY_TYPE,
            op_type="close",
            payload=force_close_payload,
            parent_op_ids=[entity_id],
            actor_id=actor_id,
        )

        folded = kernel.fold_work_item(conn, entity_id)
        if folded is None:
            raise RuntimeError("fold_work_item returned None after close op")

        kernel.emit_event(
            conn,
            op_id=op["op_id"],
            event_type="item.closed",
            payload={"entity_id": entity_id, "identifier": identifier},
        )

        write_change(
            conn,
            kind=KIND,
            workspace_id=workspace_id,
            project_id=project_id,
            identifier=identifier,
            event="status_changed",
            payload={"old_status": old["status"], "new_status": "closed"},
        )

        conn.commit()
        return dict(folded)


def attest_gate_waiver(
    project_id: int,
    identifier: str,
    reason: str,
    actor_id: str | None,
    model_lineage: str | None = None,
) -> dict:
    """Record that the review gate was retroactively waived (Plan 014 WI-4)."""
    from datetime import datetime, timezone

    with _conn() as conn:
        workspace_id = _common.resolve_workspace_for_project(conn, project_id)
        old = _common.load_work_item_row(conn, project_id, identifier)
        entity_id = old["entity_id"]
        status = old["status"]
        canonical = "done" if status == "closed" else status
        if not is_terminal(canonical):
            raise ValueError(
                f"Work item {identifier!r} is not terminal (status={status!r}); "
                "gate attestation applies only to completed items."
            )

        # Only op-log (native-path) items can be attested — regista-path
        # items have no op-chain and are gate-verified by construction.
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM op_log WHERE entity_id = %s AND op_type = 'create'",
            (entity_id,),
        )
        if cur.fetchone() is None:
            raise ValueError(
                f"Work item {identifier!r} has no op-chain (regista-path item); "
                "it is gate-verified by construction and needs no attestation."
            )

        actor = (
            face_factory.actor_with_overrides(
                actor_id, model_lineage, clear_principal=True, operation="attest-gate-waiver"
            )
            if actor_id
            else face_factory.actor_with_overrides(
                None, model_lineage, operation="attest-gate-waiver"
            )
        )
        attestation = {
            "status": "waived",
            "reason": reason,
            "actor_id": actor.actor_id,
            "waived_at": datetime.now(timezone.utc).isoformat(),
        }
        op = kernel.commit_op(
            conn,
            entity_id=entity_id,
            entity_type=ENTITY_TYPE,
            op_type="set_field",
            payload={"diagnostic_keys": {"gate_attestation": attestation}},
            parent_op_ids=[entity_id],
            actor_id=actor.actor_id,
        )

        folded = kernel.fold_work_item(conn, entity_id)
        if folded is None:
            raise RuntimeError("fold_work_item returned None after attest op")

        kernel.emit_event(
            conn,
            op_id=op["op_id"],
            event_type="item.gate_attested",
            payload={
                "entity_id": entity_id,
                "identifier": identifier,
                "attestation": attestation,
            },
        )

        write_change(
            conn,
            kind=KIND,
            workspace_id=workspace_id,
            project_id=project_id,
            identifier=identifier,
            event="gate_attested",
            payload={"attestation": attestation},
            actor=actor.actor_id,
        )

        conn.commit()
        return dict(folded)


def review_transition(
    project_id: int,
    identifier: str,
    transition_name: str,
    review_note: str,
    actor_id: str | None,
    model_lineage: str | None,
) -> dict:
    """Native (degrade) review transition — stores note, defers gate.

    The status transition is written as a ``set_status`` op whose payload
    includes the ``review_note`` (making the op_id unique — the op-log
    content-addresses ops by ``(entity_type, op_type, payload,
    parent_op_ids)``, so a bare ``{"status": "in_progress"}`` collides
    when the same status is revisited, e.g. ``reject`` → ``in_progress``
    after ``start`` → ``in_progress``).  ``accept`` is blocked (Plan 014
    WI-2) because the cross-lineage gate cannot run off-regista.
    """
    from datetime import datetime, timezone

    face_factory.assert_declared_lineage(
        actor_id, model_lineage, operation=f"work-item review {transition_name}"
    )
    target_status = _REVIEW_TRANSITION_TO_STATUS[transition_name]

    with _conn() as conn:
        workspace_id = _common.resolve_workspace_for_project(conn, project_id)
        old = _common.load_work_item_row(conn, project_id, identifier)
        entity_id = old["entity_id"]

        # Plan 014 WI-2: degrade mode cannot run the gate, so accept
        # (in_human_review → done) is blocked — it would fake the
        # cross-lineage requirement.
        if transition_name == "accept":
            raise ValueError(
                f"Cannot accept {identifier!r} to 'done' in degrade mode: "
                "completion requires regista's cross-lineage review gate. "
                "For admin/repair use 'agent-notes work-item close --force' "
                "or 'work-item update --status done --force'."
            )

        if old["status"] == target_status:
            raise ValueError(
                f"{identifier!r} is already in {target_status!r}; "
                f"'{transition_name}' would be a no-op."
            )

        _lifecycle_transition_for(old["status"], target_status)

        # Write the set_status op with review_note in the payload so the
        # op_id is unique (avoids collision on revisited statuses).
        status_payload = {
            "status": target_status,
            "review_transition": transition_name,
            "review_note": review_note,
        }
        status_op = kernel.commit_op(
            conn,
            entity_id=entity_id,
            entity_type=ENTITY_TYPE,
            op_type="set_status",
            payload=status_payload,
            parent_op_ids=[entity_id],
            actor_id=actor_id,
        )
        kernel.emit_event(
            conn,
            op_id=status_op["op_id"],
            event_type="item.status_changed",
            payload={
                "entity_id": entity_id,
                "identifier": identifier,
                "old_status": old["status"],
                "new_status": target_status,
                "review_transition": transition_name,
            },
        )

        # Stamp the review note into diagnostic_keys.
        old_diag = old.get("diagnostic_keys") or {}
        if not isinstance(old_diag, dict):
            old_diag = {}
        review_notes = list(old_diag.get("review_notes") or [])
        review_notes.append(
            {
                "transition": transition_name,
                "note": review_note,
                "actor_id": actor_id,
                "model_lineage": model_lineage,
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )
        merged_diag = {**old_diag, "review_notes": review_notes}
        kernel.commit_op(
            conn,
            entity_id=entity_id,
            entity_type=ENTITY_TYPE,
            op_type="set_field",
            payload={"diagnostic_keys": merged_diag},
            parent_op_ids=[entity_id],
            actor_id=actor_id,
        )

        # Fold into cache.
        folded = kernel.fold_work_item(conn, entity_id)
        if folded is None:
            raise RuntimeError("fold_work_item returned None after review transition")

        write_change(
            conn,
            kind=KIND,
            workspace_id=workspace_id,
            project_id=project_id,
            identifier=identifier,
            event="status_changed",
            payload={
                "old_status": old["status"],
                "new_status": target_status,
                "review_transition": transition_name,
                "review_note": review_note,
            },
            actor=actor_id,
        )

        conn.commit()
        return dict(folded)


def claim_work_item(
    project_id: int,
    identifier: str,
    actor_id: str | None,
    ttl_seconds: int,
    model_lineage: str | None = None,
) -> dict:
    # WI-068: the lease verbs write ops (claim + set_status) with the caller's
    # actor_id, so they are agent-authored writes like any other — gate them
    # exactly as the regista lease path does (3d2552e gated that side only).
    face_factory.assert_declared_lineage(actor_id, model_lineage, operation="work-item claim")
    with _conn() as conn:
        workspace_id = _common.resolve_workspace_for_project(conn, project_id)
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT * FROM work_items WHERE project_id = %s AND identifier = %s",
            (project_id, identifier),
        )
        old = cur.fetchone()
        if old is None:
            raise ValueError(f"Work item not found: {identifier!r} in project {project_id}")

        entity_id = old["entity_id"]
        status = old["status"]

        # Only claimable items can be claimed
        if status != "open":
            raise ValueError(f"Cannot claim: status is {status!r} (must be 'open')")

        # Check if already leased
        cur.execute(
            "SELECT 1 FROM work_item_leases WHERE entity_id = %s AND expires_at > now()",
            (entity_id,),
        )
        if cur.fetchone() is not None:
            raise ValueError(f"Work item {identifier!r} is already claimed")

        # Write claim op
        op = kernel.commit_op(
            conn,
            entity_id=entity_id,
            entity_type=ENTITY_TYPE,
            op_type="claim",
            payload={"actor_id": actor_id, "ttl_seconds": ttl_seconds},
            parent_op_ids=[entity_id],
            actor_id=actor_id,
        )

        cur.execute(
            """
            INSERT INTO work_item_leases (entity_id, actor_id, expires_at)
            VALUES (%s, %s, now() + make_interval(secs => %s))
            ON CONFLICT (entity_id) DO UPDATE SET
                actor_id = EXCLUDED.actor_id,
                acquired_at = now(),
                expires_at = EXCLUDED.expires_at,
                heartbeat_count = work_item_leases.heartbeat_count + 1
            """,
            (entity_id, actor_id or "unknown", ttl_seconds),
        )

        # Write a set_status op to move status to 'claimed'
        kernel.commit_op(
            conn,
            entity_id=entity_id,
            entity_type=ENTITY_TYPE,
            op_type="set_status",
            payload={"status": "claimed"},
            parent_op_ids=[op["op_id"]],
            actor_id=actor_id,
        )

        kernel.emit_event(
            conn,
            op_id=op["op_id"],
            event_type="claim.granted",
            payload={"entity_id": entity_id, "identifier": identifier, "actor_id": actor_id},
        )

        # Fold into cache
        folded = kernel.fold_work_item(conn, entity_id)
        if folded is None:
            raise RuntimeError("fold_work_item returned None after claim op")

        # Audit trail (BC-027): mirror the regista path's change_log row.
        write_change(
            conn,
            kind=KIND,
            workspace_id=workspace_id,
            project_id=project_id,
            identifier=identifier,
            event="claimed",
            payload={"actor_id": actor_id, "ttl_seconds": ttl_seconds},
            actor=actor_id,
        )

        conn.commit()
        return dict(folded)


def release_work_item(
    project_id: int,
    identifier: str,
    actor_id: str | None,
    model_lineage: str | None = None,
) -> dict:
    # WI-068: same gate as claim — release writes a release op + set_status.
    face_factory.assert_declared_lineage(actor_id, model_lineage, operation="work-item release")
    with _conn() as conn:
        workspace_id = _common.resolve_workspace_for_project(conn, project_id)
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT * FROM work_items WHERE project_id = %s AND identifier = %s",
            (project_id, identifier),
        )
        old = cur.fetchone()
        if old is None:
            raise ValueError(f"Work item not found: {identifier!r} in project {project_id}")

        entity_id = old["entity_id"]

        # Write release op
        op = kernel.commit_op(
            conn,
            entity_id=entity_id,
            entity_type=ENTITY_TYPE,
            op_type="release",
            payload={"actor_id": actor_id},
            parent_op_ids=[entity_id],
            actor_id=actor_id,
        )

        # Delete lease
        cur.execute(
            "DELETE FROM work_item_leases WHERE entity_id = %s",
            (entity_id,),
        )

        kernel.commit_op(
            conn,
            entity_id=entity_id,
            entity_type=ENTITY_TYPE,
            op_type="set_status",
            payload={"status": "open"},
            parent_op_ids=[op["op_id"]],
            actor_id=actor_id,
        )

        kernel.emit_event(
            conn,
            op_id=op["op_id"],
            event_type="claim.released",
            payload={"entity_id": entity_id, "identifier": identifier, "actor_id": actor_id},
        )

        folded = kernel.fold_work_item(conn, entity_id)
        if folded is None:
            raise RuntimeError("fold_work_item returned None after release op")

        # Audit trail (BC-027): mirror the regista path's change_log row.
        write_change(
            conn,
            kind=KIND,
            workspace_id=workspace_id,
            project_id=project_id,
            identifier=identifier,
            event="released",
            payload={"actor_id": actor_id},
            actor=actor_id,
        )

        conn.commit()
        return dict(folded)


def heartbeat_work_item(
    project_id: int,
    identifier: str,
    actor_id: str | None,
    ttl_seconds: int,
    model_lineage: str | None = None,
) -> dict:
    # WI-068: same gate as claim — heartbeat writes a heartbeat op.
    face_factory.assert_declared_lineage(actor_id, model_lineage, operation="work-item heartbeat")
    with _conn() as conn:
        workspace_id = _common.resolve_workspace_for_project(conn, project_id)
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT * FROM work_items WHERE project_id = %s AND identifier = %s",
            (project_id, identifier),
        )
        old = cur.fetchone()
        if old is None:
            raise ValueError(f"Work item not found: {identifier!r} in project {project_id}")

        entity_id = old["entity_id"]

        cur.execute(
            """
            UPDATE work_item_leases
            SET expires_at = now() + make_interval(secs => %s),
                heartbeat_count = heartbeat_count + 1
            WHERE entity_id = %s
            RETURNING *
            """,
            (ttl_seconds, entity_id),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"Work item {identifier!r} is not claimed — cannot heartbeat")

        op = kernel.commit_op(
            conn,
            entity_id=entity_id,
            entity_type=ENTITY_TYPE,
            op_type="heartbeat",
            payload={"actor_id": actor_id, "ttl_seconds": ttl_seconds},
            parent_op_ids=[entity_id],
            actor_id=actor_id,
        )

        kernel.emit_event(
            conn,
            op_id=op["op_id"],
            event_type="item.heartbeat",
            payload={"entity_id": entity_id, "identifier": identifier, "actor_id": actor_id},
        )

        # Audit trail (BC-027): record the lease heartbeat in change_log so the
        # degrade-mode lease lifecycle is visible in the sole audit source
        # (decision 7), matching the claim/release rows above.
        write_change(
            conn,
            kind=KIND,
            workspace_id=workspace_id,
            project_id=project_id,
            identifier=identifier,
            event="heartbeat",
            payload={"actor_id": actor_id, "ttl_seconds": ttl_seconds},
            actor=actor_id,
        )

        conn.commit()
        return dict(row)


def delete_work_item(
    project_id: int,
    identifier: str,
    actor_id: str | None = None,
    model_lineage: str | None = None,
) -> bool:
    """Native soft-delete via snapshot op with tombstone. Removes from cache."""
    # WI-068: the tombstone snapshot is an authored op like any other — gate it,
    # and (below) stamp the actor on the op instead of committing it anonymous.
    face_factory.assert_declared_lineage(actor_id, model_lineage, operation="work-item delete")
    with _conn() as conn:
        workspace_id = _common.resolve_workspace_for_project(conn, project_id)
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT * FROM work_items WHERE project_id = %s AND identifier = %s",
            (project_id, identifier),
        )
        old = cur.fetchone()
        if old is None:
            return False

        entity_id = old["entity_id"]
        op = kernel.commit_op(
            conn,
            entity_id=entity_id,
            entity_type=ENTITY_TYPE,
            op_type="snapshot",
            payload={"tombstone": True},
            parent_op_ids=[entity_id],
            actor_id=actor_id,
        )

        # Delete leases first to avoid FK violation (BC-028).
        cur.execute(
            "DELETE FROM work_item_leases WHERE entity_id = %s",
            (entity_id,),
        )

        # Delete from cache.
        cur.execute(
            "DELETE FROM work_items WHERE project_id = %s AND identifier = %s",
            (project_id, identifier),
        )

        kernel.emit_event(
            conn,
            op_id=op["op_id"],
            event_type="item.deleted",
            payload={"entity_id": entity_id, "identifier": identifier},
        )

        write_change(
            conn,
            kind=KIND,
            workspace_id=workspace_id,
            project_id=project_id,
            identifier=identifier,
            event="deleted",
            payload={"title": old["title"]},
            actor=actor_id,
        )

        conn.commit()
        return True
