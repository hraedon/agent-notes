"""Cross-project support (P3): request / wait / cross-project links.

These write signed-evidence ops (``request`` / ``wait`` / ``add_link``) into
the local op-log. Request and wait records are *not* folded into the
``work_items`` cache — they live only in the op-log as evidence. The target
project materializes real work items from requests via its intake process.
"""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from agent_notes.core import face_factory, kernel
from agent_notes.core.change_log import write_change
from agent_notes.core.db import _conn

from . import _common

KIND = "work_item"
ENTITY_TYPE = "work_item"


def request_work_item(
    project_id: int,
    target_project_slug: str,
    title: str,
    body: str = "",
    kind: str = "task",
    actor_id: str | None = None,
    model_lineage: str | None = None,
) -> dict:
    """Request a new work item in a target project (cross-project, P3).

    Writes a ``request`` op in the **dependent's** (local) log. The target
    project's intake process will materialize a real work item from this
    request. The request is signed evidence; the target project owns the
    created item.
    """
    # WI-068 (B1): a request op is an agent-authored work-item write like any
    # other — resolve (and lineage-gate) the actor before touching the DB, and
    # commit the op with the resolved actor instead of a NULL / raw override.
    actor = face_factory.actor_with_overrides(
        actor_id, model_lineage, operation="work-item request"
    )
    with _conn() as conn:
        workspace_id = _common.resolve_workspace_for_project(conn, project_id)
        _common.validate_vocab(conn, workspace_id, "wi_kind", kind)

        # Generate a local identifier for the request.
        cur = conn.cursor(row_factory=psycopg.rows.tuple_row)
        cur.execute("SELECT allocate_work_item_identifier(%s)", (project_id,))
        identifier = cur.fetchone()[0]

        body_hash = kernel.store_blob(conn, body)
        payload = {
            "project_id": project_id,
            "identifier": identifier,
            "title": title,
            "body_hash": body_hash,
            "kind": kind,
            "status": "open",
            "severity": "medium",
            "external_refs": {},
            "diagnostic_keys": {},
            "target_project": target_project_slug,
            "request_type": "create_work_item",
        }

        entity_id = kernel._make_op_id(ENTITY_TYPE, "request", payload, [])
        op = kernel.commit_op(
            conn,
            entity_id=entity_id,
            entity_type=ENTITY_TYPE,
            op_type="request",
            payload=payload,
            actor_id=actor.actor_id,
        )

        # Request entities don't go into the work_items cache (they're
        # not real work items — they're signed evidence). They live only
        # in the op_log. The target project B creates a real work item
        # from this request via its intake process.
        kernel.emit_event(
            conn,
            op_id=op["op_id"],
            event_type="request.created",
            payload={
                "entity_id": entity_id,
                "identifier": identifier,
                "project_id": project_id,
                "target_project": target_project_slug,
            },
        )

        write_change(
            conn,
            kind=KIND,
            workspace_id=workspace_id,
            project_id=project_id,
            identifier=identifier,
            event="filed",
            payload={"title": title, "kind": kind, "request_type": "create_work_item"},
            actor=actor.actor_id,
        )

        conn.commit()
        return {
            "entity_id": entity_id,
            "identifier": identifier,
            "project_id": project_id,
            "title": title,
            "kind": kind,
            "target_project": target_project_slug,
            "request_type": "create_work_item",
        }


def wait_on_work_item(
    project_id: int,
    target_project_slug: str,
    target_identifier: str,
    actor_id: str | None = None,
    model_lineage: str | None = None,
) -> dict:
    """Register a wait on a target project's work item (cross-project, P3).

    Writes a ``wait`` op in the local log. This signals that the local
    session is blocked until the target item resolves. The wake system
    will resume the session when the target closes.
    """
    # WI-068 (B1): gate + resolve before any write, commit with the resolved
    # actor (see request_work_item above).
    actor = face_factory.actor_with_overrides(actor_id, model_lineage, operation="work-item wait")
    with _conn() as conn:
        workspace_id = _common.resolve_workspace_for_project(conn, project_id)

        # Generate a local identifier for the wait record.
        cur = conn.cursor(row_factory=psycopg.rows.tuple_row)
        cur.execute("SELECT allocate_work_item_identifier(%s)", (project_id,))
        identifier = cur.fetchone()[0]

        payload = {
            "project_id": project_id,
            "identifier": identifier,
            "title": f"Wait on {target_project_slug}:{target_identifier}",
            "target_project": target_project_slug,
            "target_identifier": target_identifier,
            "wait_type": "block_on_work_item",
            "status": "waiting",
        }

        entity_id = kernel._make_op_id(ENTITY_TYPE, "wait", payload, [])
        op = kernel.commit_op(
            conn,
            entity_id=entity_id,
            entity_type=ENTITY_TYPE,
            op_type="wait",
            payload=payload,
            actor_id=actor.actor_id,
        )

        # Wait records don't go into the work_items cache. They live only
        # in the op_log as signed evidence of the wait intent.
        kernel.emit_event(
            conn,
            op_id=op["op_id"],
            event_type="wait.registered",
            payload={
                "entity_id": entity_id,
                "identifier": identifier,
                "project_id": project_id,
                "target_project": target_project_slug,
                "target_identifier": target_identifier,
            },
        )

        write_change(
            conn,
            kind=KIND,
            workspace_id=workspace_id,
            project_id=project_id,
            identifier=identifier,
            event="filed",
            payload={
                "title": payload["title"],
                "kind": "wait",
                "wait_type": "block_on_work_item",
            },
            actor=actor.actor_id,
        )

        conn.commit()
        return {
            "entity_id": entity_id,
            "identifier": identifier,
            "project_id": project_id,
            "title": payload["title"],
            "kind": "wait",
            "target_project": target_project_slug,
            "target_identifier": target_identifier,
            "wait_type": "block_on_work_item",
        }


