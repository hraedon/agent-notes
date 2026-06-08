"""Cross-project layer for the work-log kernel (Plan 008 P3).

Public surface:
- `export_ops_jsonl` — export local op-log for a project as JSONL.
- `ingest_jsonl_ops` — ingest JSONL ops into the derived index (`cross_project_ops`).
- `rebuild_cross_project_cache` — fold foreign ops into `cross_project_work_items` cache.
- `fold_cross_project_work_item` — fold a single foreign entity from `cross_project_ops`.
- `get_cross_project_work_item` — read from the folded cache.
- `add_cross_repo_link` — add a cross-repo link (cross_project_links table).
- `update_project_registry` — set log_location / wake_channel on a project.

Design:
- The derived index (`cross_project_ops`) is a rebuildable cache; it is never SoT.
- Ingestion is additive and idempotent (UPSERT on conflict).
- The fold logic is shared with the local kernel: apply ops ordered by (lamport, op_id).
- Cross-repo blockers are resolved via `cross_project_work_items`, never by reaching
  into other repos' logs.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_notes.core.db import _conn
from agent_notes.core.kernel import (
    _apply_op_to_state,
    _resolve_status_lattice,
)

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def update_project_registry(
    project_id: int,
    log_location: str | None = None,
    wake_channel: str | None = None,
) -> None:
    """Update the registry descriptor for a project (log_location + wake_channel)."""
    with _conn() as conn:
        cur = conn.cursor()
        fields: list[str] = []
        params: list[Any] = []
        if log_location is not None:
            fields.append("log_location = %s")
            params.append(log_location)
        if wake_channel is not None:
            fields.append("wake_channel = %s")
            params.append(wake_channel)
        if not fields:
            return
        params.append(project_id)
        cur.execute(
            f"UPDATE projects SET {', '.join(fields)} WHERE id = %s",
            params,
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_ops_jsonl(project_id: int) -> str:
    """Export all local ops for a project as newline-delimited JSON.

    Each line is a JSON object with the same shape as an op_log row,
    minus the local envelope (envelope is stripped to avoid leaking local
    signing metadata). The output can be piped directly to `ingest_jsonl_ops`.
    """
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        # Select ops whose entity appears in the local work_items cache for this project.
        cur.execute(
            """
            SELECT o.op_id, o.entity_id, o.entity_type, o.op_type, o.lamport,
                   o.actor_id, o.payload, o.parent_op_ids, o.created_at
            FROM op_log o
            WHERE o.entity_id IN (
                SELECT entity_id FROM work_items WHERE project_id = %s
            )
            ORDER BY o.lamport ASC, o.op_id ASC
            """,
            (project_id,),
        )
        lines: list[str] = []
        for row in cur.fetchall():
            payload = row["payload"]
            if payload and isinstance(payload, dict) and "envelope" in payload:
                payload = {k: v for k, v in payload.items() if k != "envelope"}
                row = dict(row)
                row["payload"] = payload
            lines.append(json.dumps(row, default=str, sort_keys=True))
        return "\n".join(lines) + "\n" if lines else ""


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


def ingest_jsonl_ops(jsonl_data: str, source_project_slug: str) -> int:
    """Ingest JSONL ops into `cross_project_ops`.

    Returns the number of ops ingested. Idempotent: duplicate op_ids are
    updated (payload and parent_op_ids refreshed, ingested_at bumped).
    """
    if not jsonl_data.strip():
        return 0

    lines = jsonl_data.strip().split("\n")
    count = 0

    with _conn() as conn:
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                op = json.loads(line)
            except json.JSONDecodeError:
                continue

            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO cross_project_ops
                    (source_project_slug, op_id, entity_id, entity_type, op_type,
                     lamport, actor_id, payload, parent_op_ids, freshness_offset)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_project_slug, op_id) DO UPDATE SET
                    payload = EXCLUDED.payload,
                    parent_op_ids = EXCLUDED.parent_op_ids,
                    freshness_offset = EXCLUDED.freshness_offset,
                    ingested_at = now()
                """,
                (
                    source_project_slug,
                    op["op_id"],
                    op["entity_id"],
                    op["entity_type"],
                    op["op_type"],
                    op["lamport"],
                    op.get("actor_id"),
                    psycopg.types.json.Jsonb(op.get("payload", {})),
                    op.get("parent_op_ids", []),
                    op.get("freshness_offset"),
                ),
            )
            count += 1

        # Update freshness record.
        if count:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO cross_project_freshness (source_project_slug, last_ingested_at)
                VALUES (%s, now())
                ON CONFLICT (source_project_slug) DO UPDATE SET
                    last_ingested_at = now()
                """,
                (source_project_slug,),
            )

        conn.commit()
    return count


# ---------------------------------------------------------------------------
# Fold: rebuild foreign entity state from cross_project_ops
# ---------------------------------------------------------------------------


def _get_cross_project_ops(
    conn: psycopg.Connection, source_project_slug: str, entity_id: str
) -> list[dict]:
    """Return all foreign ops for an entity, ordered by (lamport, op_id)."""
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        SELECT op_id, op_type, lamport, payload, parent_op_ids, ingested_at
        FROM cross_project_ops
        WHERE source_project_slug = %s AND entity_id = %s
        ORDER BY lamport ASC, op_id ASC
        """,
        (source_project_slug, entity_id),
    )
    return [dict(r) for r in cur.fetchall()]


