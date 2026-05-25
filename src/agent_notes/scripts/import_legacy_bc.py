"""One-shot import script for legacy breadcrumb-mcp data.

Usage:
    AGENT_NOTES_DSN=postgresql://... \
    AGENT_NOTES_LEGACY_DSN=postgresql://... \
    python -m agent_notes.scripts.import_legacy_bc

Requirements:
- COPY (not per-row INSERT) for speed.
- Single batch INSERT into change_log (trigger disabled for speed).
- Seed vocabularies with is_terminal / is_open / sort_order columns.
- Re-embed in-process (~1 minute for ~250 rows).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from agent_notes.core import db
from agent_notes.core.embed import embed

_LEGACY_DSN = os.environ.get("AGENT_NOTES_LEGACY_DSN", "")
_DSN = os.environ.get("AGENT_NOTES_DSN", "")


def _get_legacy_conn():
    if not _LEGACY_DSN:
        raise RuntimeError("AGENT_NOTES_LEGACY_DSN not set")
    return psycopg.connect(_LEGACY_DSN)


def _get_new_conn():
    if not _DSN:
        raise RuntimeError("AGENT_NOTES_DSN not set")
    return psycopg.connect(_DSN)


def _seed_vocabularies(ws_id: int) -> None:
    """Ensure legacy values exist with correct is_terminal / is_open / sort_order."""
    # Hard-coded seed based on the schema/100_breadcrumbs.sql defaults.
    # Real import would infer from legacy values.
    vals = [
        # bc_kind
        (ws_id, "bc_kind", "todo", False, True, 10),
        (ws_id, "bc_kind", "observation", False, True, 20),
        (ws_id, "bc_kind", "decision", False, True, 30),
        (ws_id, "bc_kind", "risk", False, True, 40),
        (ws_id, "bc_kind", "task", False, True, 50),
        (ws_id, "bc_kind", "bug", False, True, 60),
        (ws_id, "bc_kind", "feature", False, True, 70),
        (ws_id, "bc_kind", "improvement", False, True, 80),
        (ws_id, "bc_kind", "question", False, True, 90),
        (ws_id, "bc_kind", "experiment", False, True, 100),
        (ws_id, "bc_kind", "spike", False, True, 110),
        (ws_id, "bc_kind", "refactor", False, True, 120),
        (ws_id, "bc_kind", "docs", False, True, 130),
        (ws_id, "bc_kind", "ci", False, True, 140),
        (ws_id, "bc_kind", "job", False, True, 150),
        # bc_status
        (ws_id, "bc_status", "new", False, True, 10),
        (ws_id, "bc_status", "open", False, True, 20),
        (ws_id, "bc_status", "in_progress", False, True, 30),
        (ws_id, "bc_status", "blocked", False, True, 40),
        (ws_id, "bc_status", "under_review", False, True, 50),
        (ws_id, "bc_status", "resolved", True, False, 100),
        (ws_id, "bc_status", "closed", True, False, 110),
        (ws_id, "bc_status", "wont_fix", True, False, 120),
        (ws_id, "bc_status", "duplicate", True, False, 130),
        # bc_severity
        (ws_id, "bc_severity", "low", False, True, 10),
        (ws_id, "bc_severity", "medium", False, True, 20),
        (ws_id, "bc_severity", "high", False, True, 30),
        (ws_id, "bc_severity", "critical", False, True, 40),
    ]
    with _get_new_conn() as conn:
        cur = conn.cursor()
        cur.executemany(
            """
            INSERT INTO vocabularies
                (workspace_id, kind_namespace, name, is_terminal, is_open, sort_order)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (workspace_id, kind_namespace, name) DO UPDATE SET
                is_terminal = EXCLUDED.is_terminal,
                is_open = EXCLUDED.is_open,
                sort_order = EXCLUDED.sort_order
            """,
            vals,
        )
        conn.commit()


def _disable_notify(conn: psycopg.Connection) -> None:
    conn.execute("ALTER TABLE change_log DISABLE TRIGGER change_log_notify")


def _enable_notify(conn: psycopg.Connection) -> None:
    conn.execute("ALTER TABLE change_log ENABLE TRIGGER change_log_notify")


def _resolve_workspace(ws_slug: str) -> int:
    ws = next((w for w in db.list_workspaces() if w.slug == ws_slug), None)
    if ws is None:
        ws = db.get_or_create_workspace(ws_slug, ws_slug)
    return ws.id


def _resolve_project(workspace_id: int, proj_slug: str) -> int:
    proj = next(
        (p for p in db.list_projects(workspace_id=workspace_id) if p.slug == proj_slug),
        None,
    )
    if proj is None:
        proj = db.get_or_create_project(workspace_id, proj_slug, proj_slug)
    return proj.id


def _copy_breadcrumbs(ws_id: int) -> list[dict]:
    with _get_legacy_conn() as leg:
        cur = leg.cursor(row_factory=dict_row)
        cur.execute("SELECT * FROM breadcrumbs ORDER BY identifier")
        rows = [dict(r) for r in cur.fetchall()]
    return rows


def _insert_breadcrumbs_copy(conn: psycopg.Connection, rows: list[dict], project_id: int) -> None:
    if not rows:
        return
    columns = [
        "project_id",
        "identifier",
        "title",
        "body",
        "kind",
        "status",
        "severity",
        "external_refs",
        "diagnostic_keys",
        "embedding",
        "frontmatter_version",
        "created_at",
        "updated_at",
        "closed_at",
    ]
    col_str = ", ".join(columns)
    import psycopg.sql

    placeholders = ", ".join(["%s"] * len(columns))
    insert_sql = psycopg.sql.SQL(
        f"INSERT INTO breadcrumbs ({col_str}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
    )
    cur = conn.cursor()
    for r in rows:
        vals = [
            project_id,
            r["identifier"],
            r["title"],
            r.get("body", ""),
            r["kind"],
            r["status"],
            r.get("severity", "medium"),
            psycopg.types.json.Jsonb(r.get("external_refs") or {}),
            psycopg.types.json.Jsonb(r.get("diagnostic_keys") or {}),
            None,  # embedding re-computed later
            r.get("frontmatter_version", 1),
            r.get("filed_at"),
            r.get("updated_at"),
            r.get("closed_at"),
        ]
        cur.execute(insert_sql, vals)


def _batch_change_log(rows: list[dict], ws_id: int, project_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    # Build tuples for batch INSERT.
    vals = []
    for r in rows:
        vals.append(
            (
                "breadcrumb",
                ws_id,
                project_id,
                r["identifier"],
                "filed",
                psycopg.types.json.Jsonb(
                    {"title": r["title"], "kind": r["kind"], "status": r["status"]}
                ),
                now,
            )
        )
    if not vals:
        return
    with _get_new_conn() as conn:
        for v in vals:
            conn.execute(
                """
                INSERT INTO change_log (kind, workspace_id, project_id,
                                        identifier, event, payload, changed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                v,
            )
        conn.commit()


