"""One-shot migration: breadcrumbs → work-item entities (Plan 008 Tier A #2).

.. deprecated::
    HISTORICAL RECORD ONLY — do not re-run and do not copy the status map below
    as current truth. The ``breadcrumbs`` table this script read from was dropped
    in schema 800 (``800_drop_breadcrumbs.sql``), so the script can no longer
    execute. The status mapping here reflects the **legacy breadcrumb vocabulary
    that predated the canonical lifecycle (Plan 010)**: it maps ``in_progress`` →
    ``claimed`` and ``blocked`` → ``open`` because those canonical states did not
    exist in the breadcrumb workflow yet.     The *current* canonical lifecycle
    states (open/in_progress/blocked/deferred/in_review/in_human_review/done) and
    the correct legacy→canonical mapping live in
    ``agent_notes.core.lifecycle`` (``LEGACY_TO_CANONICAL`` /
    ``map_legacy_to_canonical``).

Usage (historical):
    AGENT_NOTES_DSN=postgresql://... \
    python -m agent_notes.scripts.migrate_breadcrumbs_to_work_items

Design:
- Reads every row from the ``breadcrumbs`` table.
- For each breadcrumb, stores its body as a content-addressed blob.
- Creates a single ``create`` op in ``op_log`` that captures the current state.
- Folds the op into the ``work_items`` cache.
- Converts ``links`` rows from ``breadcrumb`` kind to ``work_item`` kind.
- Maps ``bc_status`` values to ``wi_status`` values.
- Idempotent: re-running on an already-migrated entity is a no-op (the op_id
  is deterministic based on the payload, so the INSERT into ``op_log``
  does nothing on conflict).

Status mapping (bc_status → wi_status):
    new              → open
    open             → open
    active           → claimed
    in_progress      → claimed
    blocked          → open
    under_review     → claimed
    proposed         → open
    decision-pending → open
    resolved         → closed
    closed           → closed
    implemented      → closed
    accepted         → closed
    wont_fix         → deferred
    wontfix          → deferred
    duplicate        → deferred
    obsolete         → deferred
    rejected         → deferred
    deferred         → deferred

**Run on a backed-up store.** This is a one-way migration that mutates the
``op_log`` and ``work_items`` tables. The ``breadcrumbs`` table is left intact
(as a safety net; it can be dropped after the migration is verified).
"""

from __future__ import annotations

import os
import sys

import psycopg
from psycopg.rows import dict_row


def _resolve_status(bc_status: str) -> str:
    """Map a breadcrumb status to a work-item status."""
    mapping = {
        "new": "open",
        "open": "open",
        "active": "claimed",
        "in_progress": "claimed",
        "blocked": "open",
        "under_review": "claimed",
        "proposed": "open",
        "decision-pending": "open",
        "resolved": "closed",
        "closed": "closed",
        "implemented": "closed",
        "accepted": "closed",
        "wont_fix": "deferred",
        "wontfix": "deferred",
        "duplicate": "deferred",
        "obsolete": "deferred",
        "rejected": "deferred",
        "deferred": "deferred",
    }
    return mapping.get(bc_status, "open")


def _resolve_kind(bc_kind: str) -> str:
    """Map a breadcrumb kind to a work-item kind.

    The wi_kind vocabulary is a superset of bc_kind, so most values map
    directly. Unknown values fall back to ``todo``.
    """
    # wi_kind and bc_kind share the same set of values in the schema.
    # If a project has custom bc_kind values not in wi_kind, they will
    # fail the vocab validation during migration. We fall back to ``todo``.
    return bc_kind if bc_kind in _VALID_WI_KINDS else "todo"


_VALID_WI_KINDS = {
    "todo",
    "observation",
    "decision",
    "risk",
    "task",
    "bug",
    "feature",
    "improvement",
    "question",
    "experiment",
    "spike",
    "refactor",
    "docs",
    "ci",
    "job",
}


def _content_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _store_blob(conn: psycopg.Connection, text: str) -> str:
    h = _content_hash(text)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO content_blobs (hash, content) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (h, text),
    )
    return h