def fold_cross_project_work_item(
    conn: psycopg.Connection,
    source_project_slug: str,
    entity_id: str,
    write_cache: bool = True,
) -> dict | None:
    """Rebuild a foreign work_item from its cross_project_ops and write to cache.

    Mirrors `kernel.fold_work_item` but reads from `cross_project_ops`.
    Returns the folded work_item dict, or None if the entity has no ops.
    """
    ops = _get_cross_project_ops(conn, source_project_slug, entity_id)
    if not ops:
        return None

    # Group ops by lamport for concurrent resolution.
    groups: list[list[dict]] = []
    current_lamport: int | None = None
    current_group: list[dict] = []
    for op in ops:
        if op["lamport"] != current_lamport:
            if current_group:
                groups.append(current_group)
            current_lamport = op["lamport"]
            current_group = [op]
        else:
            current_group.append(op)
    if current_group:
        groups.append(current_group)

    state: dict[str, Any] = {
        "entity_id": entity_id,
        "project_id": None,
        "identifier": None,
        "title": None,
        "body_hash": None,
        "kind": None,
        "status": None,
        "severity": "medium",
        "external_refs": {},
        "diagnostic_keys": {},
        "embedding": None,
        "frontmatter_version": 1,
    }

    for group in groups:
        status_ops: list[dict] = []
        for op in group:
            if op["op_type"] in ("set_status", "close"):
                status_ops.append(op)
            else:
                _apply_op_to_state(state, op)

        if status_ops:
            state["status"] = _resolve_status_lattice(status_ops, state.get("status"))

    if state["project_id"] is None or state["identifier"] is None:
        return None

    if write_cache:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            INSERT INTO cross_project_work_items
                (source_project_slug, entity_id, identifier, title, body_hash,
                 kind, status, severity, external_refs)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_project_slug, entity_id) DO UPDATE SET
                identifier = EXCLUDED.identifier,
                title = EXCLUDED.title,
                body_hash = EXCLUDED.body_hash,
                kind = EXCLUDED.kind,
                status = EXCLUDED.status,
                severity = EXCLUDED.severity,
                external_refs = EXCLUDED.external_refs,
                updated_at = now()
            RETURNING *
            """,
            (
                source_project_slug,
                state["entity_id"],
                state["identifier"],
                state["title"],
                state["body_hash"],
                state["kind"],
                state["status"],
                state["severity"],
                psycopg.types.json.Jsonb(state["external_refs"]),
            ),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    return dict(state)


# ---------------------------------------------------------------------------
# Rebuild all foreign entities
# ---------------------------------------------------------------------------


def rebuild_cross_project_cache() -> int:
    """Rebuild the entire `cross_project_work_items` cache from `cross_project_ops`.

    Returns the number of foreign work_items rebuilt.
    """
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT DISTINCT source_project_slug, entity_id
            FROM cross_project_ops
            WHERE entity_type = 'work_item'
            ORDER BY source_project_slug, entity_id
            """
        )
        pairs = [(r["source_project_slug"], r["entity_id"]) for r in cur.fetchall()]
        count = 0
        for source_slug, entity_id in pairs:
            folded = fold_cross_project_work_item(conn, source_slug, entity_id, write_cache=True)
            if folded:
                count += 1
        conn.commit()
    return count


