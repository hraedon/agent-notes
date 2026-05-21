"""One-shot import script for legacy breadcrumb-mcp data.

Usage:
    AGENT_NOTES_DSN=postgresql://... \
    AGENT_NOTES_LEGACY_DSN=postgresql://... \
    python -m agent_notes.scripts.import_legacy_bc

Phase 2a.3 requirements:
- COPY (not per-row INSERT) for speed.
- Single batch INSERT into change_log (trigger disabled for speed).
- Seed vocabularies with is_terminal / is_open / sort_order columns.
- Normalize legacy file_path values to repo-relative-under-breadcrumbs_dir form.
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


def _normalize_file_path(raw: str | None, breadcrumbs_dir: str) -> str | None:
    """Strip absolute prefix and breadcrumbs_dir to leave a clean relative path."""
    if not raw:
        return None
    # Remove leading absolute or repo-relative prefix up to breadcrumbs_dir.
    # e.g. "/projects/sf2/breadcrumbs/resolved/RFC-031.md" → "resolved/RFC-031.md"
    # or "breadcrumbs/resolved/RFC-031.md" → "resolved/RFC-031.md"
    parts = raw.replace("\\", "/").split("/")
    if breadcrumbs_dir in parts:
        idx = parts.index(breadcrumbs_dir) + 1
        return "/".join(parts[idx:])
    return raw.lstrip("/")


def _copy_breadcrumbs(ws_id: int) -> list[dict]:
    with _get_legacy_conn() as leg:
        cur = leg.cursor(row_factory=dict_row)
        cur.execute("SELECT * FROM breadcrumbs ORDER BY identifier")
        rows = [dict(r) for r in cur.fetchall()]
    return rows


def _insert_breadcrumbs_copy(rows: list[dict], project_id: int) -> None:
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
        "file_path",
        "frontmatter_version",
        "projection_sha256",
        "projection_dirty",
        "created_at",
        "updated_at",
        "closed_at",
    ]
    col_str = ", ".join(columns)
    with _get_new_conn() as conn:
        with conn.cursor() as cur:
            # Use COPY for speed.
            import io

            buf = io.StringIO()
            for r in rows:
                vals = [
                    str(project_id),
                    r["identifier"],
                    r["title"],
                    r.get("body", ""),
                    r["kind"],
                    r["status"],
                    r.get("severity", "medium"),
                    psycopg.types.json.Jsonb(r.get("external_refs") or {}).dumpb().decode(),
                    psycopg.types.json.Jsonb(r.get("diagnostic_keys") or {}).dumpb().decode(),
                    None,  # embedding re-computed later
                    # TODO: pull breadcrumbs_dir from projects row instead of
                    # hardcoding; works because all consumer repos use "breadcrumbs"
                    # (YAGNI until a project diverges)
                    _normalize_file_path(r.get("file_path"), "breadcrumbs"),
                    str(r.get("frontmatter_version", 1)),
                    r.get("projection_sha256") if r.get("projection_sha256") else "\\N",
                    "false",
                    r["created_at"].isoformat() if r.get("created_at") else "\\N",
                    r["updated_at"].isoformat() if r.get("updated_at") else "\\N",
                    r["closed_at"].isoformat() if r.get("closed_at") else "\\N",
                ]
                buf.write("\t".join(str(v) for v in vals) + "\n")
            buf.seek(0)
            with cur.copy(f"COPY breadcrumbs ({col_str}) FROM STDIN") as copy:
                copy.write(buf.read().encode())
        conn.commit()


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
        conn.execute(
            """
            INSERT INTO change_log (kind, workspace_id, project_id,
                                    identifier, event, payload, changed_at)
            SELECT * FROM UNNEST(%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                [v[0] for v in vals],
                [v[1] for v in vals],
                [v[2] for v in vals],
                [v[3] for v in vals],
                [v[4] for v in vals],
                [v[5] for v in vals],
                [v[6] for v in vals],
            ),
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
    for i, r in enumerate(rows, 1):
        vec = embed(f"{r['title']} {r['body']}", task="document")
        with _get_new_conn() as conn:
            conn.execute(
                "UPDATE breadcrumbs SET embedding = %s WHERE project_id = %s AND identifier = %s",
                (vec.tolist(), r["project_id"], r["identifier"]),
            )
            conn.commit()
        if i % 50 == 0:
            print(f"  {i}/{total} done")
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
        _insert_breadcrumbs_copy(rows, proj_id)
        _batch_change_log(rows, ws_id, proj_id)
        _enable_notify(conn)
    print("Rows copied and change_log batch-inserted.")

    _reembed(proj_id)
    print("Import complete.")


if __name__ == "__main__":
    main()
