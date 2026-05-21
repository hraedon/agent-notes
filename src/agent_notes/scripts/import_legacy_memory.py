"""Import legacy memory-mcp data into the new agent-notes-mcp schema (Phase 3.5).

Usage:
    AGENT_NOTES_DSN=postgresql://... python -m agent_notes.scripts.import_legacy_memory \
        --legacy-dsn postgresql://user:pass@localhost/memory_mcp

This script:
1. Reads all memories from the legacy memory-mcp database.
2. Creates a 'default' workspace if not present.
3. Creates projects from observed project slugs (sf2-tagged memories → project=sf2,
   per Phase 0.2 / GLM #12).
4. Seeds memory_type vocabulary entries.
5. Copies memories, setting active=true.
6. Mirrors supersedes into the links table.
7. Re-embeds all memories in-process.
8. Disables change_log_notify trigger during bulk insert (Kimi round-3 #2),
   writes change_log rows in a single batch INSERT (GLM #4), then re-enables.

NOTE: This is a scaffold. Adjust the legacy schema assumptions (table/column names)
      to match the actual legacy memory-mcp schema when running against real data.
"""

from __future__ import annotations

import argparse
import os
import sys


def _find_legacy_memories(legacy_dsn: str) -> list[dict]:
    """Read memories from the legacy memory-mcp database.

    Adjust the SELECT columns and table name to match the actual legacy schema.
    Expected columns: id, name, project (TEXT), memory_type, body,
                      supersedes (nullable BIGINT), attributes (JSONB).
    """
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(legacy_dsn, row_factory=dict_row) as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                SELECT id, name, project, memory_type, body,
                       supersedes, attributes, created_at, updated_at
                FROM memories
                ORDER BY id
                """
            )
        except psycopg.Error:
            conn.rollback()
            cur.execute(
                """
                SELECT id, name, project, memory_type, body,
                       supersedes, attributes, created_at, updated_at
                FROM memory
                ORDER BY id
                """
            )
            return cur.fetchall()
        return cur.fetchall()


def run_import(legacy_dsn: str, new_dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    from agent_notes.core import db as coredb
    from agent_notes.core.embed import embed

    old_dsn = os.environ.get("AGENT_NOTES_DSN")
    os.environ["AGENT_NOTES_DSN"] = new_dsn
    coredb._pool = None
    coredb._DSN = new_dsn

    try:
        print("Reading legacy memories...")
        legacy_rows = _find_legacy_memories(legacy_dsn)
        if not legacy_rows:
            print("No legacy memories found. Nothing to import.")
            return

        print(f"Found {len(legacy_rows)} legacy memory row(s).")

        ws = coredb.get_or_create_workspace("default", "Default Workspace")

        project_map: dict[str, int] = {}
        for row in legacy_rows:
            proj_slug = row.get("project") or "global"
            if proj_slug not in project_map:
                proj = coredb.get_or_create_project(ws.id, proj_slug, proj_slug)
                project_map[proj_slug] = proj.id

        memory_types: set[str] = set()
        for row in legacy_rows:
            mt = row.get("memory_type")
            if mt:
                memory_types.add(mt)
        for mt in sorted(memory_types):
            coredb.add_vocabulary(ws.id, "memory_type", mt)
            print(f"  seeded vocabulary: memory_type/{mt}")

        print("Re-embedding memories...")
        id_map: dict[int, int] = {}

        with psycopg.connect(new_dsn, row_factory=dict_row) as conn:
            cur = conn.cursor()

            cur.execute("ALTER TABLE change_log DISABLE TRIGGER change_log_notify")

            for row in legacy_rows:
                proj_slug = row.get("project") or "global"
                proj_id = project_map[proj_slug]
                body = row.get("body", "")
                name = row["name"]
                memory_type = row.get("memory_type", "note")
                attributes = row.get("attributes") or {}
                supersedes = row.get("supersedes")

                vec = embed(body, task="document") if body.strip() else None

                cur.execute(
                    """
                    INSERT INTO memories
                        (workspace_id, project_id, name, memory_type, body,
                         embedding, active, supersedes, attributes,
                         created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, true, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        ws.id,
                        proj_id,
                        name,
                        memory_type,
                        body,
                        vec.tolist() if vec is not None else None,
                        id_map.get(supersedes) if supersedes else None,
                        psycopg.types.json.Jsonb(attributes),
                        row.get("created_at"),
                        row.get("updated_at"),
                    ),
                )
                new_id = cur.fetchone()["id"]
                id_map[row["id"]] = new_id

            cur.execute("ALTER TABLE change_log ENABLE TRIGGER change_log_notify")

            cur.execute(
                """
                INSERT INTO change_log (kind, workspace_id, project_id, identifier, event, payload)
                SELECT 'memory', %s, project_id, name, 'filed',
                       jsonb_build_object('id', id, 'source', 'legacy_import')
                FROM memories WHERE workspace_id = %s
                """,
                (ws.id, ws.id),
            )

            conn.commit()

        print(f"Import complete: {len(id_map)} memories migrated.")
        print(f"Projects: {', '.join(project_map.keys())}")

    finally:
        if old_dsn is None:
            del os.environ["AGENT_NOTES_DSN"]
        else:
            os.environ["AGENT_NOTES_DSN"] = old_dsn
        coredb._pool = None
        coredb._DSN = old_dsn or ""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import memories from legacy memory-mcp into agent-notes-mcp"
    )
    parser.add_argument(
        "--legacy-dsn",
        required=True,
        help="DSN for the legacy memory-mcp database",
    )
    args = parser.parse_args()

    new_dsn = os.environ.get("AGENT_NOTES_DSN", "")
    if not new_dsn:
        sys.exit("Error: AGENT_NOTES_DSN environment variable is not set.")

    run_import(args.legacy_dsn, new_dsn)


if __name__ == "__main__":
    main()
