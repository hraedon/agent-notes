"""Core database layer: connection pool, models, and shared CRUD (decision 22)."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

# ---------------------------------------------------------------------------
# Connection pool (decision 22: sync pool, min_size=2, max_size=5)
# ---------------------------------------------------------------------------

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def _reset_pool() -> None:
    """Reset the pool singleton. Called after fork to avoid sharing connections."""
    global _pool
    _pool = None


os.register_at_fork(after_in_child=_reset_pool)


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                dsn = os.environ.get("AGENT_NOTES_DSN", "")
                if not dsn:
                    raise RuntimeError(
                        "AGENT_NOTES_DSN environment variable is not set. "
                        "Example: postgresql://user:pass@localhost/agent_notes"
                    )
                _pool = ConnectionPool(
                    dsn,
                    min_size=2,
                    max_size=5,
                    open=True,
                    kwargs={"row_factory": dict_row},
                )
    return _pool


def get_pool() -> ConnectionPool:
    return _get_pool()


def _conn():
    """Return a pooled connection context manager."""
    return _get_pool().connection()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass
class Workspace:
    id: int
    slug: str
    name: str
    created_at: datetime


@dataclass
class Project:
    id: int
    workspace_id: int
    slug: str
    name: str
    repo_root: str | None
    created_at: datetime


@dataclass
class Vocab:
    workspace_id: int
    kind_namespace: str
    name: str
    is_terminal: bool
    is_open: bool
    sort_order: int
    archived: bool
    attributes: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Workspace CRUD
# ---------------------------------------------------------------------------


def get_or_create_workspace(slug: str, name: str) -> Workspace:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO workspaces (slug, name)
            VALUES (%s, %s)
            ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
            RETURNING id, slug, name, created_at
            """,
            (slug, name),
        )
        conn.commit()
        row = cur.fetchone()
        return Workspace(
            id=row["id"], slug=row["slug"], name=row["name"], created_at=row["created_at"]
        )


def list_workspaces() -> list[Workspace]:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, slug, name, created_at FROM workspaces ORDER BY slug")
        return [
            Workspace(id=r["id"], slug=r["slug"], name=r["name"], created_at=r["created_at"])
            for r in cur.fetchall()
        ]


# ---------------------------------------------------------------------------
# Project CRUD
# ---------------------------------------------------------------------------


def get_or_create_project(
    workspace_id: int,
    slug: str,
    name: str,
    repo_root: str | None = None,
) -> Project:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO projects (workspace_id, slug, name, repo_root)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (workspace_id, slug) DO UPDATE SET
                name = EXCLUDED.name,
                repo_root = COALESCE(EXCLUDED.repo_root, projects.repo_root)
            RETURNING id, workspace_id, slug, name, repo_root, created_at
            """,
            (workspace_id, slug, name, repo_root),
        )
        conn.commit()
        row = cur.fetchone()
        return _row_to_project(row)


def list_projects(workspace_id: int | None = None) -> list[Project]:
    with _conn() as conn:
        cur = conn.cursor()
        if workspace_id is not None:
            cur.execute(
                "SELECT id, workspace_id, slug, name, repo_root, created_at "
                "FROM projects WHERE workspace_id = %s ORDER BY slug",
                (workspace_id,),
            )
        else:
            cur.execute(
                (
                    "SELECT id, workspace_id, slug, name, repo_root, created_at "
                    "FROM projects ORDER BY workspace_id, slug"
                )
            )
        return [_row_to_project(r) for r in cur.fetchall()]


def resolve_project(path: str) -> dict:
    """Return project matching a filesystem path by longest-prefix match on repo_root.

    Returns dict: {"workspace": <slug>, "project": <slug>, "repo_root": <str>}
    Raises ValueError with a structured PROJECT_NOT_REGISTERED error if no match.
    """
    abs_path = os.path.abspath(path)
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT p.id, p.slug AS project, p.repo_root,
                   w.slug AS workspace
            FROM projects p
            JOIN workspaces w ON w.id = p.workspace_id
            ORDER BY LENGTH(p.repo_root) DESC
            """
        )
        for row in cur.fetchall():
            rr = row["repo_root"]
            if rr and (abs_path == rr or abs_path.startswith(rr + os.sep)):
                return {
                    "workspace": row["workspace"],
                    "project": row["project"],
                    "repo_root": row["repo_root"],
                    # "exact" = path is the project root; "ancestor" = path is
                    # *inside* a registered project (incl. resolving to a broad
                    # librarian root). Callers surface this so an unregistered
                    # project can't masquerade as an exact match / global view.
                    "resolved_via": "exact" if abs_path == rr else "ancestor",
                }
    raise ValueError(
        f"PROJECT_NOT_REGISTERED: no project found for path {abs_path!r}. "
        f"Run `agent-notes init {abs_path}` to register."
    )


