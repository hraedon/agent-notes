"""Work-item server model — public facade (Plan 008 P0).

Mirrors the structure of `BreadcrumbModel` so the CLI can present the same
interface. Underneath, every mutation writes an op to the op-log and then
folds into the `work_items` cache. The native op-log is the only write path:
regista write-through was retired (Plan 025 D7) and deleted (Plan 026 D1).

The implementation is split across the ``core/work_item/`` subpackage:
- ``_native``   — native op-log write path.
- ``_queries``  — read-only queries, delete, diagnose.
- ``_cross_project`` — P3 request / wait / cross-project links.
- ``_common``   — shared helpers.

This module is a thin public facade: each method delegates to the subpackage.
It stays a class because the CLI, hooks, dispatcher and bridge all call
``WorkItemModel``; with one path left it holds no dispatch logic.

Public surface:
- `file_work_item` — create a new work item.
- `update_work_item` — update fields / status.
- `set_status` — dedicated status transition (delegates to update_work_item).
- `close_work_item` — complete the item (`done`, native path; Plan 025);
  `force=True` writes a terminal `close` op (admin/repair).
- `attest_gate_waiver` — record a retroactive gate waiver (Plan 014 WI-4).
- `review_transition` — drive a review-gate transition with a review note.
- `get_work_item` — read from the folded cache.
- `delete_work_item` — remove from cache.
- `query_work_items` / `find_work_items` — filtered list / semantic search.
- `suggest_duplicates` — embedding similarity check.
- `diagnose` — history + recent ops.
- `ready_work_items` / `claimable_work_items` — coordination queries.
- `claim_work_item` / `release_work_item` / `heartbeat_work_item` — lease.
- `request_work_item` / `wait_on_work_item` / `add_cross_project_link` — P3.
"""

from __future__ import annotations

from typing import Any

from agent_notes.core.lifecycle import transition_for as _lifecycle_transition_for
from agent_notes.core.work_item import _cross_project, _native, _queries


