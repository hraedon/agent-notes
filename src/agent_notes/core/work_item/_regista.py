"""Regista-face write path for work items.

The converged store (regista) is the source of truth when a face is
attached (Plan 009 / Plan 010). These functions drive regista and mirror
the result into the local ``work_items`` projection. Each mirrors the
behavior of the original ``WorkItemModel._*_regista`` staticmethods.
"""

from __future__ import annotations

from typing import Any

import psycopg

from agent_notes.core import face_factory, kernel, projection
from agent_notes.core.change_log import write_change
from agent_notes.core.db import _conn
from agent_notes.core.lifecycle import transition_for as _lifecycle_transition_for
from agent_notes.core.regista_face import normalize_source_identifier

from . import _common

KIND = "work_item"
ENTITY_TYPE = "work_item"


def file_work_item(
    face: Any,
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
    actor_id: str | None = None,
    model_lineage: str | None = None,
) -> dict:
    with _conn() as conn:
        workspace_id = _common.resolve_workspace_for_project(conn, project_id)
        _common.validate_vocab(conn, workspace_id, "wi_kind", kind)
        _common.validate_vocab(conn, workspace_id, "wi_status", status)
        _common.validate_vocab(conn, workspace_id, "wi_severity", severity)

        if identifier is None:
            cur = conn.cursor(row_factory=psycopg.rows.tuple_row)
            cur.execute("SELECT allocate_work_item_identifier(%s)", (project_id,))
            identifier = cur.fetchone()[0]

    effective_title = title or identifier
    actor = face_factory.actor_with_overrides(
        actor_id, model_lineage, operation="work-item file"
    )
    norm_sid = normalize_source_identifier(identifier)

    # Idempotency guard (Plan 015): regista is the SoT, but the create-vs-update
    # decision in callers (e.g. bc_files.sync_breadcrumbs_from_dir) is made
    # against the *local* projection, which can be empty/stale relative to the
    # remote store (fresh session, reset local DB, per-project routing). When
    # it is, the caller routes a re-import here as a "create" even though the
    # breadcrumb already exists in regista — the original duplication bug. Look
    # the item up by normalized source_identifier first; if it exists, amend
    # fields in place rather than minting a duplicate. Status transitions are
    # intentionally NOT re-driven on this path (the live lifecycle state wins;
    # forcing a transition could violate the review gate).
    existing = face.find_by_source_identifier(norm_sid) if norm_sid is not None else None
    if existing is not None:
        wid = existing.work_item_id
        live_state = existing.current_state or ""
        if live_state in ("done", "closed"):
            state = live_state  # terminal — skip amend (cannot amend terminal state)
        else:
            state = face.amend_breadcrumb(
                actor,
                wid,
                current_state=live_state,
                title=effective_title,
                description=body or "",
                severity=severity,
                kind=kind,
                external_refs=external_refs or {},
                diagnostic_keys=diagnostic_keys or {},
            )
    else:
        wid, state = face.create_breadcrumb(
            actor,
            title=effective_title,
            description=body or "",
            severity=severity,
            kind=kind,
            external_refs=external_refs or {},
            diagnostic_keys=diagnostic_keys or {},
            source_identifier=norm_sid,
        )
        if status != "open":
            close_transition = _lifecycle_transition_for("open", status)
            if close_transition is not None:
                state = face.transition_breadcrumb(actor, wid, close_transition)
    entity_id = _common.entity_id_for_regista_create(identifier, wid)
    custom_fields = {
        "title": effective_title,
        "description": body or "",
        "severity": severity,
        "kind": kind,
        "external_refs": external_refs or {},
        "diagnostic_keys": diagnostic_keys or {},
        "source_identifier": norm_sid,
    }
    with _conn() as conn:
        mirrored = projection.mirror_from_regista(
            conn,
            project_id=project_id,
            identifier=identifier,
            entity_id=entity_id,
            regista_work_item_id=wid,
            state=state,
            custom_fields=custom_fields,
            embed=(lambda _text: embedding) if embedding is not None else None,
            actor_id=actor.actor_id,
        )
        write_change(
            conn,
            kind=KIND,
            workspace_id=workspace_id,
            project_id=project_id,
            identifier=identifier,
            event="filed",
            payload={"title": effective_title, "kind": kind, "status": mirrored["status"]},
            actor=actor.actor_id,
        )
        conn.commit()
    return dict(mirrored)


