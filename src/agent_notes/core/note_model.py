"""Note entity write-through path (Plan 018 WI-1.2).

When the regista face is attached (``AGENT_NOTES_REGISTA_WRITES=1``), memories
and reflections are written as signed regista entities (``entity_kind="note"``)
before being mirrored into the local ``memories`` projection table. The local
table holds the pgvector embedding (a projection, not the record) and the
``regista_note_id`` column tracks the regista entity_id.

This mirrors the breadcrumb write-through pattern (``work_item/_regista.py``):
regista is the authority; the local table is a search/read projection. The
reconcile path covers offline-append as it does for breadcrumbs.

See ``docs/note-entity-contract.md`` for the entity shape and the
breadcrumb-vs-memory/reflection split decision.

Split-brain contract (Profile A/B remediation)
----------------------------------------------
The signed regista event is committed *before* the local projection write.
If the local write then fails, the note exists in regista but not in the
local ``memories`` table. ``NoteProjectionError`` makes this state explicit:
it carries the ``regista_note_id`` so the caller can report the partial
commit and point the operator at ``rebuild_from_regista`` for recovery.
The error is never swallowed into a clean success — the CLI maps it to a
``PROJECTION_FAILED`` envelope with a nonzero exit code.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_notes.core import face_factory
from agent_notes.core.change_log import write_change
from agent_notes.core.db import _conn

_log = logging.getLogger(__name__)


class NoteProjectionError(Exception):
    """The regista event committed but the local projection write failed.

    The note *is* in regista (the signed event is durable); the local
    ``memories`` row is missing or stale. Recovery: call
    ``rebuild_from_regista(face, project_id=...)`` to re-derive the
    projection from the authoritative event log.

    Attributes:
        regista_note_id: The entity_id of the committed note event.
        operation: Which write operation failed ('add', 'update', 'delete').
    """

    def __init__(
        self,
        message: str,
        *,
        regista_note_id: uuid.UUID,
        operation: str,
    ) -> None:
        super().__init__(message)
        self.regista_note_id = regista_note_id
        self.operation = operation


KIND = "memory"

_NOTE_FILED = "note_filed"
_NOTE_UPDATED = "note_updated"
_NOTE_SUPERSEDED = "note_superseded"
_NOTE_DELETED = "note_deleted"


def _resolve_workspace(conn: psycopg.Connection, project_id: int) -> int:
    cur = conn.cursor()
    cur.execute("SELECT workspace_id FROM projects WHERE id = %s", (project_id,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"Project {project_id} not found")
    return row["workspace_id"]


def _find_existing_note(conn: psycopg.Connection, project_id: int, name: str) -> dict | None:
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        "SELECT id, regista_note_id, memory_type, body, attributes, active "
        "FROM memories WHERE project_id = %s AND name = %s AND active = true",
        (project_id, name),
    )
    return cur.fetchone()


def _mirror_note_to_projection(
    conn: psycopg.Connection,
    *,
    workspace_id: int,
    project_id: int,
    name: str,
    memory_type: str,
    body: str,
    attributes: dict | None,
    embedding: Any | None,
    regista_note_id: uuid.UUID | None,
    active: bool = True,
    supersedes: int | None = None,
    pending_sync: bool = False,
) -> dict:
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        INSERT INTO memories
            (workspace_id, project_id, name, memory_type, body, embedding,
             active, supersedes, attributes, regista_note_id, pending_sync)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id, workspace_id, project_id, name, memory_type, body,
                  active, supersedes, attributes, regista_note_id, pending_sync,
                  created_at, updated_at
        """,
        (
            workspace_id,
            project_id,
            name,
            memory_type,
            body,
            embedding,
            active,
            supersedes,
            psycopg.types.json.Jsonb(attributes or {}),
            regista_note_id,
            pending_sync,
        ),
    )
    return dict(cur.fetchone())


