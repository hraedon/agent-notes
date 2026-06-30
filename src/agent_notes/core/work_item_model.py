"""Work-item server model — encapsulates DB operations for the work-item kind (Plan 008 P0).

Mirrors the structure of `BreadcrumbModel` so the CLI can present the same
interface. The difference is underneath: every mutation writes an op to the
op-log and then folds into the `work_items` cache.

Public surface:
- `file_work_item` — create a new work item (writes `create` op + fold).
- `update_work_item` — update fields (writes `set_field` or `set_status` op + fold).
- `set_status` — dedicated status transition (writes `set_status` op + fold).
- `close_work_item` — close a work item. Defers to `in_review` (the review gate
  owns completion); `force=True` writes a terminal `close` op (admin/repair).
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

from agent_notes.core import face_factory, kernel, projection
from agent_notes.core.change_log import write_change
from agent_notes.core.db import _conn
from agent_notes.core.lifecycle import transition_for as _lifecycle_transition_for
from agent_notes.core.regista_face import normalize_source_identifier


class WorkItemModel:
    """Encapsulates work-item CRUD and query helpers."""

    kind = "work_item"
    entity_type = "work_item"

    # Plan 013: the transition table and resolution logic now live in
    # ``lifecycle`` (single source). ``_CANONICAL_TRANSITIONS`` is deleted;
    # callers use ``lifecycle.transition_for`` via the thin wrapper below.
    # ``closed`` (legacy terminal) is accepted as an alias for ``done``.

    @staticmethod
    def _transition_for_status_change(old_status: str, new_status: str) -> str | None:
        return _lifecycle_transition_for(old_status, new_status)

    @staticmethod
    def _entity_id_for_regista_create(identifier: str, regista_work_item_id: Any) -> str:
        return kernel._make_op_id(
            "work_item",
            "create",
            {"identifier": identifier, "regista_work_item_id": str(regista_work_item_id)},
            [],
        )

    @staticmethod
    def _mirror_regista_snapshot(
        conn: psycopg.Connection,
        local: dict,
        regista_work_item: Any,
        embed: Any | None = None,
        pending_sync: bool = False,
        actor_id: str | None = None,
    ) -> dict:
        def embed_fn(_text: str) -> list[float]:
            return embed

        return projection.mirror_from_regista(
            conn,
            project_id=local["project_id"],
            identifier=local["identifier"],
            entity_id=local["entity_id"],
            regista_work_item_id=regista_work_item.work_item_id,
            state=regista_work_item.current_state,
            custom_fields=dict(regista_work_item.custom_fields),
            embed=embed_fn if embed is not None else None,
            pending_sync=pending_sync,
            actor_id=actor_id,
        )

    @staticmethod
    def _file_work_item_regista(
        face: Any,
        project_id: int,
        identifier: str | None,
        title: str,
        body: str,
        kind: str,
        status: str,
        severity: str,
        external_refs: dict | None,
        diagnostic_keys: dict | None,
        embedding: Any | None,
    ) -> dict:
        with _conn() as conn:
            workspace_id = WorkItemModel._resolve_workspace_for_project(conn, project_id)
            WorkItemModel._validate_vocab(conn, workspace_id, "wi_kind", kind)
            WorkItemModel._validate_vocab(conn, workspace_id, "wi_status", status)
            WorkItemModel._validate_vocab(conn, workspace_id, "wi_severity", severity)

            if identifier is None:
                cur = conn.cursor(row_factory=psycopg.rows.tuple_row)
                cur.execute("SELECT allocate_work_item_identifier(%s)", (project_id,))
                identifier = cur.fetchone()[0]

        effective_title = title or identifier
        actor = face_factory.default_actor()
        norm_sid = normalize_source_identifier(identifier)

        # Idempotency guard (Plan 015): regista is the SoT, but the create-vs-update
        # decision in callers (e.g. bc_files.sync_breadcrumbs_from_dir) is made
        # against the *local* projection, which can be empty/stale relative to the
        # remote store (fresh session, reset local DB, per-project routing). When
        # it is, the caller routes a re-import here as a "create" even though the
        # breadcrumb already exists in regista — the original duplication bug. Look
        # the item up by normalized source_identifier first; if it exists, amend
        # fields in place rather than minting a duplicate. Status transitions are
        # intentionally NOT re-driven on this path (the live lifecycle state wins;
        # forcing a transition could violate the review gate).
        existing = face.find_by_source_identifier(norm_sid) if norm_sid is not None else None
        if existing is not None:
            wid = existing.work_item_id
            state = face.amend_breadcrumb(
                actor,
                wid,
                current_state=existing.current_state,
                title=effective_title,
                description=body or "",
                severity=severity,
                kind=kind,
                external_refs=external_refs or {},
                diagnostic_keys=diagnostic_keys or {},
            )
        else:
            wid, state = face.create_breadcrumb(
                actor,
                title=effective_title,
                description=body or "",
                severity=severity,
                kind=kind,
                external_refs=external_refs or {},
                diagnostic_keys=diagnostic_keys or {},
                source_identifier=norm_sid,
            )
            if status != "open":
                close_transition = WorkItemModel._transition_for_status_change("open", status)
                if close_transition is not None:
                    state = face.transition_breadcrumb(actor, wid, close_transition)
        entity_id = WorkItemModel._entity_id_for_regista_create(identifier, wid)
        custom_fields = {
            "title": effective_title,
            "description": body or "",
            "severity": severity,
            "kind": kind,
            "external_refs": external_refs or {},
            "diagnostic_keys": diagnostic_keys or {},
            "source_identifier": norm_sid,
        }
        with _conn() as conn:
            mirrored = projection.mirror_from_regista(
                conn,
                project_id=project_id,
                identifier=identifier,
                entity_id=entity_id,
                regista_work_item_id=wid,
                state=state,
                custom_fields=custom_fields,
                embed=(lambda _text: embedding) if embedding is not None else None,
                actor_id=actor.actor_id,
            )
            write_change(
                conn,
                kind=WorkItemModel.kind,
                workspace_id=workspace_id,
                project_id=project_id,
                identifier=identifier,
                event="filed",
                payload={"title": effective_title, "kind": kind, "status": mirrored["status"]},
                actor=actor.actor_id,
            )
            conn.commit()
        return dict(mirrored)

    @staticmethod
    def _load_work_item_row(conn: psycopg.Connection, project_id: int, identifier: str) -> dict:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT * FROM work_items WHERE project_id = %s AND identifier = %s",
            (project_id, identifier),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"Work item not found: {identifier!r} in project {project_id}")
        return dict(row)

    @staticmethod
    def _update_change_log_payload(
        old: dict,
        old_body: str,
        mirrored: dict,
        new_body: str,
        external_refs: dict | None,
        diagnostic_keys: dict | None,
    ) -> dict:
        payload: dict = {}
        for field in ("title", "kind", "status", "severity"):
            old_val = old.get(field)
            new_val = mirrored.get(field)
            if old_val != new_val:
                payload[field] = {"from": old_val, "to": new_val}
        if old_body != new_body:
            payload["body"] = {"from": old_body, "to": new_body}
        if external_refs is not None and old.get("external_refs") != external_refs:
            payload["external_refs"] = external_refs
        if diagnostic_keys is not None and old.get("diagnostic_keys") != diagnostic_keys:
            payload["diagnostic_keys"] = diagnostic_keys
        return payload

    @staticmethod
    def _update_work_item_regista(
        face: Any,
        project_id: int,
        identifier: str,
        title: str | None,
        body: str | None,
        kind: str | None,
        status: str | None,
        severity: str | None,
        external_refs: dict | None,
        diagnostic_keys: dict | None,
        embedding: Any | None,
    ) -> dict:
        with _conn() as conn:
            workspace_id = WorkItemModel._resolve_workspace_for_project(conn, project_id)
            old = WorkItemModel._load_work_item_row(conn, project_id, identifier)
            if old.get("regista_work_item_id") is None:
                raise ValueError(
                    f"Work item {identifier!r} has no regista mapping; run migrate-to-regista first"
                )
            if kind is not None:
                WorkItemModel._validate_vocab(conn, workspace_id, "wi_kind", kind)
            if status is not None:
                WorkItemModel._validate_vocab(conn, workspace_id, "wi_status", status)
            if severity is not None:
                WorkItemModel._validate_vocab(conn, workspace_id, "wi_severity", severity)
            old_body = kernel.get_blob(conn, old["body_hash"]) or ""
            wid = old["regista_work_item_id"]

        actor = face_factory.default_actor()
        custom_fields: dict[str, Any] = {}
        if title is not None:
            custom_fields["title"] = title or identifier
        if body is not None:
            custom_fields["description"] = body
        if kind is not None:
            custom_fields["kind"] = kind
        if severity is not None:
            custom_fields["severity"] = severity
        if external_refs is not None:
            custom_fields["external_refs"] = external_refs
        if diagnostic_keys is not None:
            custom_fields["diagnostic_keys"] = diagnostic_keys

        old_status = old["status"]
        new_status = status if status is not None else old_status
        transition_name = WorkItemModel._transition_for_status_change(old_status, new_status)
        if transition_name is not None:
            face.transition_breadcrumb(
                actor,
                wid,
                transition_name,
                custom_fields=custom_fields or None,
            )
        else:
            face.amend_breadcrumb(
                actor,
                wid,
                current_state=old_status,
                title=custom_fields.get("title"),
                description=custom_fields.get("description"),
                severity=custom_fields.get("severity"),
                kind=custom_fields.get("kind"),
                external_refs=custom_fields.get("external_refs"),
                diagnostic_keys=custom_fields.get("diagnostic_keys"),
            )

        regista_work_item = face.get(wid)
        with _conn() as conn:
            mirrored = WorkItemModel._mirror_regista_snapshot(
                conn,
                old,
                regista_work_item,
                embed=embedding,
                actor_id=actor.actor_id,
            )
            new_body = kernel.get_blob(conn, mirrored["body_hash"]) or ""
            payload = WorkItemModel._update_change_log_payload(
                old, old_body, mirrored, new_body, external_refs, diagnostic_keys
            )
            if payload:
                write_change(
                    conn,
                    kind=WorkItemModel.kind,
                    workspace_id=workspace_id,
                    project_id=project_id,
                    identifier=identifier,
                    event="updated",
                    payload=payload,
                    actor=actor.actor_id,
                )
            conn.commit()
        return dict(mirrored)

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
        face = face_factory.get_face()
        if face is not None:
            return cls._file_work_item_regista(
                face,
                project_id,
                identifier,
                title,
                body,
                kind,
                status,
                severity,
                external_refs,
                diagnostic_keys,
                embedding,
            )
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
        force: bool = False,
    ) -> dict:
        face = face_factory.get_face()
        if face is not None:
            return cls._update_work_item_regista(
                face,
                project_id,
                identifier,
                title,
                body,
                kind,
                status,
                severity,
                external_refs,
                diagnostic_keys,
                embedding,
            )
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
                # Plan 013 WI-5: pre-flight transition check on the native path.
                # Both paths (regista + native) now reject the same illegal
                # transitions. ``force=True`` is the explicit admin escape hatch.
                if not force:
                    transition = _lifecycle_transition_for(old["status"], status)
                    # Plan 014 WI-2: degrade mode cannot run the cross-lineage
                    # review gate, so it must not *complete* work unilaterally.
                    # The only gate transition into a terminal state is `accept`
                    # (in_human_review → done); block it off-regista. `close_from_open`
                    # (the review-exempt won't-fix/duplicate dismissal) stays allowed,
                    # matching the regista path.
                    if transition == "accept":
                        raise ValueError(
                            f"Cannot accept {identifier!r} to 'done' in degrade mode: "
                            "completion requires regista's cross-lineage review gate. "
                            "Use force=True only for admin/repair."
                        )
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

    @staticmethod
    def _close_work_item_regista(face: Any, project_id: int, identifier: str) -> dict:
        with _conn() as conn:
            workspace_id = WorkItemModel._resolve_workspace_for_project(conn, project_id)
            old = WorkItemModel._load_work_item_row(conn, project_id, identifier)
            if old.get("regista_work_item_id") is None:
                raise ValueError(
                    f"Work item {identifier!r} has no regista mapping; run migrate-to-regista first"
                )
            wid = old["regista_work_item_id"]
            state = old["status"]

        actor = face_factory.default_actor()
        # Plan 010 WI-3: close → submit_for_review → in_review. The agent cannot
        # reach `done` unilaterally (Invariant G); work awaits a cross-lineage
        # review pass + accept. `closed` (legacy terminal) is treated as `done`.
        canonical = "done" if state == "closed" else state
        if canonical in ("in_review", "in_human_review"):
            pass  # already awaiting review — no-op
        elif canonical == "done":
            raise ValueError(f"Work item {identifier!r} is already done; reopen first")
        elif canonical == "blocked":
            raise ValueError(f"Work item {identifier!r} is blocked; unblock before closing")
        elif canonical == "deferred":
            raise ValueError(f"Work item {identifier!r} is deferred; resume before closing")
        elif canonical == "claimed":
            raise ValueError(
                f"Work item {identifier!r} is in legacy 'claimed' state; "
                "migrate to the canonical workflow first"
            )
        else:  # open or in_progress
            if canonical == "open":
                face.transition_breadcrumb(actor, wid, "start")
            face.transition_breadcrumb(actor, wid, "submit_for_review")
        regista_work_item = face.get(wid)
        with _conn() as conn:
            mirrored = WorkItemModel._mirror_regista_snapshot(
                conn,
                old,
                regista_work_item,
                actor_id=actor.actor_id,
            )
            write_change(
                conn,
                kind=WorkItemModel.kind,
                workspace_id=workspace_id,
                project_id=project_id,
                identifier=identifier,
                event="status_changed",
                payload={"old_status": old["status"], "new_status": mirrored["status"]},
                actor=actor.actor_id,
            )
            conn.commit()
        return dict(mirrored)

    @classmethod
    def set_status(
        cls,
        project_id: int,
        identifier: str,
        status: str,
        actor_id: str | None = None,
        force: bool = False,
    ) -> dict:
        """Dedicated status transition (writes a set_status op).

        Plan 013 WI-5: validates the transition via ``lifecycle.transition_for``
        on the native path (same check as the regista path). Pass ``force=True``
        to bypass the check for admin/repair overrides.
        """
        return cls.update_work_item(
            project_id=project_id,
            identifier=identifier,
            status=status,
            actor_id=actor_id,
            force=force,
        )

    @classmethod
    def _close_work_item_native_deferred(
        cls, project_id: int, identifier: str, old: dict, actor_id: str | None
    ) -> dict:
        """Native (degrade) close that defers to ``in_review`` (Plan 014 A(b)).

        Mirrors ``_close_work_item_regista``'s state handling, but drives the
        op-log via ``update_work_item`` (with the Plan 013 WI-5 transition
        pre-flight) instead of a regista face. Reaching a terminal state is not
        possible here — completion requires regista's review gate.
        """
        state = old["status"]
        canonical = "done" if state == "closed" else state
        if canonical in ("in_review", "in_human_review"):
            return dict(old)  # already awaiting review — no-op
        if canonical == "done":
            raise ValueError(f"Work item {identifier!r} is already done; reopen first")
        if canonical == "blocked":
            raise ValueError(f"Work item {identifier!r} is blocked; unblock before closing")
        if canonical == "deferred":
            raise ValueError(f"Work item {identifier!r} is deferred; resume before closing")
        if canonical == "claimed":
            raise ValueError(
                f"Work item {identifier!r} is in legacy 'claimed' state; "
                "migrate to the canonical workflow first"
            )
        # open or in_progress → defer to in_review (never terminal).
        if canonical == "open":
            cls.update_work_item(
                project_id=project_id, identifier=identifier,
                status="in_progress", actor_id=actor_id,
            )
        return cls.update_work_item(
            project_id=project_id, identifier=identifier,
            status="in_review", actor_id=actor_id,
        )

    @classmethod
    def close_work_item(
        cls,
        project_id: int,
        identifier: str,
        actor_id: str | None = None,
        force: bool = False,
    ) -> dict:
        """Close a work item.

        Regista branch (Plan 010 WI-3): ``close`` → ``submit_for_review`` →
        ``in_review``. The agent cannot reach ``done`` unilaterally; work awaits a
        cross-lineage review pass + accept.

        Native / degrade branch (Plan 014, Option A(b)): ``close`` **defers** to
        ``in_review`` here too — neither path may *complete* (reach ``done``) work
        unilaterally. The review gate cannot run off-regista, so degrade-mode
        records the work as submitted and leaves completion to regista. This makes
        the two paths agree on the one provenance-critical invariant ("no
        unilateral completion") instead of the old behavior where native ``close``
        wrote a terminal op with no gate. ``force=True`` is the admin/repair escape
        hatch that writes the legacy terminal ``close`` op.
        """
        face = face_factory.get_face()
        if face is not None:
            return cls._close_work_item_regista(face, project_id, identifier)
        if not force:
            old = cls.get_work_item(project_id, identifier)
            if old is None:
                raise ValueError(f"Work item not found: {identifier!r} in project {project_id}")
            return cls._close_work_item_native_deferred(project_id, identifier, old, actor_id)
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
        face = face_factory.get_face()
        if face is not None:
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
                cur.execute(
                    "DELETE FROM work_item_leases WHERE entity_id = %s",
                    (old["entity_id"],),
                )
                cur.execute(
                    "DELETE FROM work_items WHERE project_id = %s AND identifier = %s",
                    (project_id, identifier),
                )
                write_change(
                    conn,
                    kind=cls.kind,
                    workspace_id=workspace_id,
                    project_id=project_id,
                    identifier=identifier,
                    event="deleted",
                    payload={"title": old["title"], "regista_retained": True},
                    actor=face_factory.default_actor().actor_id,
                )
                conn.commit()
            return True
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

    @staticmethod
    def _claim_work_item_regista(
        face: Any,
        project_id: int,
        identifier: str,
        ttl_seconds: int,
    ) -> dict:
        with _conn() as conn:
            WorkItemModel._resolve_workspace_for_project(conn, project_id)
            old = WorkItemModel._load_work_item_row(conn, project_id, identifier)
            if old.get("regista_work_item_id") is None:
                raise ValueError(
                    f"Work item {identifier!r} has no regista mapping; run migrate-to-regista first"
                )
            if old["status"] == "done":
                raise ValueError("Cannot claim: work item is done (terminal)")
            wid = old["regista_work_item_id"]
            entity_id = old["entity_id"]

        actor = face_factory.default_actor()
        # Plan 010 WI-2: lease is a regista claim, NOT a lifecycle state.
        # acquire_claim is the authoritative lease; the lifecycle does not move.
        claim = face.acquire_claim(actor, wid, ttl_seconds=ttl_seconds)
        regista_work_item = face.get(wid)
        with _conn() as conn:
            mirrored = WorkItemModel._mirror_regista_snapshot(
                conn,
                old,
                regista_work_item,
                actor_id=actor.actor_id,
            )
            # Mirror the claim into the local lease projection row.
            expires_at = getattr(claim, "expires_at", None)
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                """
                INSERT INTO work_item_leases (entity_id, actor_id, expires_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (entity_id) DO UPDATE SET
                    actor_id = EXCLUDED.actor_id,
                    acquired_at = now(),
                    expires_at = EXCLUDED.expires_at,
                    heartbeat_count = work_item_leases.heartbeat_count + 1
                """,
                (entity_id, actor.actor_id, expires_at),
            )
            write_change(
                conn,
                kind=WorkItemModel.kind,
                workspace_id=WorkItemModel._resolve_workspace_for_project(conn, project_id),
                project_id=project_id,
                identifier=identifier,
                event="claimed",
                payload={"actor_id": actor.actor_id, "ttl_seconds": ttl_seconds},
                actor=actor.actor_id,
            )
            conn.commit()
        return dict(mirrored)

    @classmethod
    def claim_work_item(
        cls,
        project_id: int,
        identifier: str,
        actor_id: str | None = None,
        ttl_seconds: int = 300,
    ) -> dict:
        """Claim a work item (Plan 010 WI-2 — lease as a regista claim).

        Acquires a regista claim (the authoritative lease) and mirrors it into
        the local ``work_item_leases`` projection. The lifecycle state is NOT
        moved — ``claimed`` is no longer a lifecycle state (it is a claim).
        Legacy path (no regista face) writes a ``claim`` op + ``set_status``
        to ``claimed`` unchanged.
        """
        face = face_factory.get_face()
        if face is not None:
            return cls._claim_work_item_regista(face, project_id, identifier, ttl_seconds)
        with _conn() as conn:
            cls._resolve_workspace_for_project(conn, project_id)
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT * FROM work_items WHERE project_id = %s AND identifier = %s",
                (project_id, identifier),
            )
            old = cur.fetchone()
            if old is None:
                raise ValueError(f"Work item not found: {identifier!r} in project {project_id}")

            entity_id = old["entity_id"]
            status = old["status"]

            # Only claimable items can be claimed
            if status != "open":
                raise ValueError(f"Cannot claim: status is {status!r} (must be 'open')")

            # Check if already leased
            cur.execute(
                "SELECT 1 FROM work_item_leases WHERE entity_id = %s AND expires_at > now()",
                (entity_id,),
            )
            if cur.fetchone() is not None:
                raise ValueError(f"Work item {identifier!r} is already claimed")

            # Write claim op
            op = kernel.commit_op(
                conn,
                entity_id=entity_id,
                entity_type=cls.entity_type,
                op_type="claim",
                payload={"actor_id": actor_id, "ttl_seconds": ttl_seconds},
                parent_op_ids=[entity_id],
                actor_id=actor_id,
            )

            cur.execute(
                """
                INSERT INTO work_item_leases (entity_id, actor_id, expires_at)
                VALUES (%s, %s, now() + make_interval(secs => %s))
                ON CONFLICT (entity_id) DO UPDATE SET
                    actor_id = EXCLUDED.actor_id,
                    acquired_at = now(),
                    expires_at = EXCLUDED.expires_at,
                    heartbeat_count = work_item_leases.heartbeat_count + 1
                """,
                (entity_id, actor_id or "unknown", ttl_seconds),
            )

            # Write a set_status op to move status to 'claimed'
            kernel.commit_op(
                conn,
                entity_id=entity_id,
                entity_type=cls.entity_type,
                op_type="set_status",
                payload={"status": "claimed"},
                parent_op_ids=[op["op_id"]],
                actor_id=actor_id,
            )

            kernel.emit_event(
                conn,
                op_id=op["op_id"],
                event_type="claim.granted",
                payload={"entity_id": entity_id, "identifier": identifier, "actor_id": actor_id},
            )

            # Fold into cache
            folded = kernel.fold_work_item(conn, entity_id)
            if folded is None:
                raise RuntimeError("fold_work_item returned None after claim op")

            conn.commit()
            return dict(folded)

    @staticmethod
    def _release_work_item_regista(face: Any, project_id: int, identifier: str) -> dict:
        with _conn() as conn:
            WorkItemModel._resolve_workspace_for_project(conn, project_id)
            old = WorkItemModel._load_work_item_row(conn, project_id, identifier)
            if old.get("regista_work_item_id") is None:
                raise ValueError(
                    f"Work item {identifier!r} has no regista mapping; run migrate-to-regista first"
                )
            wid = old["regista_work_item_id"]
            entity_id = old["entity_id"]

        actor = face_factory.default_actor()
        # Plan 010 WI-2: release the regista claim; the lifecycle is untouched.
        face.release_claim(actor, wid)
        regista_work_item = face.get(wid)
        with _conn() as conn:
            mirrored = WorkItemModel._mirror_regista_snapshot(
                conn,
                old,
                regista_work_item,
                actor_id=actor.actor_id,
            )
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "DELETE FROM work_item_leases WHERE entity_id = %s",
                (entity_id,),
            )
            write_change(
                conn,
                kind=WorkItemModel.kind,
                workspace_id=WorkItemModel._resolve_workspace_for_project(conn, project_id),
                project_id=project_id,
                identifier=identifier,
                event="released",
                payload={"actor_id": actor.actor_id},
                actor=actor.actor_id,
            )
            conn.commit()
        return dict(mirrored)

    @classmethod
    def release_work_item(
        cls,
        project_id: int,
        identifier: str,
        actor_id: str | None = None,
    ) -> dict:
        """Release a claimed work item (Plan 010 WI-2 — lease as a regista claim).

        Releases the regista claim; the lifecycle state is untouched. Legacy
        path writes a ``release`` op + ``set_status`` back to ``open``.
        """
        face = face_factory.get_face()
        if face is not None:
            return cls._release_work_item_regista(face, project_id, identifier)
        with _conn() as conn:
            cls._resolve_workspace_for_project(conn, project_id)
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT * FROM work_items WHERE project_id = %s AND identifier = %s",
                (project_id, identifier),
            )
            old = cur.fetchone()
            if old is None:
                raise ValueError(f"Work item not found: {identifier!r} in project {project_id}")

            entity_id = old["entity_id"]

            # Write release op
            op = kernel.commit_op(
                conn,
                entity_id=entity_id,
                entity_type=cls.entity_type,
                op_type="release",
                payload={"actor_id": actor_id},
                parent_op_ids=[entity_id],
                actor_id=actor_id,
            )

            # Delete lease
            cur.execute(
                "DELETE FROM work_item_leases WHERE entity_id = %s",
                (entity_id,),
            )

            kernel.commit_op(
                conn,
                entity_id=entity_id,
                entity_type=cls.entity_type,
                op_type="set_status",
                payload={"status": "open"},
                parent_op_ids=[op["op_id"]],
                actor_id=actor_id,
            )

            kernel.emit_event(
                conn,
                op_id=op["op_id"],
                event_type="claim.released",
                payload={"entity_id": entity_id, "identifier": identifier, "actor_id": actor_id},
            )

            folded = kernel.fold_work_item(conn, entity_id)
            if folded is None:
                raise RuntimeError("fold_work_item returned None after release op")

            conn.commit()
            return dict(folded)

    @staticmethod
    def _heartbeat_work_item_regista(
        face: Any,
        project_id: int,
        identifier: str,
        ttl_seconds: int,
    ) -> dict:
        with _conn() as conn:
            old = WorkItemModel._load_work_item_row(conn, project_id, identifier)
            entity_id = old["entity_id"]
            wid = old["regista_work_item_id"]

        actor = face_factory.default_actor()
        # Plan 010 WI-2: authoritative liveness is the regista claim heartbeat.
        claim = face.heartbeat_claim(actor, wid, ttl_seconds=ttl_seconds)
        expires_at = getattr(claim, "expires_at", None)
        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                """
                UPDATE work_item_leases
                SET expires_at = %s,
                    heartbeat_count = heartbeat_count + 1
                WHERE entity_id = %s
                RETURNING *
                """,
                (expires_at, entity_id),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"Work item {identifier!r} is not claimed — cannot heartbeat")
            conn.commit()
        return dict(row)

    @classmethod
    def heartbeat_work_item(
        cls,
        project_id: int,
        identifier: str,
        actor_id: str | None = None,
        ttl_seconds: int = 300,
    ) -> dict:
        """Heartbeat a claimed work item to extend its lease (Plan 010 WI-2).

        Authoritative liveness is the regista claim heartbeat; the local lease
        row is a projection mirror. Legacy path touches the local lease only.
        """
        face = face_factory.get_face()
        if face is not None:
            return cls._heartbeat_work_item_regista(face, project_id, identifier, ttl_seconds)
        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT * FROM work_items WHERE project_id = %s AND identifier = %s",
                (project_id, identifier),
            )
            old = cur.fetchone()
            if old is None:
                raise ValueError(f"Work item not found: {identifier!r} in project {project_id}")

            entity_id = old["entity_id"]

            cur.execute(
                """
                UPDATE work_item_leases
                SET expires_at = now() + make_interval(secs => %s),
                    heartbeat_count = heartbeat_count + 1
                WHERE entity_id = %s
                RETURNING *
                """,
                (ttl_seconds, entity_id),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"Work item {identifier!r} is not claimed — cannot heartbeat")

            op = kernel.commit_op(
                conn,
                entity_id=entity_id,
                entity_type=cls.entity_type,
                op_type="heartbeat",
                payload={"actor_id": actor_id, "ttl_seconds": ttl_seconds},
                parent_op_ids=[entity_id],
                actor_id=actor_id,
            )

            kernel.emit_event(
                conn,
                op_id=op["op_id"],
                event_type="item.heartbeat",
                payload={"entity_id": entity_id, "identifier": identifier, "actor_id": actor_id},
            )

            conn.commit()
            return dict(row)

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

        Stores the edge in ``cross_project_links`` (cross-repo / foreign
        projects). If the target project also exists in the local DB, the
        edge is mirrored in ``links`` for same-project traversal.

        The ``cross_project_links`` table stores the target by slug so it
        survives across DB instances.
        """
        with _conn() as conn:
            workspace_id = cls._resolve_workspace_for_project(conn, from_project_id)

            # Resolve the target project by slug (optional — may be foreign).
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT id, workspace_id FROM projects WHERE slug = %s",
                (to_project_slug,),
            )
            to_proj = cur.fetchone()
            to_project_id = to_proj["id"] if to_proj else None
            to_workspace_id = to_proj["workspace_id"] if to_proj else None

            # Always store in cross_project_links (foreign-safe).
            from agent_notes.core.cross_project import add_cross_repo_link

            add_cross_repo_link(
                from_project_id=from_project_id,
                from_identifier=from_identifier,
                to_project_slug=to_project_slug,
                to_identifier=to_identifier,
                relationship=relationship,
            )

            # Mirror in links if the target project is local.
            if to_project_id is not None:
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
                "to_project_slug": to_project_slug,
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
                    "to_project_slug": to_project_slug,
                    "to_identifier": to_identifier,
                    "relationship": relationship,
                    "cross_project": True,
                },
            )

            conn.commit()
            return {
                "from_project_id": from_project_id,
                "from_identifier": from_identifier,
                "to_project_id": to_project_id,
                "to_project_slug": to_project_slug,
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
