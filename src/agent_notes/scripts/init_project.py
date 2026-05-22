"""`agent-notes init`: register a project from a filesystem path (decision 32).

Walks up to find the git root; defaults workspace=default, project=dirname,
breadcrumbs_dir='breadcrumbs' relative to repo root, repo_root=absolute path.
Idempotent upsert via core.db helpers.
"""

from __future__ import annotations

import os

from agent_notes.core.db import get_or_create_project, get_or_create_workspace


def _find_git_root(start: str) -> str | None:
    """Return nearest ancestor directory containing a .git folder, or None."""
    cur = os.path.abspath(start)
    while cur != os.path.dirname(cur):
        if os.path.isdir(os.path.join(cur, ".git")):
            return cur
        cur = os.path.dirname(cur)
    return None


def main(args) -> None:
    raw_path = args.path if hasattr(args, "path") else "."
    abs_path = os.path.abspath(raw_path)

    repo_root = _find_git_root(abs_path)
    if repo_root is None:
        print(
            f"Warning: no git root found above {abs_path!r}. "
            "Using the path itself as repo_root."
        )
        repo_root = abs_path

    # Derive project slug from the repository root directory name.
    slug = os.path.basename(repo_root)
    # Default breadcrumbs_dir is 'breadcrumbs' relative to repo_root.
    breadcrumbs_dir = "breadcrumbs"

    ws = get_or_create_workspace("default", "Default Workspace")
    proj = get_or_create_project(
        workspace_id=ws.id,
        slug=slug,
        name=slug,
        repo_root=repo_root,
        breadcrumbs_dir=breadcrumbs_dir,
    )
    print(f"Project '{proj.slug}' registered under workspace 'default'.")
    print(f"  repo_root:        {proj.repo_root}")
    print(f"  breadcrumbs_dir:  {proj.breadcrumbs_dir}")
