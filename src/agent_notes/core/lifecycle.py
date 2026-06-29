"""Single source for work-item status vocabulary properties (Plan 013).

The status vocabulary is defined in three cross-checked layers:

1. **regista's ``canonical.workflow.yaml``** — the workflow authority (valid
   transitions, states). Shipped from the ``regista`` package.
2. **The DB ``wi_status`` vocabulary** — runtime flags (``is_terminal`` /
   ``is_open``) per status, seeded by ``820_canonical_lifecycle.sql``.
3. **This module** — static properties importable without a DB round-trip:
   the state sets, lattice rank, valid transitions, and legacy synonyms.

The three layers are reconciled by tests (``test_lifecycle.py`` and
``test_lifecycle_consistency.py``), not unified. Adding a state is three
coordinated edits (YAML + SQL seed + this module); a missed surface fails
a consistency test rather than shipping silently.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# State sets
# ---------------------------------------------------------------------------

# The canonical v2 lifecycle states (regista canonical.workflow.yaml).
CANONICAL_STATES = frozenset(
    {
        "open",
        "in_progress",
        "blocked",
        "deferred",
        "in_review",
        "in_human_review",
        "done",
    }
)

# Legacy breadcrumb states (v1) — valid for pre-migration items and op-log
# history. ``claimed`` is a liveness/lease axis, not a workflow state, but it
# remains a valid status value for items written before the convergence.
LEGACY_STATES = frozenset({"claimed", "closed"})

# Union of canonical + legacy. Drives ``verifier._VALID_STATUS`` and the
# ``work_items_status_check`` DB constraint.
ALL_VALID_STATES = CANONICAL_STATES | LEGACY_STATES

# ---------------------------------------------------------------------------
# Terminal / open flags (mirrors the DB wi_status vocabulary)
# ---------------------------------------------------------------------------

# Terminal statuses — no further lifecycle transitions except ``reopen``.
# Mirrors ``is_terminal = true`` in 820_canonical_lifecycle.sql.
IS_TERMINAL = frozenset({"done", "closed"})

# Open statuses — ``closed_at`` is NULL for these. Mirrors ``is_open = true``
# in 820_canonical_lifecycle.sql.
IS_OPEN = frozenset({"open", "in_progress", "claimed"})

# ---------------------------------------------------------------------------
# Lattice rank (concurrent-op resolution, Plan 013 §5)
# ---------------------------------------------------------------------------

# Fail-safe principle: in a concurrent conflict, the more-unfinished state
# wins (surface work, never silently complete it). Higher rank = more
# unfinished. Ties break by lexicographic ``op_id`` (deterministic).
#
# ``claimed`` sits below the active lifecycle states (a lease should not
# override real lifecycle progress) but above terminal. See Plan 013 §5
# for the rationale and the cross-axis ``claimed`` review scope.
STATUS_RANK: dict[str, int] = {
    "open": 8,
    "in_progress": 7,
    "blocked": 7,
    "in_review": 6,
    "in_human_review": 5,
    "claimed": 4,
    "deferred": 2,
    "done": 0,
    "closed": 0,
}

# ---------------------------------------------------------------------------
# Valid transitions (agent-side mirror of canonical.workflow.yaml)
# ---------------------------------------------------------------------------

# Maps (old, new) canonical states to the workflow transition name.
# This is the agent-side mirror used for pre-flight validation; the regista
# YAML remains the workflow authority. ``closed`` (legacy terminal) is
# accepted as an alias for ``done`` (see ``transition_for``).
#
# ``amend`` self-transitions are intentionally excluded — they are field-only
# events that do not change lifecycle state, so they are not status transitions.
VALID_TRANSITIONS: dict[tuple[str, str], str] = {
    # Active work
    ("open", "in_progress"): "start",
    ("deferred", "in_progress"): "start",
    # Blocked (external dependency)
    ("in_progress", "blocked"): "block",
    ("blocked", "in_progress"): "unblock",
    # Deferred (deliberate park, no dependency)
    ("open", "deferred"): "defer",
    ("in_progress", "deferred"): "defer",
    ("deferred", "open"): "resume",
    # Two-stage review gate
    ("in_progress", "in_review"): "submit_for_review",
    ("in_review", "in_human_review"): "adversarial_pass",
    ("in_review", "in_progress"): "request_changes",
    ("in_human_review", "done"): "accept",
    ("in_human_review", "in_progress"): "reject",
    # Reopen
    ("done", "open"): "reopen",
    # Review-exempt dismissal (won't-fix/duplicate)
    ("open", "done"): "close_from_open",
}

# ---------------------------------------------------------------------------
# Legacy synonym → canonical mapping
# ---------------------------------------------------------------------------

# Legacy bc_status synonyms → canonical lifecycle equivalent. Canonical
# states pass through (not listed here; callers use ``.get(status, status)``).
# ``claimed`` is a liveness axis, not a workflow state — no entry maps TO it.
LEGACY_TO_CANONICAL: dict[str, str] = {
    # Legacy synonyms → canonical equivalent
    "new": "open",
    "active": "in_progress",
    "under_review": "in_review",
    "proposed": "open",
    "decision-pending": "open",
    # Legacy terminals → canonical terminal (``done``)
    "resolved": "done",
    "closed": "done",
    "implemented": "done",
    "accepted": "done",
    # Deferred synonyms
    "wont_fix": "deferred",
    "wontfix": "deferred",
    "duplicate": "deferred",
    "obsolete": "deferred",
    "rejected": "deferred",
}

# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def rank(status: str) -> int:
    """Return the lattice rank for ``status`` (higher = more unfinished).

    Unknown statuses return ``-1`` (loses to every known status in a
    concurrent conflict). This is the fail-safe default: an unknown state
    is treated as less unfinished than any known state.
    """
    return STATUS_RANK.get(status, -1)


def is_valid(status: str) -> bool:
    """Return ``True`` if ``status`` is a known canonical or legacy state."""
    return status in ALL_VALID_STATES


def is_terminal(status: str) -> bool:
    """Return ``True`` if ``status`` is terminal (no forward transitions)."""
    return status in IS_TERMINAL


def is_open(status: str) -> bool:
    """Return ``True`` if ``status`` is open (``closed_at`` is NULL)."""
    return status in IS_OPEN


def map_legacy_to_canonical(status: str) -> str:
    """Map a legacy bc_status synonym to its canonical equivalent.

    Canonical states and unknown statuses pass through unchanged. This is
    the single replacement for ``cli/breadcrumbs._BC_STATUS_TO_WI`` and
    ``core/bc_files._map_bc_status_to_wi``.
    """
    return LEGACY_TO_CANONICAL.get(status, status)


def transition_for(old_status: str, new_status: str) -> str | None:
    """Return the workflow transition name for a status change.

    Returns ``None`` if ``old_status == new_status`` (no transition needed).
    Returns the transition name if the change is valid.
    Raises ``ValueError`` if the transition is unsupported.

    ``closed`` (legacy terminal) is accepted as an alias for ``done``.
    ``claimed`` (legacy liveness state) has no valid transitions — it
    requires migration to the canonical workflow first.
    """
    if old_status == new_status:
        return None
    old = "done" if old_status == "closed" else old_status
    new = "done" if new_status == "closed" else new_status
    transition = VALID_TRANSITIONS.get((old, new))
    if transition is not None:
        return transition
    raise ValueError(
        f"Unsupported status transition: {old_status!r} -> {new_status!r}"
    )