def _supersede_old(conn: psycopg.Connection, old_id: int) -> None:
    conn.execute("UPDATE memories SET active = false WHERE id = %s", (old_id,))


def _note_subtype_for(memory_type: str) -> str:
    """Map a local memory_type to a note entity subtype.

    ``reflection`` maps to ``reflection``; all other memory types (note,
    decision, feedback, project, reference, user) map to ``memory`` — they
    are knowledge notes without a lifecycle, distinguished from reflections
    only by subtype.
    """
    return "reflection" if memory_type == "reflection" else "memory"


def add_memory(
    workspace_id: int,
    project_id: int,
    name: str,
    memory_type: str,
    body: str,
    attributes: dict | None = None,
    embedding: Any | None = None,
) -> dict:
    """Write a memory as a signed note entity, then mirror to local projection.

    Split-brain contract: the regista event commits first. If the local
    projection write then fails, ``NoteProjectionError`` is raised carrying
    the ``regista_note_id`` — the note IS in regista and recoverable via
    ``rebuild_from_regista``. The error is never reported as a clean success.
    """
    from agent_notes.core.memory_model import _auto_create_wikilinks

    actor = face_factory.default_actor()
    note_uuid = uuid.uuid4()
    note_subtype = _note_subtype_for(memory_type)

    payload: dict[str, Any] = {
        "note_subtype": note_subtype,
        "memory_type": memory_type,
        "name": name,
        "body": body,
        "attributes": attributes or {},
        "links": [],
    }

    face = face_factory.get_face()
    note_outboxed = False
    if face is not None:
        face.append_note(
            actor,
            note_uuid,
            transition=_NOTE_FILED,
            payload=payload,
        )
        note_outboxed = getattr(face, "last_op_outboxed", False)

    # The regista event is now committed (unless outboxed). The local
    # projection write below is best-effort relative to regista: if it
    # fails, the note still exists in the authoritative store. We catch
    # the failure and raise NoteProjectionError so the caller can report
    # the partial commit and point the operator at rebuild_from_regista.
    #
    # Track the supersede append outcome separately: if the old note's
    # note_superseded event committed but the local projection then fails,
    # both entity_ids are involved in the split-brain.
    supersede_committed_id: uuid.UUID | None = None
    try:
        with _conn() as conn:
            ws_id = _resolve_workspace(conn, project_id)
            existing = _find_existing_note(conn, project_id, name)
            old_id = None
            if existing:
                old_id = existing["id"]
                _supersede_old(conn, old_id)
                if face is not None and existing.get("regista_note_id") is not None:
                    face.append_note(
                        actor,
                        existing["regista_note_id"],
                        transition=_NOTE_SUPERSEDED,
                        payload={"superseded_by": str(note_uuid)},
                    )
                    if getattr(face, "last_op_outboxed", False):
                        _mark_note_pending(conn, existing["regista_note_id"], True)
                    else:
                        supersede_committed_id = uuid.UUID(str(existing["regista_note_id"]))

            row = _mirror_note_to_projection(
                conn,
                workspace_id=ws_id,
                project_id=project_id,
                name=name,
                memory_type=memory_type,
                body=body,
                attributes=attributes,
                embedding=embedding,
                regista_note_id=note_uuid if face is not None else None,
                supersedes=old_id,
                pending_sync=note_outboxed,
            )
            _auto_create_wikilinks(conn, ws_id, project_id, name, body)
            write_change(
                conn,
                kind=KIND,
                workspace_id=ws_id,
                project_id=project_id,
                identifier=name,
                event="filed",
                payload={"memory_type": memory_type, "id": row["id"]},
            )
            conn.commit()
    except NoteProjectionError:
        raise
    except Exception as exc:
        if face is not None and not note_outboxed:
            involved = [f"entity_id={note_uuid}"]
            if supersede_committed_id is not None:
                involved.append(f"superseded_entity_id={supersede_committed_id}")
            raise NoteProjectionError(
                f"regista note event committed ({', '.join(involved)}) but local "
                f"projection write failed: {exc}. Recover with "
                f"'agent-notes memory rebuild-from-regista --project <slug>'.",
                regista_note_id=note_uuid,
                operation="add",
            ) from exc
        raise

    return row