def _row_to_project(row: dict) -> Project:
    return Project(
        id=row["id"],
        workspace_id=row["workspace_id"],
        slug=row["slug"],
        name=row["name"],
        repo_root=row.get("repo_root"),
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# Vocabulary CRUD
# ---------------------------------------------------------------------------


def add_vocabulary(
    workspace_id: int,
    kind_namespace: str,
    name: str,
    is_terminal: bool = False,
    is_open: bool = True,
    sort_order: int = 100,
    attributes: dict | None = None,
) -> Vocab:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO vocabularies
                (workspace_id, kind_namespace, name, is_terminal, is_open, sort_order, attributes)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (workspace_id, kind_namespace, name) DO UPDATE SET
                is_terminal = EXCLUDED.is_terminal,
                is_open = EXCLUDED.is_open,
                sort_order = EXCLUDED.sort_order,
                attributes = EXCLUDED.attributes
            RETURNING workspace_id, kind_namespace, name, is_terminal, is_open,
                      sort_order, archived, attributes
            """,
            (
                workspace_id,
                kind_namespace,
                name,
                is_terminal,
                is_open,
                sort_order,
                psycopg.types.json.Jsonb(attributes or {}),
            ),
        )
        conn.commit()
        return _row_to_vocab(cur.fetchone())


def list_vocabulary(
    workspace_id: int,
    kind_namespace: str | None = None,
    include_archived: bool = False,
) -> list[Vocab]:
    conditions = ["workspace_id = %s"]
    params: list[Any] = [workspace_id]
    if kind_namespace is not None:
        conditions.append("kind_namespace = %s")
        params.append(kind_namespace)
    if not include_archived:
        conditions.append("archived = false")
    where = " AND ".join(conditions)
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT workspace_id, kind_namespace, name, is_terminal, is_open, "
            f"sort_order, archived, attributes "
            f"FROM vocabularies WHERE {where} ORDER BY kind_namespace, sort_order, name",
            params,
        )
        return [_row_to_vocab(r) for r in cur.fetchall()]


def archive_vocabulary(workspace_id: int, kind_namespace: str, name: str) -> None:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            (
                "UPDATE vocabularies SET archived = true "
                "WHERE workspace_id = %s AND kind_namespace = %s AND name = %s"
            ),
            (workspace_id, kind_namespace, name),
        )
        conn.commit()


def delete_vocabulary(workspace_id: int, kind_namespace: str, name: str) -> None:
    """Delete a vocabulary entry after reference-checking across kind tables (decision 9).

    Phase 3: reference checks for memory_type are active.
    Phase 2a: reference checks for bc_kind/bc_status/bc_severity are active.
    Any row found → raise ValueError("vocabulary entry still referenced in <table>").
    """
    # Reference check — validates usage in breadcrumbs and memories before deletion.
    _check_vocab_references(workspace_id, kind_namespace, name)
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            (
                "DELETE FROM vocabularies "
                "WHERE workspace_id = %s AND kind_namespace = %s AND name = %s"
            ),
            (workspace_id, kind_namespace, name),
        )
        conn.commit()


def _check_vocab_references(workspace_id: int, kind_namespace: str, name: str) -> None:
    """Reference check across kind tables (decision 9)."""
    with _conn() as conn:
        cur = conn.cursor()
        if kind_namespace == "bc_kind":
            cur.execute(
                "SELECT 1 FROM breadcrumbs "
                "WHERE project_id IN (SELECT id FROM projects WHERE workspace_id = %s) "
                "AND kind = %s LIMIT 1",
                (workspace_id, name),
            )
            if cur.fetchone():
                raise ValueError(
                    "Cannot delete vocabulary entry: "
                    f"kind '{name}' is still referenced by breadcrumbs"
                )
        elif kind_namespace == "bc_status":
            cur.execute(
                "SELECT 1 FROM breadcrumbs "
                "WHERE project_id IN (SELECT id FROM projects WHERE workspace_id = %s) "
                "AND status = %s LIMIT 1",
                (workspace_id, name),
            )
            if cur.fetchone():
                raise ValueError(
                    "Cannot delete vocabulary entry: "
                    f"status '{name}' is still referenced by breadcrumbs"
                )
        elif kind_namespace == "bc_severity":
            cur.execute(
                "SELECT 1 FROM breadcrumbs "
                "WHERE project_id IN (SELECT id FROM projects WHERE workspace_id = %s) "
                "AND severity = %s LIMIT 1",
                (workspace_id, name),
            )
            if cur.fetchone():
                raise ValueError(
                    "Cannot delete vocabulary entry: "
                    f"severity '{name}' is still referenced by breadcrumbs"
                )
        elif kind_namespace == "memory_type":
            cur.execute(
                (
                    "SELECT 1 FROM memories WHERE workspace_id = %s "
                    "AND memory_type = %s AND active = true LIMIT 1"
                ),
                (workspace_id, name),
            )
            if cur.fetchone():
                raise ValueError(
                    "Cannot delete vocabulary entry: "
                    f"memory_type '{name}' is still referenced by active memories"
                )


def _row_to_vocab(row: dict) -> Vocab:
    return Vocab(
        workspace_id=row["workspace_id"],
        kind_namespace=row["kind_namespace"],
        name=row["name"],
        is_terminal=row["is_terminal"],
        is_open=row["is_open"],
        sort_order=row["sort_order"],
        archived=row["archived"],
        attributes=row.get("attributes") or {},
    )


# ---------------------------------------------------------------------------
# change_log query helper
# ---------------------------------------------------------------------------


def changes_since(
    since: datetime,
    workspace_id: int | None = None,
    project_id: int | None = None,
    kind: str | None = None,
    limit: int = 50,
) -> list[dict]:
    conditions = ["changed_at >= %s"]
    params: list[Any] = [since]
    if workspace_id is not None:
        conditions.append("workspace_id = %s")
        params.append(workspace_id)
    if project_id is not None:
        conditions.append("project_id = %s")
        params.append(project_id)
    if kind is not None:
        conditions.append("kind = %s")
        params.append(kind)
    where = " AND ".join(conditions)
    params.append(min(limit, 200))
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT kind, workspace_id, project_id, identifier, event, payload, actor, changed_at "
            f"FROM change_log WHERE {where} ORDER BY changed_at DESC LIMIT %s",
            params,
        )
        return list(cur.fetchall())
