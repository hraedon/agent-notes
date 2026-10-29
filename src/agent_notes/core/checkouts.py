"""Per-VM project checkouts (Plan 025 WI-2.6, ``schema/852_project_checkouts.sql``).

``projects.repo_root`` is one path, so it resolves on one machine. Each VM that
has a project checked out registers ``(vm_id, path)`` here, and
``db.resolve_project`` matches the current VM's checkouts before it falls back
to ``repo_root``.

Paths are compared as text, not through the local filesystem, so a Windows
checkout can be matched from any OS (the tests do): a path that looks like a
Windows path (drive letter, UNC, or a backslash) compares case-insensitively
with ``\\`` and ``/`` treated as the same separator, and so does every path on
a Windows host.
"""

from __future__ import annotations

import ntpath
import os
import re
from typing import Any

from agent_notes.core.db import _conn

_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:([\\/]|$)")


def looks_windows_path(path: str) -> bool:
    """True for ``C:\\x``, ``C:/x``, ``\\\\server\\share`` and anything with a backslash."""
    return bool(_WIN_DRIVE_RE.match(path)) or path.startswith("\\\\") or "\\" in path


def canonical_path(path: str) -> str:
    """Absolute, normalised path as stored: separators and case are kept as written."""
    if looks_windows_path(path):
        # ntpath so a Windows path normalises the same on a POSIX host, where
        # os.path.abspath would glue it onto the cwd.
        return ntpath.normpath(path)
    return os.path.abspath(path)


def match_key(path: str) -> str:
    """The comparison form of *path*: ``/`` separators, no trailing slash, folded if Windows."""
    windows = os.name == "nt" or looks_windows_path(path)
    key = canonical_path(path)
    if windows:
        key = key.replace("\\", "/").casefold()
    if len(key) > 1:
        key = key.rstrip("/") or "/"
    return key


def _is_within(key: str, root_key: str) -> bool:
    """Whether *key* is *root_key* or lies below it, on a path-component boundary."""
    return key == root_key or key.startswith(root_key.rstrip("/") + "/")


def _vm(vm_id: str) -> str:
    return vm_id.strip().lower()


def register_checkout(project_id: int, vm_id: str, path: str) -> None:
    """Record that *vm_id* has *project_id* checked out at *path*, and refresh ``last_seen_at``.

    A path belongs to one project per VM: registering it for another project
    moves it. A path that differs from an existing row only by case or
    separator (Windows) refreshes that row rather than adding a duplicate.
    """
    vm_id = _vm(vm_id)
    stored = canonical_path(path)
    key = match_key(stored)
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT path FROM project_checkouts WHERE vm_id = %s", (vm_id,))
        existing = next((r["path"] for r in cur.fetchall() if match_key(r["path"]) == key), None)
        cur.execute(
            """
            INSERT INTO project_checkouts (project_id, vm_id, path)
            VALUES (%s, %s, %s)
            ON CONFLICT (vm_id, path) DO UPDATE SET
                project_id = EXCLUDED.project_id,
                last_seen_at = now()
            """,
            (project_id, vm_id, existing or stored),
        )
        conn.commit()


def resolve_checkout(workspace_id: int, project_slug: str, vm_id: str) -> str | None:
    """This VM's checkout path for a project; the most recently seen if there are several."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT c.path
            FROM project_checkouts c
            JOIN projects p ON p.id = c.project_id
            WHERE p.workspace_id = %s AND p.slug = %s AND p.kind = 'repo' AND c.vm_id = %s
            ORDER BY c.last_seen_at DESC, c.created_at DESC, c.path
            LIMIT 1
            """,
            (workspace_id, project_slug, _vm(vm_id)),
        )
        row = cur.fetchone()
    return row["path"] if row else None


def find_project_by_checkout(path: str, vm_id: str) -> dict[str, Any] | None:
    """The project whose checkout on *vm_id* is the longest prefix of *path*, or ``None``.

    Same dict shape as ``db.resolve_project``. ``repo_root`` is the matching
    checkout path (this VM's root); ``project_repo_root`` is the shared
    ``projects.repo_root``. ``resolved_via`` is ``exact`` when *path* is the
    checkout itself, ``ancestor`` when it is inside one.
    """
    key = match_key(path)
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT c.path, c.vm_id, p.id AS project_id, p.slug AS project,
                   p.repo_root AS project_repo_root, p.log_location, p.wake_channel,
                   w.slug AS workspace
            FROM project_checkouts c
            JOIN projects p ON p.id = c.project_id
            JOIN workspaces w ON w.id = p.workspace_id
            WHERE c.vm_id = %s AND p.kind = 'repo'
            """,
            (_vm(vm_id),),
        )
        rows = cur.fetchall()
    best: tuple[int, dict[str, Any], str] | None = None
    for row in rows:
        root_key = match_key(row["path"])
        if _is_within(key, root_key) and (best is None or len(root_key) > best[0]):
            best = (len(root_key), row, root_key)
    if best is None:
        return None
    _, row, root_key = best
    return {
        "workspace": row["workspace"],
        "project": row["project"],
        "project_id": row["project_id"],
        "repo_root": row["path"],
        "log_location": row["log_location"],
        "wake_channel": row["wake_channel"],
        "resolved_via": "exact" if key == root_key else "ancestor",
        "source": "checkout",
        "vm_id": row["vm_id"],
        "project_repo_root": row["project_repo_root"],
    }


def list_checkouts(project_id: int | None = None, vm_id: str | None = None) -> list[dict[str, Any]]:
    """Checkouts (repo projects only), optionally filtered, newest-seen first within a project."""
    conditions = ["p.kind = 'repo'"]
    params: list[Any] = []
    if project_id is not None:
        conditions.append("c.project_id = %s")
        params.append(project_id)
    if vm_id is not None:
        conditions.append("c.vm_id = %s")
        params.append(_vm(vm_id))
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT c.project_id, w.slug AS workspace, p.slug AS project, c.vm_id, c.path,
                   c.created_at, c.last_seen_at
            FROM project_checkouts c
            JOIN projects p ON p.id = c.project_id
            JOIN workspaces w ON w.id = p.workspace_id
            WHERE {" AND ".join(conditions)}
            ORDER BY w.slug, p.slug, c.vm_id, c.last_seen_at DESC, c.path
            """,  # noqa: S608 - conditions are fixed literals; values are bound
            params,
        )
        return [dict(r) for r in cur.fetchall()]