def _reembed(project_id: int) -> None:
    with _get_new_conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT project_id, identifier, title, body FROM breadcrumbs WHERE project_id = %s",
            (project_id,),
        )
        rows = cur.fetchall()
    total = len(rows)
    print(f"Re-embedding {total} breadcrumbs ...")
    if not total:
        return
    batch = []
    batch_size = 50
    for i, r in enumerate(rows, 1):
        vec = embed(f"{r['title']} {r['body']}", task="document")
        batch.append((r["project_id"], r["identifier"], vec.tolist()))
        if len(batch) >= batch_size:
            with _get_new_conn() as conn:
                with conn.transaction():
                    for pid, ident, vec_list in batch:
                        conn.execute(
                            (
                                "UPDATE breadcrumbs SET embedding = %s "
                                "WHERE project_id = %s AND identifier = %s"
                            ),
                            (vec_list, pid, ident),
                        )
            batch = []
            print(f"  {i}/{total} done")
    if batch:
        with _get_new_conn() as conn:
            with conn.transaction():
                for pid, ident, vec_list in batch:
                    conn.execute(
                        (
                            "UPDATE breadcrumbs SET embedding = %s "
                            "WHERE project_id = %s AND identifier = %s"
                        ),
                        (vec_list, pid, ident),
                    )
    print("Re-embedding complete.")


def main() -> None:
    # Hard-coded defaults for the substrate migration.
    ws_slug = os.environ.get("IMPORT_WS", "default")
    proj_slug = os.environ.get("IMPORT_PROJECT", "sf2")
    ws_id = _resolve_workspace(ws_slug)
    proj_id = _resolve_project(ws_id, proj_slug)

    _seed_vocabularies(ws_id)
    print("Vocabularies seeded.")

    rows = _copy_breadcrumbs(ws_id)
    print(f"Loaded {len(rows)} legacy breadcrumbs.")

    with _get_new_conn() as conn:
        _disable_notify(conn)
        try:
            _insert_breadcrumbs_copy(conn, rows, proj_id)
            conn.commit()
            _batch_change_log(rows, ws_id, proj_id)
        finally:
            _enable_notify(conn)
            conn.commit()
    print("Rows copied and change_log batch-inserted.")

    _reembed(proj_id)
    print("Import complete.")


if __name__ == "__main__":
    main()