def update_work_item(
    face: Any,
    project_id: int,
    identifier: str,
    title: str | None,
    body: str | None,
    kind: str | None,
    status: str | None,
    severity: str | None,
    external_refs: dict | None,
    diagnostic_keys: dict | None,
    embedding: Any | None,
    actor_id: str | None = None,
    model_lineage: str | None = None,
) -> dict:
    with _conn() as conn:
        workspace_id = _common.resolve_workspace_for_project(conn, project_id)
        old = _common.load_work_item_row(conn, project_id, identifier)
        if old.get("regista_work_item_id") is None:
            raise ValueError(
                f"Work item {identifier!r} has no regista mapping; run migrate-to-regista first"
            )
        if kind is not None:
            _common.validate_vocab(conn, workspace_id, "wi_kind", kind)
        if status is not None:
            _common.validate_vocab(conn, workspace_id, "wi_status", status)
        if severity is not None:
            _common.validate_vocab(conn, workspace_id, "wi_severity", severity)
        old_body = kernel.get_blob(conn, old["body_hash"]) or ""
        wid = old["regista_work_item_id"]

    actor = face_factory.actor_with_overrides(
        actor_id, model_lineage, operation="work-item update"
    )
    custom_fields: dict[str, Any] = {}
    if title is not None:
        custom_fields["title"] = title or identifier
    if body is not None:
        custom_fields["description"] = body
    if kind is not None:
        custom_fields["kind"] = kind
    if severity is not None:
        custom_fields["severity"] = severity
    if external_refs is not None:
        custom_fields["external_refs"] = external_refs
    if diagnostic_keys is not None:
        custom_fields["diagnostic_keys"] = diagnostic_keys

    old_status = old["status"]
    new_status = status if status is not None else old_status
    transition_name = _lifecycle_transition_for(old_status, new_status)
    if transition_name is not None:
        face.transition_breadcrumb(
            actor,
            wid,
            transition_name,
            custom_fields=custom_fields or None,
        )
    else:
        face.amend_breadcrumb(
            actor,
            wid,
            current_state=old_status,
            title=custom_fields.get("title"),
            description=custom_fields.get("description"),
            severity=custom_fields.get("severity"),
            kind=custom_fields.get("kind"),
            external_refs=custom_fields.get("external_refs"),
            diagnostic_keys=custom_fields.get("diagnostic_keys"),
        )

    regista_work_item = face.get(wid)
    with _conn() as conn:
        mirrored = _common.mirror_regista_snapshot(
            conn,
            old,
            regista_work_item,
            embed=embedding,
            actor_id=actor.actor_id,
        )
        new_body = kernel.get_blob(conn, mirrored["body_hash"]) or ""
        payload = _common.update_change_log_payload(
            old, old_body, mirrored, new_body, external_refs, diagnostic_keys
        )
        if payload:
            write_change(
                conn,
                kind=KIND,
                workspace_id=workspace_id,
                project_id=project_id,
                identifier=identifier,
                event="updated",
                payload=payload,
                actor=actor.actor_id,
            )
        conn.commit()
    return dict(mirrored)