def add_cross_project_link(
    from_project_id: int,
    from_identifier: str,
    to_project_slug: str,
    to_identifier: str,
    relationship: str = "blocks",
    actor_id: str | None = None,
    model_lineage: str | None = None,
) -> dict:
    """Add a cross-project link (P3).

    Stores the edge in ``cross_project_links`` (cross-repo / foreign
    projects). If the target project also exists in the local DB, the
    edge is mirrored in ``links`` for same-project traversal.

    The ``cross_project_links`` table stores the target by slug so it
    survives across DB instances.
    """
    # WI-068 (B1): gate + resolve before any write — the link tables and the
    # add_link op are all authored writes (see request_work_item above).
    actor = face_factory.actor_with_overrides(
        actor_id, model_lineage, operation="work-item link-cross"
    )
    with _conn() as conn:
        workspace_id = _common.resolve_workspace_for_project(conn, from_project_id)

        # Resolve the target project by slug (optional — may be foreign).
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT id, workspace_id FROM projects WHERE slug = %s",
            (to_project_slug,),
        )
        to_proj = cur.fetchone()
        to_project_id = to_proj["id"] if to_proj else None
        to_workspace_id = to_proj["workspace_id"] if to_proj else None

        # Always store in cross_project_links (foreign-safe).
        from agent_notes.core.cross_project import add_cross_repo_link

        add_cross_repo_link(
            from_project_id=from_project_id,
            from_identifier=from_identifier,
            to_project_slug=to_project_slug,
            to_identifier=to_identifier,
            relationship=relationship,
        )

        # Mirror in links if the target project is local.
        if to_project_id is not None:
            from agent_notes.core.links import add_link

            add_link(
                from_kind="work_item",
                from_workspace=workspace_id,
                from_project=from_project_id,
                from_identifier=from_identifier,
                to_kind="work_item",
                to_workspace=to_workspace_id,
                to_project=to_project_id,
                to_identifier=to_identifier,
                relationship=relationship,
                # WI-069: the mirrored link_added change_log row carries the
                # lineage-gated actor resolved above, not NULL.
                actor=actor.actor_id,
            )

        # Write a link op to the op_log for provenance.
        payload = {
            "from_project_id": from_project_id,
            "from_identifier": from_identifier,
            "to_project_id": to_project_id,
            "to_project_slug": to_project_slug,
            "to_identifier": to_identifier,
            "relationship": relationship,
            "cross_project": True,
        }

        entity_id = kernel._make_op_id(ENTITY_TYPE, "add_link", payload, [])
        op = kernel.commit_op(
            conn,
            entity_id=entity_id,
            entity_type=ENTITY_TYPE,
            op_type="add_link",
            payload=payload,
            actor_id=actor.actor_id,
        )

        kernel.emit_event(
            conn,
            op_id=op["op_id"],
            event_type="link.added",
            payload={
                "from_project_id": from_project_id,
                "from_identifier": from_identifier,
                "to_project_id": to_project_id,
                "to_project_slug": to_project_slug,
                "to_identifier": to_identifier,
                "relationship": relationship,
                "cross_project": True,
            },
        )

        conn.commit()
        return {
            "from_project_id": from_project_id,
            "from_identifier": from_identifier,
            "to_project_id": to_project_id,
            "to_project_slug": to_project_slug,
            "to_identifier": to_identifier,
            "relationship": relationship,
        }


def parse_address(address: str) -> tuple[str, str]:
    """Parse a ``project:identifier`` address (P3).

    Returns ``(project_slug, identifier)``. Raises ValueError if malformed.
    """
    if ":" not in address:
        raise ValueError(f"Invalid address: {address!r}. Expected format: project:identifier")
    parts = address.split(":", 1)
    return parts[0], parts[1]