def _make_op_id(entity_type: str, op_type: str, payload: dict, parent_op_ids: list[str]) -> str:
    import hashlib
    import json

    canonical = json.dumps(
        {
            "entity_type": entity_type,
            "op_type": op_type,
            "payload": payload,
            "parent_op_ids": sorted(parent_op_ids),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _migrate_breadcrumbs(conn: psycopg.Connection) -> int:
    """Migrate all breadcrumbs to work_items. Returns count migrated."""
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        SELECT b.*, p.workspace_id
        FROM breadcrumbs b
        JOIN projects p ON p.id = b.project_id
        ORDER BY b.project_id, b.identifier
        """
    )
    rows = cur.fetchall()
    if not rows:
        print("No breadcrumbs to migrate.")
        return 0

    count = 0
    for r in rows:
        project_id = r["project_id"]
        identifier = r["identifier"]
        title = r["title"]
        body = r.get("body", "")
        bc_kind = r["kind"]
        bc_status = r["status"]
        severity = r.get("severity", "medium")
        external_refs = r.get("external_refs") or {}
        diagnostic_keys = r.get("diagnostic_keys") or {}
        embedding = r.get("embedding")
        frontmatter_version = r.get("frontmatter_version", 1)
        created_at = r.get("created_at")
        updated_at = r.get("updated_at")
        closed_at = r.get("closed_at")

        # Store body as content-addressed blob.
        body_hash = _store_blob(conn, body)

        # Map status and kind.
        wi_status = _resolve_status(bc_status)
        wi_kind = _resolve_kind(bc_kind)

        # Build the create op payload.
        payload = {
            "project_id": project_id,
            "identifier": identifier,
            "title": title,
            "body_hash": body_hash,
            "kind": wi_kind,
            "status": wi_status,
            "severity": severity,
            "external_refs": external_refs,
            "diagnostic_keys": diagnostic_keys,
            "embedding": embedding,
            "frontmatter_version": frontmatter_version,
        }

        # The entity_id is the hash of the create op payload.
        entity_id = _make_op_id("work_item", "create", payload, [])

        # Check if this entity already has ops (idempotent re-run).
        cur.execute("SELECT 1 FROM op_log WHERE entity_id = %s LIMIT 1", (entity_id,))
        if cur.fetchone() is not None:
            print(f"  SKIP {identifier} (already migrated)")
            continue

        # Get the next lamport value.
        cur.execute("SELECT nextval('op_log_id_seq') AS lamport")
        lamport = cur.fetchone()["lamport"]

        # Build the op_id.
        op_id = _make_op_id("work_item", "create", payload, [])

        # Insert the create op.
        cur.execute(
            """
            INSERT INTO op_log
                (op_id, entity_id, entity_type, op_type, lamport, actor_id, payload, parent_op_ids)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (op_id) DO NOTHING
            """,
            (
                op_id,
                entity_id,
                "work_item",
                "create",
                lamport,
                "migration",
                psycopg.types.json.Jsonb(payload),
                [],
            ),
        )

        if closed_at is not None and wi_status in ("closed", "deferred"):
            cur.execute("SELECT nextval('op_log_id_seq') AS lamport")
            close_lamport = cur.fetchone()["lamport"]
            if wi_status == "closed":
                close_payload = {"reason": "migrated_from_breadcrumb"}
                close_op_id = _make_op_id("work_item", "close", close_payload, [op_id])
                terminal_op_type = "close"
            else:
                close_payload = {"status": "deferred", "reason": "migrated_from_breadcrumb"}
                close_op_id = _make_op_id("work_item", "set_status", close_payload, [op_id])
                terminal_op_type = "set_status"
            cur.execute(
                """
                INSERT INTO op_log
                    (op_id, entity_id, entity_type, op_type, lamport, actor_id, payload,
                     parent_op_ids)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (op_id) DO NOTHING
                """,
                (
                    close_op_id,
                    entity_id,
                    "work_item",
                    terminal_op_type,
                    close_lamport,
                    "migration",
                    psycopg.types.json.Jsonb(close_payload),
                    [op_id],
                ),
            )

        # Fold into work_items cache.
        from agent_notes.core.kernel import fold_work_item

        folded = fold_work_item(conn, entity_id)
        if folded is None:
            print(f"  WARN {identifier}: fold returned None after create op")
            continue

        # Update timestamps to match the original breadcrumb.
        # If the mapped status is terminal but the original breadcrumb had no
        # closed_at (e.g., bc_status='resolved' which is not terminal), set it
        # now so the work_item is consistent with its status.
        if closed_at is None and wi_status in ("closed", "deferred"):
            cur.execute(
                """
                UPDATE work_items
                SET created_at = %s,
                    updated_at = %s,
                    closed_at = now()
                WHERE entity_id = %s
                """,
                (created_at, updated_at, entity_id),
            )
        else:
            cur.execute(
                """
                UPDATE work_items
                SET created_at = %s,
                    updated_at = %s,
                    closed_at = COALESCE(%s, closed_at)
                WHERE entity_id = %s
                """,
                (created_at, updated_at, closed_at, entity_id),
            )

        print(f"  MIGRATED {identifier} → {folded['identifier']} ({wi_status})")
        count += 1

    return count


def _migrate_links(conn: psycopg.Connection) -> int:
    """Convert links rows from breadcrumb kind to work_item kind.

    Returns the number of links updated.
    """
    cur = conn.cursor()
    # Update from_kind = 'breadcrumb' → 'work_item'
    cur.execute(
        """
        UPDATE links
        SET from_kind = 'work_item'
        WHERE from_kind = 'breadcrumb'
        """
    )
    from_updated = cur.rowcount

    # Update to_kind = 'breadcrumb' → 'work_item'
    cur.execute(
        """
        UPDATE links
        SET to_kind = 'work_item'
        WHERE to_kind = 'breadcrumb'
        """
    )
    to_updated = cur.rowcount

    return from_updated + to_updated


def main() -> None:
    dsn = os.environ.get("AGENT_NOTES_DSN", "")
    if not dsn:
        sys.exit("Error: AGENT_NOTES_DSN environment variable is not set.")

    with psycopg.connect(dsn) as conn:
        print("Migrating breadcrumbs → work-items ...")
        migrated = _migrate_breadcrumbs(conn)
        print(f"Migrated {migrated} breadcrumb(s).")

        print("Migrating links (breadcrumb → work_item kind) ...")
        links_updated = _migrate_links(conn)
        print(f"Updated {links_updated} link row(s).")

        conn.commit()

    print("Migration complete.")
    print("  - Breadcrumbs table left intact (verify, then drop manually).")
    print("  - Run `agent-notes doctor` to verify schema.")
    print("  - Run `agent-notes work-item query` to inspect migrated items.")


if __name__ == "__main__":
    main()
