"""Work-item server model — encapsulates DB operations for the work-item kind (Plan 008 P0).

Mirrors the structure of `BreadcrumbModel` so the CLI can present the same
interface. The difference is underneath: every mutation writes an op to the
op-log and then folds into the `work_items` cache.

Public surface:
- `file_work_item` — create a new work item (writes `create` op + fold).
- `update_work_item` — update fields (writes `set_field` or `set_status` op + fold).
- `set_status` — dedicated status transition (writes `set_status` op + fold).
- `close_work_item` — close a work item (writes `close` op + fold).
- `get_work_item` — read from the folded cache.
- `delete_work_item` — remove from cache (writes `snapshot` op with tombstone).
- `query_work_items` — filtered list from cache.
- `find_work_items` — semantic search from cache.
- `suggest_duplicates` — embedding similarity check.
- `diagnose` — history + recent ops.
- `ready_work_items` / `claimable_work_items` — coordination queries.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_notes.core import kernel
from agent_notes.core.change_log import write_change
from agent_notes.core.db import _conn


class WorkItemModel:
    """Encapsulates work-item CRUD and query helpers."""

    kind = "work_item"
    entity_type = "work_item"

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
            (
                "SELECT 1 FROM vocabularies WHERE workspace_id = %s "
                "AND kind_namespace = %s AND name = %s AND archived = false"
            ),
            (workspace_id, kind_namespace, name),
        )
        if cur.fetchone() is None:
            cur.execute(
                "SELECT name FROM vocabularies "
                "WHERE workspace_id = %s AND kind_namespace = %s AND archived = false "
                "ORDER BY sort_order",
                (workspace_id, kind_namespace),
            )
            valid = [r["name"] for r in cur.fetchall()]
            if valid:
                raise ValueError(
                    f"Unknown {kind_namespace} value: {name!r}. Valid values: {', '.join(valid)}"
                )
            else:
                raise ValueError(
                    f"Unknown {kind_namespace} value: {name!r}. "
                    f"No {kind_namespace} entries found for this workspace."
                )

    @classmethod
    def file_work_item(
        cls,
        project_id: int,
        identifier: str | None = None,
        title: str = "",
        body: str = "",
        kind: str = "todo",
        status: str = "open",
        severity: str = "medium",
        external_refs: dict | None = None,
        diagnostic_keys: dict | None = None,
        embedding: Any | None = None,
        frontmatter_version: int = 1,
        actor_id: str | None = None,
    ) -> dict:
        with _conn() as conn:
            workspace_id = cls._resolve_workspace_for_project(conn, project_id)
            cls._validate_vocab(conn, workspace_id, "wi_kind", kind)
            cls._validate_vocab(conn, workspace_id, "wi_status", status)
            cls._validate_vocab(conn, workspace_id, "wi_severity", severity)

            if identifier is None:
                cur = conn.cursor(row_factory=psycopg.rows.tuple_row)
                cur.execute("SELECT allocate_work_item_identifier(%s)", (project_id,))
                identifier = cur.fetchone()[0]

            # Store body as content-addressed blob.
            body_hash = kernel.store_blob(conn, body)

            # Build the create op payload.
            payload = {
                "project_id": project_id,
                "identifier": identifier,
                "title": title,
                "body_hash": body_hash,
                "kind": kind,
                "status": status,
                "severity": severity,
                "external_refs": external_refs or {},
                "diagnostic_keys": diagnostic_keys or {},
                "embedding": embedding,
                "frontmatter_version": frontmatter_version,
            }

            # The entity_id is the hash of the create op itself.
            entity_id = kernel._make_op_id(cls.entity_type, "create", payload, [])

            # Commit the create op.
            op = kernel.commit_op(
                conn,
                entity_id=entity_id,
                entity_type=cls.entity_type,
                op_type="create",
                payload=payload,
                actor_id=actor_id,
            )

            # Fold into cache.
            folded = kernel.fold_work_item(conn, entity_id)
            if folded is None:
                raise RuntimeError("fold_work_item returned None after create op")

            # Write change_log for backward compatibility.
            write_change(
                conn,
                kind=cls.kind,
                workspace_id=workspace_id,
                project_id=project_id,
                identifier=identifier,
                event="filed",
                payload={"title": title, "kind": kind, "status": status},
            )

            # Emit event for the post-commit hook surface.
            kernel.emit_event(
                conn,
                op_id=op["op_id"],
                event_type="item.created",
                payload={
                    "entity_id": entity_id,
                    "identifier": identifier,
                    "project_id": project_id,
                },
            )

            conn.commit()
            return dict(folded)

    @classmethod
    def update_work_item(
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
        frontmatter_version: int | None = None,
        actor_id: str | None = None,
    ) -> dict:
        with _conn() as conn:
            workspace_id = cls._resolve_workspace_for_project(conn, project_id)
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT * FROM work_items WHERE project_id = %s AND identifier = %s",
                (project_id, identifier),
            )
            old = cur.fetchone()
            if old is None:
                raise ValueError(f"Work item not found: {identifier!r} in project {project_id}")

            entity_id = old["entity_id"]
            old_body_hash = old["body_hash"]
            old_body = kernel.get_blob(conn, old_body_hash) or ""

            if kind is not None:
                cls._validate_vocab(conn, workspace_id, "wi_kind", kind)
            if status is not None:
                cls._validate_vocab(conn, workspace_id, "wi_status", status)
            if severity is not None:
                cls._validate_vocab(conn, workspace_id, "wi_severity", severity)

            # Build payload for the op.
            payload: dict = {}
            if title is not None:
                payload["title"] = title
            if body is not None:
                payload["body_hash"] = kernel.store_blob(conn, body)
            if kind is not None:
                payload["kind"] = kind
            if severity is not None:
                payload["severity"] = severity
            if external_refs is not None:
                payload["external_refs"] = external_refs
            if diagnostic_keys is not None:
                payload["diagnostic_keys"] = diagnostic_keys
            if embedding is not None:
                payload["embedding"] = embedding
            if frontmatter_version is not None:
                payload["frontmatter_version"] = frontmatter_version

            # If status changed, write a separate set_status op.
            if status is not None and status != old["status"]:
                status_op = kernel.commit_op(
                    conn,
                    entity_id=entity_id,
                    entity_type=cls.entity_type,
                    op_type="set_status",
                    payload={"status": status},
                    parent_op_ids=[old["entity_id"]],  # simplistic parent chaining
                    actor_id=actor_id,
                )
                kernel.emit_event(
                    conn,
                    op_id=status_op["op_id"],
                    event_type="item.status_changed",
                    payload={
                        "entity_id": entity_id,
                        "identifier": identifier,
                        "old_status": old["status"],
                        "new_status": status,
                    },
                )

            # Write the set_field op if any non-status fields changed.
            if payload:
                op = kernel.commit_op(
                    conn,
                    entity_id=entity_id,
                    entity_type=cls.entity_type,
                    op_type="set_field",
                    payload=payload,
                    parent_op_ids=[entity_id],
                    actor_id=actor_id,
                )
                kernel.emit_event(
                    conn,
                    op_id=op["op_id"],
                    event_type="item.updated",
                    payload={
                        "entity_id": entity_id,
                        "identifier": identifier,
                        "fields": list(payload.keys()),
                    },
                )

            # Fold into cache.
            folded = kernel.fold_work_item(conn, entity_id)
            if folded is None:
                raise RuntimeError("fold_work_item returned None after update op")

            # Backward-compat change_log.
            cl_payload: dict = {}
            for field in ("title", "body", "kind", "status", "severity"):
                old_val = old.get(field)
                if field == "body":
                    old_val = old_body
                new_val = folded.get(field)
                if old_val != new_val:
                    cl_payload[field] = {"from": old_val, "to": new_val}
            if external_refs is not None and old.get("external_refs") != external_refs:
                cl_payload["external_refs"] = external_refs
            if diagnostic_keys is not None and old.get("diagnostic_keys") != diagnostic_keys:
                cl_payload["diagnostic_keys"] = diagnostic_keys

            write_change(
                conn,
                kind=cls.kind,
                workspace_id=workspace_id,
                project_id=project_id,
                identifier=identifier,
                event="updated",
                payload=cl_payload if cl_payload else {},
            )

            conn.commit()
            return dict(folded)

    @classmethod
    def set_status(
        cls,
        project_id: int,
        identifier: str,
        status: str,
        actor_id: str | None = None,
    ) -> dict:
        """Dedicated status transition (writes a set_status op)."""
        return cls.update_work_item(
            project_id=project_id,
            identifier=identifier,
            status=status,
            actor_id=actor_id,
        )

    @classmethod
    def close_work_item(
        cls,
        project_id: int,
        identifier: str,
        actor_id: str | None = None,
    ) -> dict:
        """Close a work item (writes a close op)."""
        with _conn() as conn:
            workspace_id = cls._resolve_workspace_for_project(conn, project_id)
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT * FROM work_items WHERE project_id = %s AND identifier = %s",
                (project_id, identifier),
            )
            old = cur.fetchone()
            if old is None:
                raise ValueError(f"Work item not found: {identifier!r} in project {project_id}")

            entity_id = old["entity_id"]
            op = kernel.commit_op(
                conn,
                entity_id=entity_id,
                entity_type=cls.entity_type,
                op_type="close",
                payload={"reason": "manual_close"},
                parent_op_ids=[entity_id],
                actor_id=actor_id,
            )

            folded = kernel.fold_work_item(conn, entity_id)
            if folded is None:
                raise RuntimeError("fold_work_item returned None after close op")

            kernel.emit_event(
                conn,
                op_id=op["op_id"],
                event_type="item.closed",
                payload={"entity_id": entity_id, "identifier": identifier},
            )

            write_change(
                conn,
                kind=cls.kind,
                workspace_id=workspace_id,
                project_id=project_id,
                identifier=identifier,
                event="status_changed",
                payload={"old_status": old["status"], "new_status": "closed"},
            )

            conn.commit()
            return dict(folded)

    @classmethod
    def get_work_item(cls, project_id: int, identifier: str) -> dict | None:
        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT * FROM work_items WHERE project_id = %s AND identifier = %s",
                (project_id, identifier),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    @classmethod
    def delete_work_item(cls, project_id: int, identifier: str) -> bool:
        """Soft-delete via snapshot op with tombstone. Removes from cache."""
        with _conn() as conn:
            workspace_id = cls._resolve_workspace_for_project(conn, project_id)
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT * FROM work_items WHERE project_id = %s AND identifier = %s",
                (project_id, identifier),
            )
            old = cur.fetchone()
            if old is None:
                return False

            entity_id = old["entity_id"]
            op = kernel.commit_op(
                conn,
                entity_id=entity_id,
                entity_type=cls.entity_type,
                op_type="snapshot",
                payload={"tombstone": True},
                parent_op_ids=[entity_id],
            )

            # Delete from cache.
            cur.execute(
                "DELETE FROM work_items WHERE project_id = %s AND identifier = %s",
                (project_id, identifier),
            )

            kernel.emit_event(
                conn,
                op_id=op["op_id"],
                event_type="item.deleted",
                payload={"entity_id": entity_id, "identifier": identifier},
            )

            write_change(
                conn,
                kind=cls.kind,
                workspace_id=workspace_id,
                project_id=project_id,
                identifier=identifier,
                event="deleted",
                payload={"title": old["title"]},
            )

            conn.commit()
            return True

    @classmethod
    def query_work_items(
        cls,
        project_id: int | None = None,
        workspace_id: int | None = None,
        status: str | None = None,
        kind: str | None = None,
        severity: str | None = None,
        is_open: bool | None = None,
        identifier: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        conditions: list[str] = []
        params: list[Any] = []

        if project_id is not None:
            conditions.append("wi.project_id = %s")
            params.append(project_id)
        if workspace_id is not None:
            conditions.append(
                "EXISTS ("
                "SELECT 1 FROM projects p "
                "WHERE p.id = wi.project_id AND p.workspace_id = %s"
                ")"
            )
            params.append(workspace_id)
        if status is not None:
            conditions.append("wi.status = %s")
            params.append(status)
        if kind is not None:
            conditions.append("wi.kind = %s")
            params.append(kind)
        if severity is not None:
            conditions.append("wi.severity = %s")
            params.append(severity)
        if is_open is not None:
            if is_open:
                conditions.append("wi.closed_at IS NULL")
            else:
                conditions.append("wi.closed_at IS NOT NULL")
        if identifier is not None:
            conditions.append("wi.identifier = %s")
            params.append(identifier)

        where = " AND ".join(conditions) if conditions else "TRUE"
        params.append(min(limit, 200))

        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                f"""
                SELECT wi.*,
                       p.slug AS project_slug,
                       p.workspace_id AS workspace_id
                FROM work_items wi
                JOIN projects p ON p.id = wi.project_id
                WHERE {where}
                ORDER BY wi.updated_at DESC
                LIMIT %s
                """,
                params,
            )
            return [dict(r) for r in cur.fetchall()]

    @classmethod
    def find_work_items(
        cls,
        query_vec: Any,
        project_id: int | None = None,
        workspace_id: int | None = None,
        limit: int = 10,
    ) -> list[dict]:
        conditions = ["wi.embedding IS NOT NULL"]
        where_params: list[Any] = []

        if project_id is not None:
            conditions.append("wi.project_id = %s")
            where_params.append(project_id)
        if workspace_id is not None:
            conditions.append(
                "EXISTS ("
                "SELECT 1 FROM projects p "
                "WHERE p.id = wi.project_id AND p.workspace_id = %s"
                ")"
            )
            where_params.append(workspace_id)

        where = " AND ".join(conditions)
        limit_val = min(limit, 50)

        params: list[Any] = [query_vec] + where_params + [query_vec, limit_val]

        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                f"""
                SELECT wi.*,
                       p.slug AS project_slug,
                       p.workspace_id AS workspace_id,
                       wi.embedding <=> %s::vector AS distance
                FROM work_items wi
                JOIN projects p ON p.id = wi.project_id
                WHERE {where}
                ORDER BY wi.embedding <=> %s::vector
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
                SELECT embedding FROM work_items
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
                FROM work_items
                WHERE project_id = %s
                  AND identifier != %s
                  AND embedding IS NOT NULL
                  AND embedding <=> %s::vector <= 1 - %s
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
        wi = cls.get_work_item(project_id, identifier)
        if wi is None:
            raise ValueError(f"Work item not found: {identifier!r}")

        from agent_notes.core.change_log import history as cl_history
        from agent_notes.core.kernel import _get_entity_ops

        with _conn() as conn:
            ws_id = cls._resolve_workspace_for_project(conn, project_id)
            entity_id = wi["entity_id"]
            ops = _get_entity_ops(conn, entity_id)

        cl_rows = cl_history(cls.kind, ws_id, project_id, identifier, limit=20)
        return {
            "work_item": wi,
            "diagnostic_keys": wi.get("diagnostic_keys") or {},
            "ops": [
                {
                    "op_type": op["op_type"],
                    "lamport": op["lamport"],
                    "created_at": op["created_at"].isoformat(),
                }
                for op in ops
            ],
            "recent_changes": [
                {
                    "event": r.event,
                    "changed_at": r.changed_at.isoformat(),
                    "payload": r.payload,
                }
                for r in cl_rows
            ],
        }

    @classmethod
    def ready_work_items(
        cls,
        project_id: int | None = None,
        workspace_id: int | None = None,
        limit: int = 50,
    ) -> list[dict]:
        return kernel.ready_work_items(project_id, workspace_id, limit)

    @classmethod
    def claimable_work_items(
        cls,
        project_id: int | None = None,
        workspace_id: int | None = None,
        limit: int = 50,
    ) -> list[dict]:
        return kernel.claimable_work_items(project_id, workspace_id, limit)

    @classmethod
    def get_work_item_body(cls, project_id: int, identifier: str) -> str | None:
        """Return the body text for a work item by looking up its blob."""
        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT body_hash FROM work_items WHERE project_id = %s AND identifier = %s",
                (project_id, identifier),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return kernel.get_blob(conn, row["body_hash"])

    # -----------------------------------------------------------------------
    # Cross-project support (P3)
    # -----------------------------------------------------------------------

    @classmethod
    def request_work_item(
        cls,
        project_id: int,
        target_project_slug: str,
        title: str,
        body: str = "",
        kind: str = "task",
        actor_id: str | None = None,
    ) -> dict:
        """Request a new work item in a target project (cross-project, P3).

        Writes a ``request`` op in the **dependent's** (local) log. The
        target project's intake process will materialize a real work item
        from this request. The request is signed evidence; the target project
        owns the created item.
        """
        with _conn() as conn:
            workspace_id = cls._resolve_workspace_for_project(conn, project_id)
            cls._validate_vocab(conn, workspace_id, "wi_kind", kind)

            # Generate a local identifier for the request.
            cur = conn.cursor(row_factory=psycopg.rows.tuple_row)
            cur.execute("SELECT allocate_work_item_identifier(%s)", (project_id,))
            identifier = cur.fetchone()[0]

            body_hash = kernel.store_blob(conn, body)
            payload = {
                "project_id": project_id,
                "identifier": identifier,
                "title": title,
                "body_hash": body_hash,
                "kind": kind,
                "status": "open",
                "severity": "medium",
                "external_refs": {},
                "diagnostic_keys": {},
                "target_project": target_project_slug,
                "request_type": "create_work_item",
            }

            entity_id = kernel._make_op_id(cls.entity_type, "request", payload, [])
            op = kernel.commit_op(
                conn,
                entity_id=entity_id,
                entity_type=cls.entity_type,
                op_type="request",
                payload=payload,
                actor_id=actor_id,
            )

            # Request entities don't go into the work_items cache (they're
            # not real work items — they're signed evidence). They live only
            # in the op_log. The target project B creates a real work item
            # from this request via its intake process.
            kernel.emit_event(
                conn,
                op_id=op["op_id"],
                event_type="request.created",
                payload={
                    "entity_id": entity_id,
                    "identifier": identifier,
                    "project_id": project_id,
                    "target_project": target_project_slug,
                },
            )

            write_change(
                conn,
                kind=cls.kind,
                workspace_id=workspace_id,
                project_id=project_id,
                identifier=identifier,
                event="filed",
                payload={"title": title, "kind": kind, "request_type": "create_work_item"},
            )

            conn.commit()
            return {
                "entity_id": entity_id,
                "identifier": identifier,
                "project_id": project_id,
                "title": title,
                "kind": kind,
                "target_project": target_project_slug,
                "request_type": "create_work_item",
            }

    @classmethod
    def wait_on_work_item(
        cls,
        project_id: int,
        target_project_slug: str,
        target_identifier: str,
        actor_id: str | None = None,
    ) -> dict:
        """Register a wait on a target project's work item (cross-project, P3).

        Writes a ``wait`` op in the local log. This signals that the local
        session is blocked until the target item resolves. The wake system
        will resume the session when the target closes.
        """
        with _conn() as conn:
            workspace_id = cls._resolve_workspace_for_project(conn, project_id)

            # Generate a local identifier for the wait record.
            cur = conn.cursor(row_factory=psycopg.rows.tuple_row)
            cur.execute("SELECT allocate_work_item_identifier(%s)", (project_id,))
            identifier = cur.fetchone()[0]

            payload = {
                "project_id": project_id,
                "identifier": identifier,
                "title": f"Wait on {target_project_slug}:{target_identifier}",
                "target_project": target_project_slug,
                "target_identifier": target_identifier,
                "wait_type": "block_on_work_item",
                "status": "waiting",
            }

            entity_id = kernel._make_op_id(cls.entity_type, "wait", payload, [])
            op = kernel.commit_op(
                conn,
                entity_id=entity_id,
                entity_type=cls.entity_type,
                op_type="wait",
                payload=payload,
                actor_id=actor_id,
            )

            # Wait records don't go into the work_items cache. They live only
            # in the op_log as signed evidence of the wait intent.
            kernel.emit_event(
                conn,
                op_id=op["op_id"],
                event_type="wait.registered",
                payload={
                    "entity_id": entity_id,
                    "identifier": identifier,
                    "project_id": project_id,
                    "target_project": target_project_slug,
                    "target_identifier": target_identifier,
                },
            )

            write_change(
                conn,
                kind=cls.kind,
                workspace_id=workspace_id,
                project_id=project_id,
                identifier=identifier,
                event="filed",
                payload={
                    "title": payload["title"],
                    "kind": "wait",
                    "wait_type": "block_on_work_item",
                },
            )

            conn.commit()
            return {
                "entity_id": entity_id,
                "identifier": identifier,
                "project_id": project_id,
                "title": payload["title"],
                "kind": "wait",
                "target_project": target_project_slug,
                "target_identifier": target_identifier,
                "wait_type": "block_on_work_item",
            }

    @classmethod
    def add_cross_project_link(
        cls,
        from_project_id: int,
        from_identifier: str,
        to_project_slug: str,
        to_identifier: str,
        relationship: str = "blocks",
        actor_id: str | None = None,
    ) -> dict:
        """Add a cross-project link (P3).

        The link is stored in the local ``links`` table with the target
        project identified by slug. The derived index resolves the slug
        to the actual project_id at query time.
        """
        with _conn() as conn:
            workspace_id = cls._resolve_workspace_for_project(conn, from_project_id)

            # Resolve the target project by slug.
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT id, workspace_id FROM projects WHERE slug = %s",
                (to_project_slug,),
            )
            to_proj = cur.fetchone()
            if to_proj is None:
                raise ValueError(f"Target project not found: {to_project_slug!r}")

            to_project_id = to_proj["id"]
            to_workspace_id = to_proj["workspace_id"]

            from agent_notes.core.links import add_link

            add_link(
                from_kind="work_item",
                from_workspace=workspace_id,
                from_project=from_project_id,
                from_identifier=from_identifier,
                to_kind="work_item",
                to_workspace=to_workspace_id,
                to_project=to_project_id,
                to_identifier=to_identifier,
                relationship=relationship,
            )

            # Write a link op to the op_log for provenance.
            payload = {
                "from_project_id": from_project_id,
                "from_identifier": from_identifier,
                "to_project_id": to_project_id,
                "to_identifier": to_identifier,
                "relationship": relationship,
                "cross_project": True,
            }

            entity_id = kernel._make_op_id(cls.entity_type, "add_link", payload, [])
            op = kernel.commit_op(
                conn,
                entity_id=entity_id,
                entity_type=cls.entity_type,
                op_type="add_link",
                payload=payload,
                actor_id=actor_id,
            )

            kernel.emit_event(
                conn,
                op_id=op["op_id"],
                event_type="link.added",
                payload={
                    "from_project_id": from_project_id,
                    "from_identifier": from_identifier,
                    "to_project_id": to_project_id,
                    "to_identifier": to_identifier,
                    "relationship": relationship,
                },
            )

            conn.commit()
            return {
                "from_project_id": from_project_id,
                "from_identifier": from_identifier,
                "to_project_id": to_project_id,
                "to_identifier": to_identifier,
                "relationship": relationship,
            }

    @staticmethod
    def parse_address(address: str) -> tuple[str, str]:
        """Parse a ``project:identifier`` address (P3).

        Returns ``(project_slug, identifier)``. Raises ValueError if malformed.
        """
        if ":" not in address:
            raise ValueError(f"Invalid address: {address!r}. Expected format: project:identifier")
        parts = address.split(":", 1)
        return parts[0], parts[1]
