"""Breadcrumb server model — encapsulates DB operations for the breadcrumb kind (Phase 2a).

Mirrors the structure of `agent_notes.servers.memory.MemoryModel` so both
kind servers follow the same pattern: model handles raw SQL, server layer
handles JSON-RPC schema + embedding.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_notes.core.change_log import write_change
from agent_notes.core.db import _conn


class BreadcrumbModel:
    """Encapsulates breadcrumb CRUD and query helpers."""

    kind = "breadcrumb"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_workspace_for_project(conn: psycopg.Connection, project_id: int) -> int:
        cur = conn.cursor()
        cur.execute("SELECT workspace_id FROM projects WHERE id = %s", (project_id,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"Project {project_id} not found")
        return row["workspace_id"]

    @staticmethod
    def _validate_vocab(
        conn: psycopg.Connection,
        workspace_id: int,
        kind_namespace: str,
        name: str,
    ) -> None:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM vocabularies "
            "WHERE workspace_id = %s AND kind_namespace = %s AND name = %s",
            (workspace_id, kind_namespace, name),
        )
        if cur.fetchone() is None:
            raise ValueError(f"Unknown {kind_namespace} value: {name!r}")

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    @classmethod
    def file_breadcrumb(
        cls,
        project_id: int,
        title: str,
        identifier: str | None = None,
        body: str = "",
        kind: str = "todo",
        status: str = "new",
        severity: str = "medium",
        external_refs: dict | None = None,
        diagnostic_keys: dict | None = None,
        embedding: Any | None = None,
        file_path: str | None = None,
        frontmatter_version: int = 1,
    ) -> dict:
        with _conn() as conn:
            workspace_id = cls._resolve_workspace_for_project(conn, project_id)
            cls._validate_vocab(conn, workspace_id, "bc_kind", kind)
            cls._validate_vocab(conn, workspace_id, "bc_status", status)
            cls._validate_vocab(conn, workspace_id, "bc_severity", severity)

            # Auto-allocate identifier if not provided (legacy parity).
            if identifier is None:
                cur = conn.cursor(row_factory=psycopg.rows.tuple_row)
                cur.execute("SELECT allocate_bc_identifier(%s)", (project_id,))
                identifier = cur.fetchone()[0]

            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                """
                INSERT INTO breadcrumbs
                    (project_id, identifier, title, body, kind, status, severity,
                     external_refs, diagnostic_keys, embedding, file_path, frontmatter_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (project_id, identifier) DO UPDATE SET
                    title           = EXCLUDED.title,
                    body            = EXCLUDED.body,
                    kind            = EXCLUDED.kind,
                    status          = EXCLUDED.status,
                    severity        = EXCLUDED.severity,
                    external_refs   = EXCLUDED.external_refs,
                    diagnostic_keys = EXCLUDED.diagnostic_keys,
                    embedding       = EXCLUDED.embedding,
                    file_path       = COALESCE(EXCLUDED.file_path, breadcrumbs.file_path),
                    frontmatter_version = EXCLUDED.frontmatter_version,
                    updated_at      = now()
                RETURNING *
                """,
                (
                    project_id,
                    identifier,
                    title,
                    body,
                    kind,
                    status,
                    severity,
                    psycopg.types.json.Jsonb(external_refs or {}),
                    psycopg.types.json.Jsonb(diagnostic_keys or {}),
                    embedding,
                    file_path,
                    frontmatter_version,
                ),
            )
            row = cur.fetchone()
            write_change(
                conn,
                kind=cls.kind,
                workspace_id=workspace_id,
                project_id=project_id,
                identifier=identifier,
                event="filed",
                payload={"title": title, "kind": kind, "status": status},
            )
            conn.commit()
            return dict(row)

    @classmethod
    def update_breadcrumb(
        cls,
        project_id: int,
        identifier: str,
        title: str | None = None,
        body: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        severity: str | None = None,
        external_refs: dict | None = None,
        diagnostic_keys: dict | None = None,
        embedding: Any | None = None,
        file_path: str | None = None,
        frontmatter_version: int | None = None,
    ) -> dict:
        with _conn() as conn:
            workspace_id = cls._resolve_workspace_for_project(conn, project_id)
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT * FROM breadcrumbs WHERE project_id = %s AND identifier = %s",
                (project_id, identifier),
            )
            old = cur.fetchone()
            if old is None:
                raise ValueError(f"Breadcrumb not found: {identifier!r} in project {project_id}")
            if kind is not None:
                cls._validate_vocab(conn, workspace_id, "bc_kind", kind)
            if status is not None:
                cls._validate_vocab(conn, workspace_id, "bc_status", status)
            if severity is not None:
                cls._validate_vocab(conn, workspace_id, "bc_severity", severity)

            sets = ["updated_at = now()"]
            params: list[Any] = []
            if title is not None:
                sets.append("title = %s")
                params.append(title)
            if body is not None:
                sets.append("body = %s")
                params.append(body)
            if kind is not None:
                sets.append("kind = %s")
                params.append(kind)
            if status is not None:
                sets.append("status = %s")
                params.append(status)
            if severity is not None:
                sets.append("severity = %s")
                params.append(severity)
            if external_refs is not None:
                sets.append("external_refs = %s")
                params.append(psycopg.types.json.Jsonb(external_refs))
            if diagnostic_keys is not None:
                sets.append("diagnostic_keys = %s")
                params.append(psycopg.types.json.Jsonb(diagnostic_keys))
            if embedding is not None:
                sets.append("embedding = %s")
                params.append(embedding)
            if file_path is not None:
                sets.append("file_path = %s")
                params.append(file_path)
            if frontmatter_version is not None:
                sets.append("frontmatter_version = %s")
                params.append(frontmatter_version)

            sql = (
                f"UPDATE breadcrumbs SET {', '.join(sets)} "
                f"WHERE project_id = %s AND identifier = %s RETURNING *"
            )
            params.extend([project_id, identifier])
            cur.execute(sql, params)
            row = cur.fetchone()

            payload: dict = {}
            for field in ("title", "body", "kind", "status", "severity"):
                old_val = old.get(field)
                new_val = row.get(field)
                if old_val != new_val:
                    payload[field] = {"from": old_val, "to": new_val}
            if external_refs is not None and old.get("external_refs") != external_refs:
                payload["external_refs"] = external_refs
            if diagnostic_keys is not None and old.get("diagnostic_keys") != diagnostic_keys:
                payload["diagnostic_keys"] = diagnostic_keys

            write_change(
                conn,
                kind=cls.kind,
                workspace_id=workspace_id,
                project_id=project_id,
                identifier=identifier,
                event="updated",
                payload=payload if payload else {},
            )
            conn.commit()
            return dict(row)

    @classmethod
    def get_breadcrumb(cls, project_id: int, identifier: str) -> dict | None:
        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT * FROM breadcrumbs WHERE project_id = %s AND identifier = %s",
                (project_id, identifier),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    @classmethod
    def delete_breadcrumb(cls, project_id: int, identifier: str) -> bool:
        with _conn() as conn:
            workspace_id = cls._resolve_workspace_for_project(conn, project_id)
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "DELETE FROM breadcrumbs WHERE project_id = %s AND identifier = %s RETURNING *",
                (project_id, identifier),
            )
            row = cur.fetchone()
            if row is None:
                return False
            write_change(
                conn,
                kind=cls.kind,
                workspace_id=workspace_id,
                project_id=project_id,
                identifier=identifier,
                event="deleted",
                payload={"title": row["title"]},
            )
            conn.commit()
            return True

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @classmethod
    def query_breadcrumbs(
        cls,
        project_id: int | None = None,
        workspace_id: int | None = None,
        status: str | None = None,
        kind: str | None = None,
        severity: str | None = None,
        is_open: bool | None = None,
        limit: int = 50,
    ) -> list[dict]:
        conditions: list[str] = []
        params: list[Any] = []

        if project_id is not None:
            conditions.append("b.project_id = %s")
            params.append(project_id)
        if workspace_id is not None:
            conditions.append(
                "EXISTS (SELECT 1 FROM projects p "
                "WHERE p.id = b.project_id AND p.workspace_id = %s)"
            )
            params.append(workspace_id)
        if status is not None:
            conditions.append("b.status = %s")
            params.append(status)
        if kind is not None:
            conditions.append("b.kind = %s")
            params.append(kind)
        if severity is not None:
            conditions.append("b.severity = %s")
            params.append(severity)
        if is_open is not None:
            if is_open:
                conditions.append("b.closed_at IS NULL")
            else:
                conditions.append("b.closed_at IS NOT NULL")

        where = " AND ".join(conditions) if conditions else "TRUE"
        params.append(min(limit, 200))

        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                f"""
                SELECT b.*,
                       p.slug AS project_slug,
                       p.workspace_id AS workspace_id
                FROM breadcrumbs b
                JOIN projects p ON p.id = b.project_id
                WHERE {where}
                ORDER BY b.updated_at DESC
                LIMIT %s
                """,
                params,
            )
            return [dict(r) for r in cur.fetchall()]

    @classmethod
    def find_breadcrumbs(
        cls,
        query_vec: Any,
        project_id: int | None = None,
        workspace_id: int | None = None,
        limit: int = 10,
    ) -> list[dict]:
        conditions = ["b.embedding IS NOT NULL"]
        params: list[Any] = []

        if project_id is not None:
            conditions.append("b.project_id = %s")
            params.append(project_id)
        if workspace_id is not None:
            conditions.append(
                "EXISTS (SELECT 1 FROM projects p "
                "WHERE p.id = b.project_id AND p.workspace_id = %s)"
            )
            params.append(workspace_id)

        where = " AND ".join(conditions)
        params.append(query_vec)
        params.append(min(limit, 50))

        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                f"""
                SELECT b.*,
                       p.slug AS project_slug,
                       p.workspace_id AS workspace_id,
                       b.embedding <=> %s AS distance
                FROM breadcrumbs b
                JOIN projects p ON p.id = b.project_id
                WHERE {where}
                ORDER BY b.embedding <=> %s
                LIMIT %s
                """,
                params,
            )
            return [dict(r) for r in cur.fetchall()]

    @classmethod
    def suggest_duplicates(
        cls,
        project_id: int,
        identifier: str,
        threshold: float = 0.95,
    ) -> list[dict]:
        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                """
                SELECT embedding FROM breadcrumbs
                WHERE project_id = %s AND identifier = %s AND embedding IS NOT NULL
                """,
                (project_id, identifier),
            )
            row = cur.fetchone()
            if row is None or row["embedding"] is None:
                return []

            vec = row["embedding"]
            cur.execute(
                """
                SELECT identifier, title, status,
                       1 - (embedding <=> %s::vector) AS similarity
                FROM breadcrumbs
                WHERE project_id = %s
                  AND identifier != %s
                  AND embedding IS NOT NULL
                  AND 1 - (embedding <=> %s::vector) >= %s
                ORDER BY similarity DESC
                LIMIT 10
                """,
                (vec, project_id, identifier, vec, threshold),
            )
            return [dict(r) for r in cur.fetchall()]

    @classmethod
    def diagnose(
        cls,
        project_id: int,
        identifier: str,
    ) -> dict:
        bc = cls.get_breadcrumb(project_id, identifier)
        if bc is None:
            raise ValueError(f"Breadcrumb not found: {identifier!r}")

        from agent_notes.core.change_log import history as cl_history

        ws_id = cls._resolve_workspace_for_project(
            None,
            project_id,  # type: ignore[arg-type]
        )
        rows = cl_history(cls.kind, ws_id, project_id, identifier, limit=20)
        return {
            "breadcrumb": bc,
            "diagnostic_keys": bc.get("diagnostic_keys") or {},
            "recent_changes": [
                {
                    "event": r.event,
                    "changed_at": r.changed_at.isoformat(),
                    "payload": r.payload,
                }
                for r in rows
            ],
        }

    @classmethod
    def audit(cls, project_id: int | None = None) -> list[dict]:
        conditions = ["projection_dirty = true"]
        params: list[Any] = []
        if project_id is not None:
            conditions.append("project_id = %s")
            params.append(project_id)
        where = " AND ".join(conditions)
        params.append(200)

        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                f"""
                SELECT project_id, identifier, title,
                       status, file_path, projection_dirty, updated_at
                FROM breadcrumbs
                WHERE {where}
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                params,
            )
            return [dict(r) for r in cur.fetchall()]

    @classmethod
    def compute_projection_paths(
        cls,
        project_id: int,
        identifier: str,
        target_status: str | None = None,
    ) -> dict:
        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                """
                SELECT b.file_path, b.status, p.breadcrumbs_dir, p.repo_root
                FROM breadcrumbs b
                JOIN projects p ON p.id = b.project_id
                WHERE b.project_id = %s AND b.identifier = %s
                """,
                (project_id, identifier),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"Breadcrumb not found: {identifier!r}")

        status = target_status if target_status else row["status"]
        status_dir = _status_to_dir(status)
        new_file_path = f"{status_dir}/{identifier}.md"

        breadcrumbs_dir = (row["breadcrumbs_dir"] or "").strip("/")
        repo_root = row["repo_root"]

        old_file_path = row["file_path"] or new_file_path

        def _abs(fp: str) -> str:
            if repo_root:
                return f"{repo_root}/{breadcrumbs_dir}/{fp}".replace("//", "/")
            return f"/{breadcrumbs_dir}/{fp}".replace("//", "/")

        def _repo_rel(fp: str) -> str:
            return f"{breadcrumbs_dir}/{fp}".strip("/")

        return {
            "old_absolute": _abs(old_file_path),
            "new_absolute": _abs(new_file_path),
            "old_repo_relative": _repo_rel(old_file_path),
            "new_repo_relative": _repo_rel(new_file_path),
        }


def _status_to_dir(status: str) -> str:
    terminal_dirs = {"resolved", "closed", "wont_fix", "duplicate"}
    return "resolved" if status in terminal_dirs else "active"
