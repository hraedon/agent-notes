"""Import historical reflections from disk into the agent-notes memory store (Phase 5.1).

Usage:
    AGENT_NOTES_DSN=postgresql://... python -m agent_notes.scripts.import_reflections \
        --workspace default \
        --project sf2 \
        /projects/software-factory-2/reflections/ \
        /projects/substrate/reflections/ \
        /projects/software-factory/reflections/ \
        /projects/breadcrumb-mcp/reflections/ \
        /projects/memory-mcp/reflections/

This script:
1. Walks each directory for *.md files.
2. Parses YAML frontmatter (model, datetime) from each reflection file.
3. Seeds 'reflection' memory_type vocabulary if not present.
4. Re-embeds each reflection body in-process (decision 2).
5. Calls add_memory(memory_type='reflection') for each file via the core DB layer.
6. Disables change_log_notify trigger during bulk insert (Kimi round-3 #2),
   writes change_log rows in a single batch, then re-enables.

Idempotent: re-running skips files whose name already exists as an active memory.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import frontmatter


def _collect_reflection_files(directories: list[str]) -> list[tuple[Path, dict, str]]:
    """Walk directories and return (path, frontmatter_dict, body) tuples."""
    results = []
    for dir_path in directories:
        p = Path(dir_path)
        if not p.is_dir():
            print(f"  skipping (not a directory): {p}")
            continue
        for md_file in sorted(p.glob("*.md")):
            try:
                post = frontmatter.load(md_file)
                fm = dict(post.metadata)
                body = post.content
                results.append((md_file, fm, body))
            except Exception as exc:
                print(f"  skipping (parse error): {md_file}: {exc}")
    return results


def _derive_memory_name(filepath: Path) -> str:
    """Derive a memory name from the file stem: YYYY-MM-DD-model-slug."""
    stem = filepath.stem
    slug = stem.replace(".", "-").replace(" ", "-").lower()
    return f"reflection-{slug}"


def run_import(
    directories: list[str],
    workspace_slug: str,
    project_slug: str,
    new_dsn: str,
) -> None:
    import psycopg
    from psycopg.rows import dict_row

    from agent_notes.core import db as coredb
    from agent_notes.core.embed import embed

    old_dsn = os.environ.get("AGENT_NOTES_DSN")
    os.environ["AGENT_NOTES_DSN"] = new_dsn
    coredb._pool = None

    try:
        print("Scanning reflection directories...")
        entries = _collect_reflection_files(directories)
        if not entries:
            print("No reflection files found. Nothing to import.")
            return

        print(f"Found {len(entries)} reflection file(s).")

        ws = coredb.get_or_create_workspace(workspace_slug, workspace_slug)
        proj = coredb.get_or_create_project(ws.id, project_slug, project_slug)
        coredb.add_vocabulary(ws.id, "memory_type", "reflection")
        print(f"  workspace={ws.slug}, project={proj.slug}")

        existing_names: set[str] = set()
        with coredb._conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM memories WHERE project_id = %s AND active = true",
                (proj.id,),
            )
            existing_names = {r["name"] for r in cur.fetchall()}

        to_import = []
        for filepath, fm, body in entries:
            name = _derive_memory_name(filepath)
            if name in existing_names:
                print(f"  skipping (already imported): {name}")
                continue
            to_import.append((filepath, fm, body, name))

        if not to_import:
            print("All reflections already imported. Nothing to do.")
            return

        print(f"Importing {len(to_import)} new reflection(s)...")

        with psycopg.connect(new_dsn, row_factory=dict_row) as conn:
            cur = conn.cursor()
            cur.execute("ALTER TABLE change_log DISABLE TRIGGER change_log_notify")

            imported = 0
            imported_names = []
            try:
                for filepath, fm, body, name in to_import:
                    imported_names.append(name)
                    vec = embed(body, task="document") if body.strip() else None

                    attributes = {
                        "model": fm.get("model", "unknown"),
                        "source_file": str(filepath),
                        "original_datetime": str(fm.get("datetime", "")),
                    }

                    cur.execute(
                        """
                        INSERT INTO memories
                            (workspace_id, project_id, name, memory_type, body,
                             embedding, active, attributes)
                        VALUES (%s, %s, %s, 'reflection', %s, %s, true, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            ws.id,
                            proj.id,
                            name,
                            body,
                            vec.tolist() if vec is not None else None,
                            psycopg.types.json.Jsonb(attributes),
                        ),
                    )
                    imported += 1
                    if imported % 10 == 0:
                        print(f"  {imported}/{len(to_import)} processed...")

                if imported_names:
                    cur.execute(
                        """
                        INSERT INTO change_log
                            (kind, workspace_id, project_id, identifier, event, payload)
                        SELECT 'memory', %s, %s, name, 'filed',
                               jsonb_build_object('source', 'reflection_import')
                        FROM UNNEST(%s) AS t(name)
                        """,
                        (ws.id, proj.id, imported_names),
                    )
            finally:
                cur.execute("ALTER TABLE change_log ENABLE TRIGGER change_log_notify")

            conn.commit()

        print(f"Import complete: {imported} reflection(s) ingested into {project_slug}.")
        print("Use find_reflections or search_memory(memory_type='reflection') to query.")

    finally:
        if old_dsn is None:
            del os.environ["AGENT_NOTES_DSN"]
        else:
            os.environ["AGENT_NOTES_DSN"] = old_dsn
        coredb._pool = None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import historical reflection markdown files into agent-notes-mcp"
    )
    parser.add_argument(
        "directories",
        nargs="+",
        help="One or more directories containing reflection *.md files",
    )
    parser.add_argument(
        "--workspace",
        default="default",
        help="Workspace slug (default: 'default')",
    )
    parser.add_argument(
        "--project",
        required=True,
        help="Project slug to import reflections into",
    )
    args = parser.parse_args()

    new_dsn = os.environ.get("AGENT_NOTES_DSN", "")
    if not new_dsn:
        sys.exit("Error: AGENT_NOTES_DSN environment variable is not set.")

    run_import(args.directories, args.workspace, args.project, new_dsn)


if __name__ == "__main__":
    main()
