"""Core database layer: connection pool, models, and shared CRUD (decision 22)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

# ---------------------------------------------------------------------------
# Connection pool (decision 22: sync pool, min_size=2, max_size=5)
# ---------------------------------------------------------------------------

_DSN = os.environ.get("AGENT_NOTES_DSN", "")
_pool: ConnectionPool | None = None


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        if not _DSN:
            raise RuntimeError(
                "AGENT_NOTES_DSN environment variable is not set. "
                "Example: postgresql://user:pass@localhost/agent_notes"
            )
        _pool = ConnectionPool(
            _DSN,
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
    breadcrumbs_dir: str | None
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
    breadcrumbs_dir: str | None = None,
) -> Project:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO projects (workspace_id, slug, name, repo_root, breadcrumbs_dir)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (workspace_id, slug) DO UPDATE SET
                name = EXCLUDED.name,
                repo_root = COALESCE(EXCLUDED.repo_root, projects.repo_root),
                breadcrumbs_dir = COALESCE(EXCLUDED.breadcrumbs_dir, projects.breadcrumbs_dir)
            RETURNING id, workspace_id, slug, name, repo_root, breadcrumbs_dir, created_at
            """,
            (workspace_id, slug, name, repo_root, breadcrumbs_dir),
        )
        conn.commit()
        row = cur.fetchone()
        return _row_to_project(row)


def list_projects(workspace_id: int | None = None) -> list[Project]:
    with _conn() as conn:
        cur = conn.cursor()
        if workspace_id is not None:
            cur.execute(
                "SELECT id, workspace_id, slug, name, repo_root, breadcrumbs_dir, created_at "
                "FROM projects WHERE workspace_id = %s ORDER BY slug",
                (workspace_id,),
            )
        else:
            cur.execute(
                "SELECT id, workspace_id, slug, name, repo_root, breadcrumbs_dir, created_at "
                "FROM projects ORDER BY workspace_id, slug"
            )
        return [_row_to_project(r) for r in cur.fetchall()]


def _row_to_project(row: dict) -> Project:
    return Project(
        id=row["id"],
        workspace_id=row["workspace_id"],
        slug=row["slug"],
        name=row["name"],
        repo_root=row.get("repo_root"),
        breadcrumbs_dir=row.get("breadcrumbs_dir"),
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
            "UPDATE vocabularies SET archived = true "
            "WHERE workspace_id = %s AND kind_namespace = %s AND name = %s",
            (workspace_id, kind_namespace, name),
        )
        conn.commit()


def delete_vocabulary(workspace_id: int, kind_namespace: str, name: str) -> None:
    """Delete a vocabulary entry after reference-checking across kind tables (decision 9).

    Phase 1a stub: no kind tables exist yet. TODO: when adding kind tables in
    Phase 2a (breadcrumbs) and Phase 3 (memories), add reference checks here:
      - breadcrumbs: CHECK WHERE kind = %s OR status = %s OR severity = %s
      - memories: CHECK WHERE memory_type = %s
    Any row found → raise ValueError("vocabulary entry still referenced in <table>").
    """
    # Stub reference check — always passes in Phase 1a (no kind tables).
    _check_vocab_references(workspace_id, kind_namespace, name)
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM vocabularies "
            "WHERE workspace_id = %s AND kind_namespace = %s AND name = %s",
            (workspace_id, kind_namespace, name),
        )
        conn.commit()


def _check_vocab_references(workspace_id: int, kind_namespace: str, name: str) -> None:
    """Stub reference check (decision 9). Returns 'no references' in Phase 1a.

    TODO Phase 2a: scan breadcrumbs table.
    TODO Phase 3: scan memories table.
    TODO Phase 5+: scan reflections table if dedicated server is built.
    """
    pass  # no kind tables exist yet


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
# Links (decision 14 / 16)
# ---------------------------------------------------------------------------


def add_link(
    from_kind: str,
    from_workspace: int,
    from_project: int,
    from_identifier: str,
    to_kind: str,
    to_workspace: int,
    to_project: int,
    to_identifier: str,
    relationship: str,
) -> None:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO links
                (from_kind, from_workspace, from_project, from_identifier,
                 to_kind,   to_workspace,   to_project,   to_identifier, relationship)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                from_kind,
                from_workspace,
                from_project,
                from_identifier,
                to_kind,
                to_workspace,
                to_project,
                to_identifier,
                relationship,
            ),
        )
        conn.commit()


def remove_link(
    from_kind: str,
    from_workspace: int,
    from_project: int,
    from_identifier: str,
    to_kind: str,
    to_workspace: int,
    to_project: int,
    to_identifier: str,
    relationship: str,
) -> bool:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM links
            WHERE from_kind = %s AND from_workspace = %s AND from_project = %s
              AND from_identifier = %s AND to_kind = %s AND to_workspace = %s
              AND to_project = %s AND to_identifier = %s AND relationship = %s
            """,
            (
                from_kind,
                from_workspace,
                from_project,
                from_identifier,
                to_kind,
                to_workspace,
                to_project,
                to_identifier,
                relationship,
            ),
        )
        conn.commit()
        return cur.rowcount > 0


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