def _mark_note_pending(conn: psycopg.Connection, regista_note_id: Any, pending: bool) -> None:
    conn.execute(
        "UPDATE memories SET pending_sync = %s WHERE regista_note_id = %s",
        (pending, regista_note_id),
    )


def update_memory(
    workspace_id: int,
    project_id: int,
    name: str,
    body: str | None = None,
    attributes: dict | None = None,
) -> dict:
    """Update a memory: append a note_updated event, then mirror to projection.

    Split-brain contract: same as ``add_memory`` — the regista event commits
    first; a local projection failure raises ``NoteProjectionError``.
    """
    face = face_factory.get_face()
    actor = face_factory.default_actor()

    # Read the existing row before touching regista so we can raise a clean
    # ValueError (not a NoteProjectionError) when the memory simply doesn't exist.
    with _conn() as conn:
        existing = _find_existing_note(conn, project_id, name)
    if existing is None:
        raise ValueError(f"Memory '{name}' not found (or deleted)")

    note_id = existing.get("regista_note_id")
    regista_committed = False
    if face is not None and note_id is not None:
        payload: dict[str, Any] = {
            "note_subtype": _note_subtype_for(existing["memory_type"]),
            "memory_type": existing["memory_type"],
            "name": name,
        }
        if body is not None:
            payload["body"] = body
        if attributes is not None:
            merged_attrs = dict(existing.get("attributes") or {})
            merged_attrs.update(attributes)
            payload["attributes"] = merged_attrs
        face.append_note(
            actor,
            note_id,
            transition=_NOTE_UPDATED,
            payload=payload,
        )
        regista_committed = not getattr(face, "last_op_outboxed", False)

    try:
        with _conn() as conn:
            ws_id = _resolve_workspace(conn, project_id)
            merged_attrs = dict(existing.get("attributes") or {})
            if attributes is not None:
                merged_attrs.update(attributes)

            cur = conn.cursor(row_factory=dict_row)
            sets = ["updated_at = now()"]
            params: list[Any] = []
            if body is not None:
                sets.append("body = %s")
                params.append(body)
            if attributes is not None:
                sets.append("attributes = %s")
                params.append(psycopg.types.json.Jsonb(merged_attrs))
            params.extend([project_id, workspace_id, name])
            cur.execute(
                f"UPDATE memories SET {', '.join(sets)} "
                "WHERE project_id = %s AND workspace_id = %s AND name = %s AND active = true "
                "RETURNING id, workspace_id, project_id, name, memory_type, body, "
                "active, supersedes, attributes, regista_note_id, created_at, updated_at",
                params,
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"Memory '{name}' not found (or deleted)")

            write_change(
                conn,
                kind=KIND,
                workspace_id=ws_id,
                project_id=project_id,
                identifier=name,
                event="updated",
                payload={
                    "fields": [k for k in ("body", "attributes") if locals().get(k) is not None]
                },
            )
            conn.commit()
    except NoteProjectionError:
        raise
    except ValueError:
        raise
    except Exception as exc:
        if regista_committed and note_id is not None:
            raise NoteProjectionError(
                f"regista note_updated event committed (entity_id={note_id}) but "
                f"local projection write failed: {exc}. Recover with "
                f"'agent-notes memory rebuild-from-regista --project <slug>'.",
                regista_note_id=uuid.UUID(str(note_id)),
                operation="update",
            ) from exc
        raise

    return dict(row)