def close_work_item(
    face: Any,
    project_id: int,
    identifier: str,
    actor_id: str | None = None,
    model_lineage: str | None = None,
) -> dict:
    with _conn() as conn:
        workspace_id = _common.resolve_workspace_for_project(conn, project_id)
        old = _common.load_work_item_row(conn, project_id, identifier)
        if old.get("regista_work_item_id") is None:
            raise ValueError(
                f"Work item {identifier!r} has no regista mapping; run migrate-to-regista first"
            )
        wid = old["regista_work_item_id"]
        state = old["status"]

    actor = face_factory.actor_with_overrides(
        actor_id, model_lineage, operation="work-item close"
    )
    # Plan 010 WI-3: close → submit_for_review → in_review. The agent cannot
    # reach `done` unilaterally (Invariant G); work awaits a cross-lineage
    # review pass + accept. `closed` (legacy terminal) is treated as `done`.
    canonical = "done" if state == "closed" else state
    if canonical in ("in_review", "in_human_review"):
        pass  # already awaiting review — no-op
    elif canonical == "done":
        raise ValueError(f"Work item {identifier!r} is already done; reopen first")
    elif canonical == "blocked":
        raise ValueError(f"Work item {identifier!r} is blocked; unblock before closing")
    elif canonical == "deferred":
        raise ValueError(f"Work item {identifier!r} is deferred; resume before closing")
    elif canonical == "claimed":
        raise ValueError(
            f"Work item {identifier!r} is in legacy 'claimed' state; "
            "migrate to the canonical workflow first"
        )
    else:  # open or in_progress
        if canonical == "open":
            face.transition_breadcrumb(actor, wid, "start")
        face.transition_breadcrumb(actor, wid, "submit_for_review")
    regista_work_item = face.get(wid)
    with _conn() as conn:
        mirrored = _common.mirror_regista_snapshot(
            conn,
            old,
            regista_work_item,
            actor_id=actor.actor_id,
        )
        write_change(
            conn,
            kind=KIND,
            workspace_id=workspace_id,
            project_id=project_id,
            identifier=identifier,
            event="status_changed",
            payload={"old_status": old["status"], "new_status": mirrored["status"]},
            actor=actor.actor_id,
        )
        conn.commit()
    return dict(mirrored)


_REVIEW_TRANSITION_TO_STATUS: dict[str, str] = {
    "adversarial_pass": "in_human_review",
    "accept": "done",
    "reject": "in_progress",
    "request_changes": "in_progress",
}


def review_transition(
    face: Any,
    project_id: int,
    identifier: str,
    transition_name: str,
    review_note: str,
    actor_id: str | None,
    model_lineage: str | None,
    same_lineage_acknowledged: bool,
) -> dict:
    with _conn() as conn:
        workspace_id = _common.resolve_workspace_for_project(conn, project_id)
        old = _common.load_work_item_row(conn, project_id, identifier)
        if old.get("regista_work_item_id") is None:
            raise ValueError(
                f"Work item {identifier!r} has no regista mapping; run migrate-to-regista first"
            )
        wid = old["regista_work_item_id"]

    actor = face_factory.actor_with_overrides(
        actor_id,
        model_lineage,
        clear_principal=True,
        operation=f"work-item review {transition_name}",
    )
    payload: dict[str, Any] = {"review_note": review_note}
    if same_lineage_acknowledged:
        payload["same_lineage_acknowledged"] = True

    face.transition_breadcrumb(
        actor,
        wid,
        transition_name,
        payload=payload,
    )

    regista_work_item = face.get(wid)
    with _conn() as conn:
        mirrored = _common.mirror_regista_snapshot(
            conn,
            old,
            regista_work_item,
            actor_id=actor.actor_id,
        )
        write_change(
            conn,
            kind=KIND,
            workspace_id=workspace_id,
            project_id=project_id,
            identifier=identifier,
            event="status_changed",
            payload={
                "old_status": old["status"],
                "new_status": mirrored["status"],
                "review_transition": transition_name,
                "review_note": review_note,
            },
            actor=actor.actor_id,
        )
        conn.commit()
    return dict(mirrored)


