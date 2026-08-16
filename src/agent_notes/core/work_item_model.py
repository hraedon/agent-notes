"""Work-item server model — public facade (Plan 008 P0).

Mirrors the structure of `BreadcrumbModel` so the CLI can present the same
interface. The difference is underneath: every mutation writes an op to the
op-log and then folds into the `work_items` cache (native path) or drives the
regista face and mirrors the result (regista path).

The implementation is split across the ``core/work_item/`` subpackage:
- ``_regista``  — regista-face write path (the converged store is SoT).
- ``_native``   — native op-log write path (degrade mode).
- ``_queries``  — read-only queries, delete, diagnose.
- ``_cross_project`` — P3 request / wait / cross-project links.
- ``_common``   — shared helpers.

This module is the thin dispatch layer: each public method selects the path
based on ``face_factory.get_face()`` and delegates. Behavior is preserved
exactly from the pre-split monolith (Plan 013/014 transition checks, Plan 010
lease semantics, Plan 014 gate-parity invariants).

Public surface:
- `file_work_item` — create a new work item.
- `update_work_item` — update fields / status.
- `set_status` — dedicated status transition (delegates to update_work_item).
- `close_work_item` — defer to in_review (review gate owns completion);
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

from agent_notes.core import face_factory
from agent_notes.core.lifecycle import transition_for as _lifecycle_transition_for
from agent_notes.core.work_item import _cross_project, _native, _queries, _regista


class WorkItemModel:
    """Encapsulates work-item CRUD, query, lease, and cross-project helpers.

    This is a dispatch facade over the ``core/work_item/`` subpackage; each
    method selects the regista path (face attached) or the native op-log
    path (degrade mode).
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
        actor_id: str | None = None,
        model_lineage: str | None = None,
    ) -> dict:
        face = face_factory.get_face()
        if face is not None:
            return _regista.file_work_item(
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
                actor_id=actor_id,
                model_lineage=model_lineage,
            )
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
            actor_id,
            model_lineage=model_lineage,
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
        actor_id: str | None = None,
        model_lineage: str | None = None,
        force: bool = False,
    ) -> dict:
        face = face_factory.get_face()
        if face is not None:
            return _regista.update_work_item(
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
                actor_id=actor_id,
                model_lineage=model_lineage,
            )
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
                actor_id,
                model_lineage=model_lineage,
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
    def close_work_item(
        cls,
        project_id: int,
        identifier: str,
        actor_id: str | None = None,
        model_lineage: str | None = None,
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
        hatch: on the native path it writes the legacy terminal ``close`` op; on the
        regista path it terminalizes through the workflow (``done``) via
        :meth:`update_work_item`. Routing the face check first (matching
        :meth:`update_work_item`) matters: a regista-synced item has no native op
        chain, so the native force-close cannot fold it (it used to raise
        ``fold_work_item returned None``).
        """
        face = face_factory.get_face()
        if face is not None:
            if force:
                return cls.update_work_item(
                    project_id=project_id,
                    identifier=identifier,
                    status="done",
                    actor_id=actor_id,
                    model_lineage=model_lineage,
                    force=True,
                )
            return _regista.close_work_item(
                face,
                project_id,
                identifier,
                actor_id=actor_id,
                model_lineage=model_lineage,
            )
        if force:
            return _native.close_work_item_force(
                project_id,
                identifier,
                actor_id,
                model_lineage=model_lineage,
            )
        old = cls.get_work_item(project_id, identifier)
        if old is None:
            raise ValueError(f"Work item not found: {identifier!r} in project {project_id}")
        return _native.close_work_item_native_deferred(
            project_id,
            identifier,
            old,
            actor_id,
            model_lineage=model_lineage,
        )

    @classmethod
    def attest_gate_waiver(
        cls,
        project_id: int,
        identifier: str,
        reason: str,
        actor_id: str | None = None,
        model_lineage: str | None = None,
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
        op-log only — regista-path items (projection-only, no ops) are
        gate-verified by construction and have nothing to attest.
        """
        return _native.attest_gate_waiver(
            project_id, identifier, reason, actor_id, model_lineage=model_lineage
        )

    # ------------------------------------------------------------------
    # Review-gate transitions (adversarial_pass / accept / reject /
    # request_changes).  These carry a mandatory review_note payload that
    # regista's cross-lineage validators require.  On the native (degrade)
    # path the note is stored in diagnostic_keys for provenance; the gate
    # itself cannot run off-regista (accept is blocked by Plan 014 WI-2).
    # ------------------------------------------------------------------

    @classmethod
    def review_transition(
        cls,
        project_id: int,
        identifier: str,
        transition_name: str,
        review_note: str,
        actor_id: str | None = None,
        model_lineage: str | None = None,
        same_lineage_acknowledged: bool = False,
    ) -> dict:
        """Drive a review-gate transition with a review_note payload.

        Transitions: ``adversarial_pass``, ``accept``, ``reject``,
        ``request_changes``.  The ``review_note`` is required by regista's
        cross-lineage validators (``_review_validators.py``); without it the
        gate rejects the transition.

        On the regista path the note is threaded into the transition payload
        and the gate validators run.  On the native (degrade) path the note
        is stamped into ``diagnostic_keys.review_notes`` for provenance;
        ``accept`` is blocked (Plan 014 WI-2) because the gate cannot run
        off-regista.

        ``actor_id`` and ``model_lineage`` override the env-resolved identity
        so a subagent can declare its own lineage without env-var mutation.
        ``same_lineage_acknowledged`` passes through to the regista validator
        for the explicit same-lineage-review case.
        """
        if transition_name not in cls._REVIEW_TRANSITION_TO_STATUS:
            raise ValueError(
                f"Unknown review transition: {transition_name!r}. "
                f"Valid: {', '.join(sorted(cls._REVIEW_TRANSITION_TO_STATUS))}"
            )
        if not review_note or not review_note.strip():
            raise ValueError("review_note is required for review-gate transitions")

        face = face_factory.get_face()
        if face is not None:
            return _regista.review_transition(
                face,
                project_id,
                identifier,
                transition_name,
                review_note,
                actor_id,
                model_lineage,
                same_lineage_acknowledged,
            )
        return _native.review_transition(
            project_id,
            identifier,
            transition_name,
            review_note,
            actor_id,
            model_lineage,
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
        actor_id: str | None = None,
        model_lineage: str | None = None,
    ) -> bool:
        """Soft-delete via snapshot op with tombstone. Removes from cache."""
        if face_factory.get_face() is not None:
            existing = _queries.get_work_item(project_id, identifier)
            if existing is not None and existing.get("regista_work_item_id") is not None:
                return _queries.delete_work_item_regista(
                    project_id,
                    identifier,
                    actor_id=actor_id,
                    model_lineage=model_lineage,
                )
        return _native.delete_work_item(
            project_id,
            identifier,
            actor_id=actor_id,
            model_lineage=model_lineage,
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
        actor_id: str | None = None,
        ttl_seconds: int = 300,
        model_lineage: str | None = None,
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
            return _regista.claim_work_item(
                face,
                project_id,
                identifier,
                ttl_seconds,
                model_lineage=model_lineage,
            )
        return _native.claim_work_item(
            project_id, identifier, actor_id, ttl_seconds, model_lineage=model_lineage
        )

    @classmethod
    def release_work_item(
        cls,
        project_id: int,
        identifier: str,
        actor_id: str | None = None,
        model_lineage: str | None = None,
    ) -> dict:
        """Release a claimed work item (Plan 010 WI-2 — lease as a regista claim).

        Releases the regista claim; the lifecycle state is untouched. Legacy
        path writes a ``release`` op + ``set_status`` back to ``open``.
        """
        face = face_factory.get_face()
        if face is not None:
            return _regista.release_work_item(
                face,
                project_id,
                identifier,
                model_lineage=model_lineage,
            )
        return _native.release_work_item(
            project_id, identifier, actor_id, model_lineage=model_lineage
        )

    @classmethod
    def heartbeat_work_item(
        cls,
        project_id: int,
        identifier: str,
        actor_id: str | None = None,
        ttl_seconds: int = 300,
        model_lineage: str | None = None,
    ) -> dict:
        """Heartbeat a claimed work item to extend its lease (Plan 010 WI-2).

        Authoritative liveness is the regista claim heartbeat; the local lease
        row is a projection mirror. Legacy path touches the local lease only.
        """
        face = face_factory.get_face()
        if face is not None:
            return _regista.heartbeat_work_item(
                face,
                project_id,
                identifier,
                ttl_seconds,
                model_lineage=model_lineage,
            )
        return _native.heartbeat_work_item(
            project_id, identifier, actor_id, ttl_seconds, model_lineage=model_lineage
        )

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
        actor_id: str | None = None,
    ) -> dict:
        return _cross_project.request_work_item(
            project_id, target_project_slug, title, body, kind, actor_id
        )

    @classmethod
    def wait_on_work_item(
        cls,
        project_id: int,
        target_project_slug: str,
        target_identifier: str,
        actor_id: str | None = None,
    ) -> dict:
        return _cross_project.wait_on_work_item(
            project_id, target_project_slug, target_identifier, actor_id
        )

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
        return _cross_project.add_cross_project_link(
            from_project_id,
            from_identifier,
            to_project_slug,
            to_identifier,
            relationship,
            actor_id,
        )

    @staticmethod
    def parse_address(address: str) -> tuple[str, str]:
        return _cross_project.parse_address(address)