def delete_memory(
    workspace_id: int,
    project_id: int,
    name: str,
) -> dict | None:
    """Soft-delete a memory: append a note_deleted event, then flip active=false.

    Split-brain contract: same as ``add_memory`` — the regista event commits
    first; a local projection failure raises ``NoteProjectionError``.
    """
    face = face_factory.get_face()
    actor = face_factory.default_actor()

    # Read the existing row before touching regista so a missing memory is a
    # clean None return, not a NoteProjectionError.
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT id, regista_note_id FROM memories "
            "WHERE project_id = %s AND workspace_id = %s AND name = %s AND active = true",
            (project_id, workspace_id, name),
        )
        existing = cur.fetchone()
    if existing is None:
        return None

    note_id = existing.get("regista_note_id")
    regista_committed = False
    if face is not None and note_id is not None:
        face.append_note(
            actor,
            note_id,
            transition=_NOTE_DELETED,
            payload={},
        )
        regista_committed = not getattr(face, "last_op_outboxed", False)

    try:
        with _conn() as conn:
            ws_id = _resolve_workspace(conn, project_id)
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "UPDATE memories SET active = false WHERE id = %s RETURNING id",
                (existing["id"],),
            )
            row = cur.fetchone()

            write_change(
                conn,
                kind=KIND,
                workspace_id=ws_id,
                project_id=project_id,
                identifier=name,
                event="deleted",
                payload={"id": row["id"]},
            )
            conn.commit()
            return dict(row)
    except NoteProjectionError:
        raise
    except Exception as exc:
        if regista_committed and note_id is not None:
            raise NoteProjectionError(
                f"regista note_deleted event committed (entity_id={note_id}) but "
                f"local projection write failed: {exc}. Recover with "
                f"'agent-notes memory rebuild-from-regista --project <slug>'.",
                regista_note_id=uuid.UUID(str(note_id)),
                operation="delete",
            ) from exc
        raise


def _resolve_memory_type(folded: dict) -> str:
    """Resolve the full local memory_type from a folded note state.

    The note entity stores the full local ``memory_type`` in its payload
    (alongside the coarse ``note_subtype``). Notes written before that field
    existed fall back to the subtype, mapped back to a valid vocabulary value
    (``memory`` subtype -> ``note``; ``reflection`` -> ``reflection``).
    """
    memory_type = folded.get("memory_type")
    if memory_type:
        return memory_type
    return "reflection" if folded.get("note_subtype") == "reflection" else "note"


