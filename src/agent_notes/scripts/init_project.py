"""`agent-notes init`: register a project from a filesystem path (decision 32).

Walks up to find the git root; defaults workspace=default, project=dirname,
repo_root=absolute path. Idempotent upsert via core.db helpers.
Also seeds default vocabularies for the workspace if none exist.
"""

from __future__ import annotations

import os

from agent_notes.core.db import (
    get_or_create_project,
    get_or_create_workspace,
    list_vocabulary,
)


def _find_git_root(start: str) -> str | None:
    cur = os.path.abspath(start)
    while cur != os.path.dirname(cur):
        if os.path.isdir(os.path.join(cur, ".git")):
            return cur
        cur = os.path.dirname(cur)
    return None


_DEFAULT_VOCAB = {
    "bc_kind": [
        ("todo", False, True, 10),
        ("observation", False, True, 20),
        ("decision", False, True, 30),
        ("risk", False, True, 40),
        ("task", False, True, 50),
        ("bug", False, True, 60),
        ("feature", False, True, 70),
        ("improvement", False, True, 80),
        ("question", False, True, 90),
        ("experiment", False, True, 100),
        ("spike", False, True, 110),
        ("refactor", False, True, 120),
        ("docs", False, True, 130),
        ("ci", False, True, 140),
        ("job", False, True, 150),
        ("ux", False, True, 160),
        ("perf", False, True, 170),
        ("rfc", False, True, 180),
        ("design", False, True, 190),
    ],
    "bc_status": [
        ("new", False, True, 10),
        ("open", False, True, 20),
        ("in_progress", False, True, 30),
        ("blocked", False, True, 40),
        ("under_review", False, True, 50),
        ("proposed", False, True, 55),
        ("resolved", True, False, 100),
        ("closed", True, False, 110),
        ("wont_fix", True, False, 120),
        ("duplicate", True, False, 130),
    ],
    "bc_severity": [
        ("low", False, True, 10),
        ("medium", False, True, 20),
        ("high", False, True, 30),
        ("critical", False, True, 40),
    ],
    "memory_type": [
        ("project", False, True, 10),
        ("reference", False, True, 20),
        ("feedback", False, True, 30),
        ("reflection", False, True, 40),
    ],
}


def _seed_vocabularies_if_empty(workspace_id: int) -> int:
    existing = list_vocabulary(workspace_id)
    if existing:
        return 0
    from agent_notes.core.db import add_vocabulary

    count = 0
    for namespace, entries in _DEFAULT_VOCAB.items():
        for name, is_terminal, is_open, sort_order in entries:
            add_vocabulary(workspace_id, namespace, name, is_terminal, is_open, sort_order)
            count += 1
    return count


def main(args) -> None:
    raw_path = args.path if hasattr(args, "path") else "."
    abs_path = os.path.abspath(raw_path)

    repo_root = _find_git_root(abs_path)
    if repo_root is None:
        print(f"Warning: no git root found above {abs_path!r}. Using the path itself as repo_root.")
        repo_root = abs_path

    slug = os.path.basename(repo_root)

    ws = get_or_create_workspace("default", "Default Workspace")

    seeded = _seed_vocabularies_if_empty(ws.id)
    if seeded:
        print(f"Seeded {seeded} default vocabulary entries for workspace 'default'.")

    proj = get_or_create_project(
        workspace_id=ws.id,
        slug=slug,
        name=slug,
        repo_root=repo_root,
    )
    print(f"Project '{proj.slug}' registered under workspace 'default'.")
    print(f"  repo_root: {proj.repo_root}")