# ---------------------------------------------------------------------------
# Cache queries
# ---------------------------------------------------------------------------


def get_cross_project_work_item(source_project_slug: str, identifier: str) -> dict | None:
    """Read a foreign work item from the folded cache."""
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT * FROM cross_project_work_items
            WHERE source_project_slug = %s AND identifier = %s
            """,
            (source_project_slug, identifier),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_cross_project_work_items(
    source_project_slug: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List foreign work items from the folded cache."""
    conditions: list[str] = []
    params: list[Any] = []
    if source_project_slug is not None:
        conditions.append("source_project_slug = %s")
        params.append(source_project_slug)
    if status is not None:
        conditions.append("status = %s")
        params.append(status)
    where = " AND ".join(conditions) if conditions else "TRUE"
    params.append(min(limit, 200))

    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT * FROM cross_project_work_items
            WHERE {where}
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            params,
        )
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Cross-repo links
# ---------------------------------------------------------------------------


def add_cross_repo_link(
    from_project_id: int,
    from_identifier: str,
    to_project_slug: str,
    to_identifier: str,
    relationship: str = "blocks",
) -> dict:
    """Add a cross-repo link to `cross_project_links`.

    The edge is owned by the dependent (the `from` side). The target is
    identified by project slug + identifier, so it can be resolved via the
    derived index even if the target project lives in a different repo.
    """
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            INSERT INTO cross_project_links
                (from_project_id, from_identifier, to_project_slug, to_identifier, relationship)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (
                from_project_id, from_identifier, to_project_slug,
                to_identifier, relationship
            )
            DO UPDATE SET created_at = now()
            RETURNING *
            """,
            (from_project_id, from_identifier, to_project_slug, to_identifier, relationship),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row)


def remove_cross_repo_link(
    from_project_id: int,
    from_identifier: str,
    to_project_slug: str,
    to_identifier: str,
    relationship: str = "blocks",
) -> bool:
    """Remove a cross-repo link. Returns True if a row was deleted."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM cross_project_links
            WHERE from_project_id = %s
              AND from_identifier = %s
              AND to_project_slug = %s
              AND to_identifier = %s
              AND relationship = %s
            """,
            (from_project_id, from_identifier, to_project_slug, to_identifier, relationship),
        )
        deleted = cur.rowcount > 0
        conn.commit()
        return deleted


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


def get_freshness(source_project_slug: str) -> dict | None:
    """Return the last-ingested freshness record for a source project."""
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT * FROM cross_project_freshness WHERE source_project_slug = %s",
            (source_project_slug,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_freshness() -> list[dict]:
    """List all freshness records."""
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT * FROM cross_project_freshness ORDER BY source_project_slug")
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Reverse-edge queries (wake routing)
# ---------------------------------------------------------------------------


def get_blocked_by(
    blocker_project_slug: str,
    blocker_identifier: str,
) -> list[dict]:
    """Return all local work items that are blocked by a foreign work item.

    Used by the cross-project trigger loop: when a foreign work item closes,
    look up who was waiting on it and route `dependency.resolved` events.
    """
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT cpl.from_project_id, cpl.from_identifier,
                   p.slug AS project_slug, w.slug AS workspace_slug
            FROM cross_project_links cpl
            JOIN projects p ON p.id = cpl.from_project_id
            JOIN workspaces w ON w.id = p.workspace_id
            WHERE cpl.to_project_slug = %s
              AND cpl.to_identifier = %s
              AND cpl.relationship = 'blocks'
            """,
            (blocker_project_slug, blocker_identifier),
        )
        return [dict(r) for r in cur.fetchall()]


def get_cross_project_blockers(
    from_project_id: int,
    from_identifier: str,
) -> list[dict]:
    """Return the foreign work items that block a given local work item."""
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT cpl.to_project_slug, cpl.to_identifier,
                   cpwi.status, cpwi.title
            FROM cross_project_links cpl
            LEFT JOIN cross_project_work_items cpwi
                ON cpwi.source_project_slug = cpl.to_project_slug
               AND cpwi.identifier = cpl.to_identifier
            WHERE cpl.from_project_id = %s
              AND cpl.from_identifier = %s
              AND cpl.relationship = 'blocks'
            """,
            (from_project_id, from_identifier),
        )
        return [dict(r) for r in cur.fetchall()]