class WorkItemModel:
    """Encapsulates work-item CRUD, query, lease, and cross-project helpers.

    A facade over the ``core/work_item/`` subpackage; every method delegates
    to the native op-log path or the read-side queries.
    """

    kind = "work_item"
    entity_type = "work_item"

    # Plan 013: the transition table and resolution logic now live in
    # ``lifecycle`` (single source). ``_CANONICAL_TRANSITIONS`` is deleted;
    # callers use ``lifecycle.transition_for`` via the thin wrapper below.
    # ``closed`` (legacy terminal) is accepted as an alias for ``done``.

    _REVIEW_TRANSITION_TO_STATUS: dict[str, str] = {
        "adversarial_pass": "in_human_review",
        "accept": "done",
        "reject": "in_progress",
        "request_changes": "in_progress",
    }

    # ------------------------------------------------------------------
    # Lifecycle / vocabulary helpers (thin wrappers over single sources)
    # ------------------------------------------------------------------

    @staticmethod
    def _transition_for_status_change(old_status: str, new_status: str) -> str | None:
        return _lifecycle_transition_for(old_status, new_status)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

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
    ) -> dict:
        return _native.file_work_item(
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
            frontmatter_version,
        )

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
        force: bool = False,
    ) -> dict:
        with _native._conn() as conn:
            result = _native.update_work_item(
                conn,
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
                frontmatter_version,
                force=force,
            )
            conn.commit()
        return result

    @classmethod
    def set_status(
        cls,
        project_id: int,
        identifier: str,
        status: str,
        force: bool = False,
    ) -> dict:
        """Dedicated status transition (writes a set_status op).

        Plan 013 WI-5: validates the transition via ``lifecycle.transition_for``.
        Pass ``force=True`` to bypass the check for admin/repair overrides.
        """
        return cls.update_work_item(
            project_id=project_id,
            identifier=identifier,
            status=status,
            force=force,
        )

    @classmethod
    def close_work_item(
        cls,
        project_id: int,
        identifier: str,
        force: bool = False,
    ) -> dict:
        """Close a work item.

        ``close`` completes the item (``done``) in one transaction and
        releases any lease (Plan 025 D7). Review is an explicit workflow
        (``review_transition`` or ``status="in_review"``), not a side effect
        of closing. ``force=True`` is the admin/repair escape hatch that
        writes the legacy terminal ``close`` op.
        """
        if force:
            return _native.close_work_item_force(
                project_id,
                identifier,
            )
        return _native.close_work_item(project_id, identifier)

    @classmethod
    def attest_gate_waiver(
        cls,
        project_id: int,
        identifier: str,
        reason: str,
    ) -> dict:
        """Record that the review gate was retroactively waived (Plan 014 WI-4).

        For a degraded completion (a terminal item that reached ``done`` /
        ``closed`` without the cross-lineage review gate — e.g. a
        ``force=True`` close), an operator may record an attestation that the
        gate was waived. This writes a ``set_field`` op stamping
        ``diagnostic_keys.gate_attestation``, which the WI-3 detector
        (:func:`verifier.verify_gate_integrity`) recognizes and skips.

        The item stays terminal; the attestation makes the waiver legible in
        the op-chain rather than silently completing work. It is an
        operator-invoked action (not automatic) and operates on the local
        op-log.
        """
        return _native.attest_gate_waiver(project_id, identifier, reason)

    # ------------------------------------------------------------------
    # Review-gate transitions (adversarial_pass / accept / reject /
    # request_changes).  These carry a mandatory review_note, stored in
    # diagnostic_keys for provenance. Positive verdicts run the reviewer
    # independence check (Plan 026 D2, ``work_item/_independence.py``).
    # ------------------------------------------------------------------

    @classmethod
    def review_transition(
        cls,
        project_id: int,
        identifier: str,
        transition_name: str,
        review_note: str,
        acknowledgement: str | None = None,
        attempt_id: str | None = None,
    ) -> dict:
        """Drive a review-gate transition with a review_note payload.

        Transitions: ``adversarial_pass``, ``accept``, ``reject``,
        ``request_changes``.  The ``review_note`` is required and is stamped
        into ``diagnostic_keys.review_notes`` for provenance.

        Positive verdicts run the reviewer independence check (Plan 026 D2).
        ``acknowledgement`` is the recorded same-lineage override, and
        ``attempt_id`` applies a validated delegated review as the reviewer.
        """
        if transition_name not in cls._REVIEW_TRANSITION_TO_STATUS:
            raise ValueError(
                f"Unknown review transition: {transition_name!r}. "
                f"Valid: {', '.join(sorted(cls._REVIEW_TRANSITION_TO_STATUS))}"
            )
        if not review_note or not review_note.strip():
            raise ValueError("review_note is required for review-gate transitions")

        return _native.review_transition(
            project_id,
            identifier,
            transition_name,
            review_note,
            acknowledgement=acknowledgement,
            attempt_id=attempt_id,
        )

    # ------------------------------------------------------------------
    # Reads / delete
    # ------------------------------------------------------------------

    @classmethod
    def get_work_item(cls, project_id: int, identifier: str) -> dict | None:
        return _queries.get_work_item(project_id, identifier)

    @classmethod
    def delete_work_item(
        cls,
        project_id: int,
        identifier: str,
    ) -> bool:
        """Soft-delete via snapshot op with tombstone. Removes from cache."""
        return _native.delete_work_item(
            project_id,
            identifier,
        )

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
        return _queries.query_work_items(
            project_id,
            workspace_id,
            status,
            kind,
            severity,
            is_open,
            identifier,
            limit,
        )

    @classmethod
    def find_work_items(
        cls,
        query_vec: Any,
        project_id: int | None = None,
        workspace_id: int | None = None,
        limit: int = 10,
        query_text: str | None = None,
    ) -> list[dict]:
        """Similarity search; pass ``query_text`` so exact title matches are
        guaranteed a slot regardless of embedding rank (WI-052)."""
        return _queries.find_work_items(
            query_vec, project_id, workspace_id, limit, query_text=query_text
        )

    @classmethod
    def suggest_duplicates(
        cls,
        project_id: int,
        identifier: str,
        threshold: float = 0.95,
    ) -> list[dict]:
        return _queries.suggest_duplicates(project_id, identifier, threshold)

    @classmethod
    def diagnose(cls, project_id: int, identifier: str) -> dict:
        return _queries.diagnose(project_id, identifier, kind=cls.kind)

    @classmethod
    def get_work_item_body(cls, project_id: int, identifier: str) -> str | None:
        return _queries.get_work_item_body(project_id, identifier)

    @classmethod
    def ready_work_items(
        cls,
        project_id: int | None = None,
        workspace_id: int | None = None,
        limit: int = 50,
    ) -> list[dict]:
        return _queries.ready_work_items(project_id, workspace_id, limit)

    @classmethod
    def claimable_work_items(
        cls,
        project_id: int | None = None,
        workspace_id: int | None = None,
        limit: int = 50,
    ) -> list[dict]:
        return _queries.claimable_work_items(project_id, workspace_id, limit)

    # ------------------------------------------------------------------
    # Lease (Plan 010 WI-2)
    # ------------------------------------------------------------------

    @classmethod
    def claim_work_item(
        cls,
        project_id: int,
        identifier: str,
        ttl_seconds: int = 300,
    ) -> dict:
        """Claim a work item: a ``claim`` op plus the local lease (Plan 010 WI-2)."""
        return _native.claim_work_item(project_id, identifier, ttl_seconds)

    @classmethod
    def release_work_item(
        cls,
        project_id: int,
        identifier: str,
    ) -> dict:
        """Release a claimed work item: a ``release`` op that drops the lease."""
        return _native.release_work_item(project_id, identifier)

    @classmethod
    def heartbeat_work_item(
        cls,
        project_id: int,
        identifier: str,
        ttl_seconds: int = 300,
    ) -> dict:
        """Heartbeat a claimed work item to extend its local lease (Plan 010 WI-2)."""
        return _native.heartbeat_work_item(project_id, identifier, ttl_seconds)

    # ------------------------------------------------------------------
    # Cross-project support (P3)
    # ------------------------------------------------------------------

    @classmethod
    def request_work_item(
        cls,
        project_id: int,
        target_project_slug: str,
        title: str,
        body: str = "",
        kind: str = "task",
    ) -> dict:
        return _cross_project.request_work_item(
            project_id,
            target_project_slug,
            title,
            body,
            kind,
        )

    @classmethod
    def wait_on_work_item(
        cls,
        project_id: int,
        target_project_slug: str,
        target_identifier: str,
    ) -> dict:
        return _cross_project.wait_on_work_item(
            project_id,
            target_project_slug,
            target_identifier,
        )

    @classmethod
    def add_cross_project_link(
        cls,
        from_project_id: int,
        from_identifier: str,
        to_project_slug: str,
        to_identifier: str,
        relationship: str = "blocks",
    ) -> dict:
        return _cross_project.add_cross_project_link(
            from_project_id,
            from_identifier,
            to_project_slug,
            to_identifier,
            relationship,
        )

    @staticmethod
    def parse_address(address: str) -> tuple[str, str]:
        return _cross_project.parse_address(address)
