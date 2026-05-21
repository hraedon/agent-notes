"""One-shot markdown regeneration for existing breadcrumbs (Phase 2b.4).

Usage:
    AGENT_NOTES_DSN=postgresql://... \
    python -m agent_notes.scripts.regenerate_markdown \
        --workspace default \
        --project sf2

Re-writes canonical frontmatter-v1 files for each project's breadcrumbs dir.
Sets `projection_sha256` after each successful write.  Reports drifts before
overwriting (pass --force to override).
"""

from __future__ import annotations

import argparse
import sys

from agent_notes.core.db import list_projects, list_workspaces
from agent_notes.core.projection import (
    SafeWriteResult,
    build_breadcrumb_markdown,
    safe_write,
)
from agent_notes.servers.breadcrumbs_model import BreadcrumbModel


def _resolve(ws_slug: str, proj_slug: str):
    ws = next((w for w in list_workspaces() if w.slug == ws_slug), None)
    if ws is None:
        raise SystemExit(f"workspace '{ws_slug}' not found")
    proj = next(
        (p for p in list_projects(workspace_id=ws.id) if p.slug == proj_slug),
        None,
    )
    if proj is None:
        raise SystemExit(f"project '{proj_slug}' not found")
    return ws, proj


def regenerate(
    workspace_slug: str,
    project_slug: str,
    force: bool = False,
    dry_run: bool = False,
) -> None:
    ws, proj = _resolve(workspace_slug, project_slug)
    rows = BreadcrumbModel.query_breadcrumbs(project_id=proj.id, limit=10000)
    if not rows:
        print("No breadcrumbs found.")
        return

    written = unchanged = drift = error = skipped = 0
    for row in rows:
        identifier = row["identifier"]
        status_dir = (
            "resolved"
            if row["status"] in {"resolved", "closed", "wont_fix", "duplicate"}
            else "active"
        )
        file_path = f"{status_dir}/{identifier}.md"

        repo_root = proj.repo_root or "/tmp"
        bcd = (proj.breadcrumbs_dir or "").strip("/")
        absolute = (
            f"{repo_root}/{bcd}/{file_path}".replace("//", "/")
            if repo_root
            else f"/{bcd}/{file_path}".strip("/")
        )

        content = build_breadcrumb_markdown(row)
        expected = row.get("projection_sha256")
        if isinstance(expected, memoryview):
            expected = bytes(expected)

        if dry_run:
            print(f"[dry-run] Would write {absolute}")
            skipped += 1
            continue

        from pathlib import Path

        outcome = safe_write(Path(absolute), content, expected if not force else None)

        if outcome.result == SafeWriteResult.WRITTEN:
            # Persist hash in DB.
            BreadcrumbModel.update_breadcrumb(
                project_id=proj.id,
                identifier=identifier,
                file_path=file_path,
                frontmatter_version=1,  # ensure version is set
            )
            written += 1
        elif outcome.result == SafeWriteResult.UNCHANGED:
            unchanged += 1
        elif outcome.result == SafeWriteResult.DRIFT:
            print(f"[DRIFT] {absolute}")
            drift += 1
        else:
            print(f"[ERROR] {absolute}: {outcome.exception}")
            error += 1

    print(
        f"Done: {written} written, {unchanged} unchanged, "
        f"{drift} drift, {error} error, {skipped} skipped"
    )
    if drift and not force:
        print("Re-run with --force to overwrite drifted files.")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Regenerate markdown projections for breadcrumbs")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    regenerate(args.workspace, args.project, force=args.force, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
