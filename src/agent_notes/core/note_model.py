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
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_notes.core import face_factory
from agent_notes.core.change_log import write_change
from agent_notes.core.db import _conn

KIND = "memory"
NOTE_KIND = "note"

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


def _find_existing_note(
    conn: psycopg.Connection, project_id: int, name: str
) -> dict | None:
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
) -> dict:
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        INSERT INTO memories
            (workspace_id, project_id, name, memory_type, body, embedding,
             active, supersedes, attributes, regista_note_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id, workspace_id, project_id, name, memory_type, body,
                  active, supersedes, attributes, regista_note_id,
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
    """Write a memory as a signed note entity, then mirror to local projection."""
    actor = face_factory.default_actor()
    note_uuid = uuid.uuid4()
    note_subtype = _note_subtype_for(memory_type)

    payload: dict[str, Any] = {
        "note_subtype": note_subtype,
        "name": name,
        "body": body,
        "attributes": attributes or {},
        "links": [],
    }

    face = face_factory.get_face()
    if face is not None:
        face.append_note(
            actor,
            note_uuid,
            transition=_NOTE_FILED,
            payload=payload,
        )

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
        )
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

    return row


def update_memory(
    workspace_id: int,
    project_id: int,
    name: str,
    body: str | None = None,
    attributes: dict | None = None,
) -> dict:
    """Update a memory: append a note_updated event, then mirror to projection."""
    face = face_factory.get_face()
    actor = face_factory.default_actor()

    with _conn() as conn:
        ws_id = _resolve_workspace(conn, project_id)
        existing = _find_existing_note(conn, project_id, name)
        if existing is None:
            raise ValueError(f"Memory '{name}' not found (or deleted)")

        merged_attrs = dict(existing.get("attributes") or {})
        if attributes is not None:
            merged_attrs.update(attributes)

        note_id = existing.get("regista_note_id")
        if face is not None and note_id is not None:
            payload: dict[str, Any] = {
                "note_subtype": _note_subtype_for(existing["memory_type"]),
                "name": name,
            }
            if body is not None:
                payload["body"] = body
            if attributes is not None:
                payload["attributes"] = merged_attrs
            face.append_note(
                actor,
                note_id,
                transition=_NOTE_UPDATED,
                payload=payload,
            )

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
            payload={"fields": [k for k in ("body", "attributes") if locals().get(k) is not None]},
        )
        conn.commit()

    return dict(row)


def delete_memory(
    workspace_id: int,
    project_id: int,
    name: str,
) -> dict | None:
    """Soft-delete a memory: append a note_deleted event, then flip active=false."""
    face = face_factory.get_face()
    actor = face_factory.default_actor()

    with _conn() as conn:
        ws_id = _resolve_workspace(conn, project_id)
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
        if face is not None and note_id is not None:
            face.append_note(
                actor,
                note_id,
                transition=_NOTE_DELETED,
                payload={},
            )

        cur.execute(
            "UPDATE memories SET active = false "
            "WHERE id = %s RETURNING id",
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
    the note body).

    Returns a summary dict with counts.
    """
    from agent_notes.core.db import _conn as _get_conn

    note_events = face.list_note_entities()
    mirrored = 0
    created = 0
    skipped = 0
    failed = 0

    for latest_evt in note_events:
        try:
            entity_id = latest_evt.effective_entity_id
            events = face.read_note_events(entity_id)
            if not events:
                skipped += 1
                continue

            folded = _fold_note_events(events)
            if folded is None:
                skipped += 1
                continue

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
                            body = %s, attributes = %s, embedding = COALESCE(%s, embedding),
                            active = %s, updated_at = now()
                        WHERE id = %s
                        """,
                        (body, psycopg.types.json.Jsonb(folded["attributes"]),
                         embedding, folded["active"], local["id"]),
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
                        (ws_id, project_id, folded["name"], folded["note_subtype"],
                         body, embedding, folded["active"],
                         psycopg.types.json.Jsonb(folded["attributes"]), entity_id),
                    )
                    created += 1
                conn.commit()
        except Exception:
            failed += 1

    return {"mirrored": mirrored, "created": created, "skipped": skipped, "failed": failed}


def _fold_note_events(events: list[Any]) -> dict | None:
    """Fold a note entity's event log into its current state.

    Returns None if the note was deleted (and should not be rebuilt).
    """
    sorted_events = sorted(events, key=lambda e: e.event_seq)
    state: dict[str, Any] = {
        "note_subtype": "memory",
        "name": "",
        "body": "",
        "attributes": {},
        "active": True,
    }
    for evt in sorted_events:
        transition = evt.transition
        payload = evt.payload or {}
        if transition == _NOTE_FILED:
            state["note_subtype"] = payload.get("note_subtype", "memory")
            state["name"] = payload.get("name", "")
            state["body"] = payload.get("body", "")
            state["attributes"] = payload.get("attributes", {})
        elif transition == _NOTE_UPDATED:
            if "body" in payload:
                state["body"] = payload["body"]
            if "attributes" in payload:
                state["attributes"] = payload["attributes"]
            if "name" in payload:
                state["name"] = payload["name"]
        elif transition == _NOTE_SUPERSEDED:
            state["active"] = False
        elif transition == _NOTE_DELETED:
            state["active"] = False
    return state