def claim_work_item(face: Any, project_id: int, identifier: str, ttl_seconds: int) -> dict:
    with _conn() as conn:
        _common.resolve_workspace_for_project(conn, project_id)
        old = _common.load_work_item_row(conn, project_id, identifier)
        if old.get("regista_work_item_id") is None:
            raise ValueError(
                f"Work item {identifier!r} has no regista mapping; run migrate-to-regista first"
            )
        if old["status"] == "done":
            raise ValueError("Cannot claim: work item is done (terminal)")
        wid = old["regista_work_item_id"]
        entity_id = old["entity_id"]

    actor = face_factory.actor_with_overrides(operation="work-item claim")
    # Plan 010 WI-2: lease is a regista claim, NOT a lifecycle state.
    # acquire_claim is the authoritative lease; the lifecycle does not move.
    claim = face.acquire_claim(actor, wid, ttl_seconds=ttl_seconds)
    regista_work_item = face.get(wid)
    with _conn() as conn:
        mirrored = _common.mirror_regista_snapshot(
            conn,
            old,
            regista_work_item,
            actor_id=actor.actor_id,
        )
        # Mirror the claim into the local lease projection row.
        expires_at = getattr(claim, "expires_at", None)
        cur = conn.cursor(row_factory=psycopg.rows.dict_row)
        cur.execute(
            """
            INSERT INTO work_item_leases (entity_id, actor_id, expires_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (entity_id) DO UPDATE SET
                actor_id = EXCLUDED.actor_id,
                acquired_at = now(),
                expires_at = EXCLUDED.expires_at,
                heartbeat_count = work_item_leases.heartbeat_count + 1
            """,
            (entity_id, actor.actor_id, expires_at),
        )
        write_change(
            conn,
            kind=KIND,
            workspace_id=_common.resolve_workspace_for_project(conn, project_id),
            project_id=project_id,
            identifier=identifier,
            event="claimed",
            payload={"actor_id": actor.actor_id, "ttl_seconds": ttl_seconds},
            actor=actor.actor_id,
        )
        conn.commit()
    return dict(mirrored)


def release_work_item(face: Any, project_id: int, identifier: str) -> dict:
    with _conn() as conn:
        _common.resolve_workspace_for_project(conn, project_id)
        old = _common.load_work_item_row(conn, project_id, identifier)
        if old.get("regista_work_item_id") is None:
            raise ValueError(
                f"Work item {identifier!r} has no regista mapping; run migrate-to-regista first"
            )
        wid = old["regista_work_item_id"]
        entity_id = old["entity_id"]

    actor = face_factory.actor_with_overrides(operation="work-item release")
    # Plan 010 WI-2: release the regista claim; the lifecycle is untouched.
    face.release_claim(actor, wid)
    regista_work_item = face.get(wid)
    with _conn() as conn:
        mirrored = _common.mirror_regista_snapshot(
            conn,
            old,
            regista_work_item,
            actor_id=actor.actor_id,
        )
        cur = conn.cursor(row_factory=psycopg.rows.dict_row)
        cur.execute(
            "DELETE FROM work_item_leases WHERE entity_id = %s",
            (entity_id,),
        )
        write_change(
            conn,
            kind=KIND,
            workspace_id=_common.resolve_workspace_for_project(conn, project_id),
            project_id=project_id,
            identifier=identifier,
            event="released",
            payload={"actor_id": actor.actor_id},
            actor=actor.actor_id,
        )
        conn.commit()
    return dict(mirrored)


def heartbeat_work_item(face: Any, project_id: int, identifier: str, ttl_seconds: int) -> dict:
    with _conn() as conn:
        old = _common.load_work_item_row(conn, project_id, identifier)
        entity_id = old["entity_id"]
        wid = old["regista_work_item_id"]

    actor = face_factory.actor_with_overrides(operation="work-item heartbeat")
    # Plan 010 WI-2: authoritative liveness is the regista claim heartbeat.
    claim = face.heartbeat_claim(actor, wid, ttl_seconds=ttl_seconds)
    expires_at = getattr(claim, "expires_at", None)
    with _conn() as conn:
        cur = conn.cursor(row_factory=psycopg.rows.dict_row)
        cur.execute(
            """
            UPDATE work_item_leases
            SET expires_at = %s,
                heartbeat_count = heartbeat_count + 1
            WHERE entity_id = %s
            RETURNING *
            """,
            (expires_at, entity_id),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"Work item {identifier!r} is not claimed — cannot heartbeat")
        conn.commit()
    return dict(row)