def rebuild_from_regista(
    face: Any,
    *,
    project_id: int,
    embed_fn: Any | None = None,
) -> dict:
    """Rebuild the local memories projection from signed note entities.

    Reads all note entities from regista, folds each event log to reconstruct
    the current state, and upserts into the local ``memories`` table. The
    pgvector index is rebuilt as part of this (embeddings are recomputed from
    the note body). The local ``memory_type`` is restored from the payload
    (round-tripping the full vocabulary, not just the coarse subtype), and the
    ``supersedes`` revision chain is reconstructed from ``note_superseded``
    events. Per-entity failures are logged at WARNING (not swallowed silently).

    Returns a summary dict with counts.
    """
    from agent_notes.core.db import _conn as _get_conn

    note_events = face.list_note_entities()
    mirrored = 0
    created = 0
    skipped = 0
    failed = 0
    # replacement entity_id -> superseded entity_id (str); inverted from each
    # note entity's `superseded_by`. After all rows are upserted we point the
    # newer row's `supersedes` at the older row's local id.
    supersede_of: dict[str, str] = {}

    for latest_evt in note_events:
        entity_id = latest_evt.effective_entity_id
        try:
            events = face.read_note_events(entity_id)
            if not events:
                skipped += 1
                continue

            folded = _fold_note_events(events)
            memory_type = _resolve_memory_type(folded)

            superseded_by = folded.get("superseded_by")
            if superseded_by:
                supersede_of[str(superseded_by)] = str(entity_id)

            with _get_conn() as conn:
                ws_id = _resolve_workspace(conn, project_id)
                cur = conn.cursor(row_factory=dict_row)
                cur.execute(
                    "SELECT id FROM memories WHERE project_id = %s AND regista_note_id = %s",
                    (project_id, entity_id),
                )
                local = cur.fetchone()

                body = folded["body"]
                embedding = None
                if embed_fn is not None and body:
                    embedding = embed_fn(body)

                if local:
                    cur.execute(
                        """
                        UPDATE memories SET
                            memory_type = %s, body = %s, attributes = %s,
                            embedding = COALESCE(%s::vector, embedding),
                            active = %s, updated_at = now()
                        WHERE id = %s
                        """,
                        (
                            memory_type,
                            body,
                            psycopg.types.json.Jsonb(folded["attributes"]),
                            embedding,
                            folded["active"],
                            local["id"],
                        ),
                    )
                    mirrored += 1
                else:
                    cur.execute(
                        """
                        INSERT INTO memories
                            (workspace_id, project_id, name, memory_type, body,
                             embedding, active, attributes, regista_note_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            ws_id,
                            project_id,
                            folded["name"],
                            memory_type,
                            body,
                            embedding,
                            folded["active"],
                            psycopg.types.json.Jsonb(folded["attributes"]),
                            entity_id,
                        ),
                    )
                    created += 1
                conn.commit()
        except Exception:
            failed += 1
            _log.warning("note rebuild failed for entity %s", entity_id, exc_info=True)

    _restore_supersedes_chain(project_id, supersede_of)

    return {"mirrored": mirrored, "created": created, "skipped": skipped, "failed": failed}


def _restore_supersedes_chain(project_id: int, supersede_of: dict[str, str]) -> None:
    """Point each newer memory's ``supersedes`` at the local id of the memory it
    replaced, using the inverted ``note_superseded`` map.

    ``supersede_of`` maps replacement entity_id -> superseded entity_id. Runs
    after all rows are upserted so the FK target exists; a missing older row
    sets ``supersedes`` to NULL (no constraint violation).
    """
    if not supersede_of:
        return
    from agent_notes.core.db import _conn as _get_conn

    try:
        with _get_conn() as conn:
            for newer_entity, older_entity in supersede_of.items():
                conn.execute(
                    """
                    UPDATE memories AS m
                    SET supersedes = sub.old_id
                    FROM (SELECT id AS old_id FROM memories
                          WHERE project_id = %s AND regista_note_id = %s) AS sub
                    WHERE m.project_id = %s AND m.regista_note_id = %s
                    """,
                    (project_id, older_entity, project_id, newer_entity),
                )
            conn.commit()
    except Exception:
        _log.warning("supersedes chain restore failed", exc_info=True)


def _fold_note_events(events: list[Any]) -> dict:
    """Fold a note entity's event log into its current state.

    Deleted/superseded notes fold to ``active=False`` but are still returned
    (their row is rebuilt inactive so the revision chain stays intact).
    ``superseded_by`` carries the replacement entity_id, if any, so the rebuild
    can restore the local ``supersedes`` chain.
    """
    sorted_events = sorted(events, key=lambda e: e.event_seq)
    state: dict[str, Any] = {
        "note_subtype": "memory",
        "memory_type": None,
        "name": "",
        "body": "",
        "attributes": {},
        "active": True,
        "superseded_by": None,
    }
    for evt in sorted_events:
        transition = evt.transition
        payload = evt.payload or {}
        if transition == _NOTE_FILED:
            state["note_subtype"] = payload.get("note_subtype", "memory")
            state["memory_type"] = payload.get("memory_type")
            state["name"] = payload.get("name", "")
            state["body"] = payload.get("body", "")
            state["attributes"] = payload.get("attributes", {})
        elif transition == _NOTE_UPDATED:
            if "memory_type" in payload:
                state["memory_type"] = payload["memory_type"]
            if "note_subtype" in payload:
                state["note_subtype"] = payload["note_subtype"]
            if "body" in payload:
                state["body"] = payload["body"]
            if "attributes" in payload:
                state["attributes"] = payload["attributes"]
            if "name" in payload:
                state["name"] = payload["name"]
        elif transition == _NOTE_SUPERSEDED:
            state["active"] = False
            state["superseded_by"] = payload.get("superseded_by")
        elif transition == _NOTE_DELETED:
            state["active"] = False
    return state


def check_projection_drift(face: Any, *, project_id: int) -> dict:
    """Read-only drift check: regista note entities vs local memories table.

    Detects two drift classes:

    1. **Missing** — a regista note entity has no local row at all (hard crash
       between the regista commit and the local projection write).
    2. **Stale** — a local row exists but its content diverges from the folded
       authoritative state (hard crash during an update or delete: the regista
       event committed but the local UPDATE/DELETE did not).

    For each entity the authoritative state is folded from its event log using
    ``_fold_note_events`` and compared field-by-field against the local row.
    Mismatches are reported with named reasons and entity IDs so monitoring can
    gate on the result.

    This is a diagnostic — it never mutates. Repair is ``rebuild_from_regista``.

    Returns a dict with:
      - ``drifted``: total count of entities with any drift (missing + stale)
      - ``missing_entity_ids``: entities present in regista but absent locally
      - ``stale``: list of dicts with ``entity_id``, ``name``, ``reasons``
      - ``local_only``: count of local rows whose regista_note_id has no
        corresponding regista entity (expected after a schema wipe, not a bug)
      - ``regista_entity_count`` / ``local_row_count``: totals for context
    """
    from agent_notes.core.db import _conn as _get_conn

    note_events = face.list_note_entities()
    regista_ids = {str(evt.effective_entity_id) for evt in note_events}

    with _get_conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT regista_note_id, name, memory_type, body, active "
            "FROM memories "
            "WHERE project_id = %s AND regista_note_id IS NOT NULL",
            (project_id,),
        )
        local_rows: dict[str, dict] = {str(row["regista_note_id"]): row for row in cur.fetchall()}

    local_ids = set(local_rows.keys())
    missing_locally = sorted(regista_ids - local_ids)
    local_only = sorted(local_ids - regista_ids)

    # Stale detection: fold authoritative state and compare to local row.
    stale: list[dict] = []
    for latest_evt in note_events:
        entity_id = str(latest_evt.effective_entity_id)
        if entity_id not in local_rows:
            continue  # already counted as missing
        try:
            events = face.read_note_events(latest_evt.effective_entity_id)
            if not events:
                continue
            folded = _fold_note_events(events)
            local = local_rows[entity_id]
            reasons: list[str] = []

            # Compare active flag (catches crashed delete/supersede).
            if local["active"] != folded["active"]:
                reasons.append(f"active: local={local['active']} authoritative={folded['active']}")
            # Compare body (catches crashed update).
            if folded["body"] and local["body"] != folded["body"]:
                reasons.append("body mismatch")
            # Compare name (catches crashed rename via update).
            if folded["name"] and local["name"] != folded["name"]:
                reasons.append(f"name: local={local['name']!r} authoritative={folded['name']!r}")
            # Compare memory_type (catches crashed type change).
            authoritative_type = _resolve_memory_type(folded)
            if local["memory_type"] != authoritative_type:
                reasons.append(
                    f"memory_type: local={local['memory_type']!r} "
                    f"authoritative={authoritative_type!r}"
                )

            if reasons:
                stale.append(
                    {
                        "entity_id": entity_id,
                        "name": local["name"],
                        "reasons": reasons,
                    }
                )
        except Exception:
            _log.warning("drift check failed for entity %s", entity_id, exc_info=True)

    return {
        "drifted": len(missing_locally) + len(stale),
        "missing_entity_ids": missing_locally,
        "stale": stale,
        "local_only": len(local_only),
        "regista_entity_count": len(regista_ids),
        "local_row_count": len(local_ids),
    }
