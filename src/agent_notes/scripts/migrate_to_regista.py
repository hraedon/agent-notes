"""One-shot migration: local work_items → regista breadcrumbs (Plan 009 P1).

Usage:
    AGENT_NOTES_REGISTA_DSN=postgresql://... \
    AGENT_NOTES_REGISTA_HMAC_KEY_PATH=/path/to/keys.json \
    python -m agent_notes.scripts.migrate_to_regista [--project <slug>] [--apply]

Dry-run by default. With --apply, creates a regista breadcrumb work-item for
each local row and records the regista_work_item_id back on the local row.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_notes.core.actor import migration_actor
from agent_notes.core.config import regista_config
from agent_notes.core.db import _conn
from agent_notes.core.kernel import get_blob
from agent_notes.core.regista_face import RegistaFace


def _resolve_project(conn: psycopg.Connection, slug: str) -> dict:
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        "SELECT id, workspace_id, slug, name FROM projects WHERE slug = %s",
        (slug,),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"Project {slug!r} not found")
    return dict(row)


def _status_transition_for_create(status: str) -> str | None:
    return {
        "open": None,
        "claimed": "claim",
        "deferred": "defer_open",
        "closed": "close_open",
    }.get(status)


def _find_pending_rows(conn: psycopg.Connection, project_id: int) -> list[dict]:
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        SELECT wi.id, wi.project_id, wi.identifier, wi.entity_id, wi.title,
               wi.body_hash, wi.kind, wi.status, wi.severity, wi.external_refs,
               wi.diagnostic_keys, wi.embedding, wi.frontmatter_version,
               wi.created_at, wi.updated_at, wi.closed_at
        FROM work_items wi
        WHERE wi.project_id = %s AND wi.regista_work_item_id IS NULL
        ORDER BY wi.identifier
        """,
        (project_id,),
    )
    return [dict(r) for r in cur.fetchall()]


def _plan_row(row: dict) -> dict[str, Any]:
    transition_name = _status_transition_for_create(row["status"])
    return {
        "local_id": row["id"],
        "identifier": row["identifier"],
        "source_status": row["status"],
        "planned_transition": transition_name,
        "regista_work_item_id": None,
    }


def _find_existing_regista_id(face: RegistaFace, identifier: str) -> Any | None:
    page = face.list(page_size=100)
    for item in page:
        if (item.custom_fields or {}).get("source_identifier") == identifier:
            return item.work_item_id
    return None


def _migrate_row(
    conn: psycopg.Connection,
    face: RegistaFace,
    row: dict,
) -> dict[str, Any]:
    body = get_blob(conn, row["body_hash"]) or ""
    actor = migration_actor()
    wid = _find_existing_regista_id(face, row["identifier"])
    if wid is None:
        wid, _state = face.create_breadcrumb(
            actor,
            title=row["title"] or row["identifier"],
            description=body,
            severity=row["severity"] or "medium",
            kind=row["kind"] or "todo",
            external_refs=dict(row.get("external_refs") or {}),
            diagnostic_keys=dict(row.get("diagnostic_keys") or {}),
            source_identifier=row["identifier"],
        )
    transition_name = _status_transition_for_create(row["status"])
    state = "open"
    if transition_name is not None:
        existing = face.get(wid)
        if existing is not None and existing.current_state == "open":
            state = face.transition_breadcrumb(actor, wid, transition_name)
        elif existing is not None:
            state = existing.current_state

    cur = conn.cursor()
    cur.execute(
        "UPDATE work_items SET regista_work_item_id = %s WHERE id = %s",
        (str(wid), row["id"]),
    )

    return {
        "local_id": row["id"],
        "identifier": row["identifier"],
        "regista_work_item_id": wid,
        "status": state,
    }


def _run_migration(project_slug: str | None, apply: bool) -> int:
    cfg = regista_config()
    if not cfg.dsn:
        print("AGENT_NOTES_REGISTA_DSN is not set; cannot migrate.", file=sys.stderr)
        return 2
    if not cfg.hmac_key_path:
        print("AGENT_NOTES_REGISTA_HMAC_KEY_PATH is not set; cannot migrate.", file=sys.stderr)
        return 2

    face: RegistaFace | None = None
    if apply:
        import regista

        face = RegistaFace(
            regista.Regista(
                cfg.dsn,
                cfg.project,
                cfg.hmac_key_path,
                require_ssl=cfg.require_ssl,
            )
        )

    with _conn() as conn:
        if project_slug is None:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute("SELECT slug FROM projects ORDER BY slug")
            slugs = [r["slug"] for r in cur.fetchall()]
        else:
            slugs = [project_slug]

        total = 0
        samples: list[dict] = []
        for slug in slugs:
            project = _resolve_project(conn, slug)
            rows = _find_pending_rows(conn, project["id"])
            if not rows:
                print(f"Project {slug}: 0 rows to migrate")
                continue
            print(f"Project {slug}: {len(rows)} row(s) to migrate")
            for row in rows:
                if apply:
                    mapping = _migrate_row(conn, face, row)
                else:
                    mapping = _plan_row(row)
                total += 1
                if len(samples) < 3:
                    samples.append({"project": slug, **mapping})
            if apply:
                conn.commit()

    if face is not None:
        face.close()

    mode = "migrated" if apply else "would migrate"
    print(f"{mode} {total} work item(s)")
    for sample in samples:
        if apply:
            print(
                f"  {sample['project']}: {sample['identifier']} -> "
                f"{sample['regista_work_item_id']} (state={sample['status']})"
            )
        else:
            transition = sample["planned_transition"] or "(no transition)"
            print(
                f"  {sample['project']}: {sample['identifier']} "
                f"({sample['source_status']} -> {transition})"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate local work_items into regista breadcrumbs (dry-run by default)"
    )
    parser.add_argument("--project", default=None, help="Project slug (default: all projects)")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the migration; without this flag, only report what would happen",
    )
    args = parser.parse_args(argv)
    return _run_migration(args.project, args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
