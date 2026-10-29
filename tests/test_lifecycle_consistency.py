"""Consistency guards for Plan 013 — the "can't miss a surface" guarantee.

Cross-layer checks ensure the status vocabulary stays in sync:

1. **lifecycle module vs DB wi_status vocab** (DB-backed) — every
   ``wi_status`` row's ``is_terminal``/``is_open`` must match
   ``lifecycle.IS_TERMINAL``/``IS_OPEN``, and every lifecycle state must be
   present in the vocab. This catches a missed SQL seed edit.

2. **lifecycle.VALID_TRANSITIONS vs the canonical workflow** (no DB) —
   every non-amend transition of the canonical workflow must appear in
   ``VALID_TRANSITIONS`` and vice versa. The workflow is frozen below from
   regista 0.7.2's ``canonical.workflow.yaml`` (version 3), vendored when the
   regista dependency was dropped (Plan 026 D3), so an accidental edit to the
   transition table still fails here.

These guards MUST run in the default CI job. The "can't miss a surface"
guarantee is only real if the consistency tests actually execute on every
PR (Plan 013 §0.2 / §4c).
"""

from __future__ import annotations

import os

import psycopg
import pytest
from psycopg.rows import dict_row

from agent_notes.core import lifecycle

# Import the fixture so pytest discovers it
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


# ---------------------------------------------------------------------------
# 1. lifecycle module vs DB wi_status vocab (DB-backed)
# ---------------------------------------------------------------------------


def test_lifecycle_module_matches_db_vocab():
    """Every wi_status vocab row's flags must match lifecycle, and vice versa.

    This is the core anti-drift guard: if someone adds a status to the DB
    seed (820_canonical_lifecycle.sql) but forgets to update lifecycle.py
    (or vice versa), this test fails.
    """
    dsn = os.environ["AGENT_NOTES_DSN"]
    with psycopg.connect(dsn) as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT name, is_terminal, is_open
            FROM vocabularies
            WHERE kind_namespace = 'wi_status'
            """
        )
        db_rows = {r["name"]: r for r in cur.fetchall()}

    # Every lifecycle state must be present in the DB vocab.
    for state in lifecycle.ALL_VALID_STATES:
        assert state in db_rows, (
            f"lifecycle state {state!r} is missing from the DB wi_status vocabulary"
        )

    # Every DB vocab entry's flags must match lifecycle.
    for name, row in db_rows.items():
        assert name in lifecycle.ALL_VALID_STATES, (
            f"DB wi_status entry {name!r} is not in lifecycle.ALL_VALID_STATES"
        )
        assert row["is_terminal"] == (name in lifecycle.IS_TERMINAL), (
            f"is_terminal mismatch for {name!r}: "
            f"DB={row['is_terminal']}, lifecycle={name in lifecycle.IS_TERMINAL}"
        )
        assert row["is_open"] == (name in lifecycle.IS_OPEN), (
            f"is_open mismatch for {name!r}: "
            f"DB={row['is_open']}, lifecycle={name in lifecycle.IS_OPEN}"
        )


def test_db_vocab_has_no_extra_statuses():
    """The DB wi_status vocab must not have entries unknown to lifecycle."""
    dsn = os.environ["AGENT_NOTES_DSN"]
    with psycopg.connect(dsn) as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT name FROM vocabularies WHERE kind_namespace = 'wi_status'")
        db_names = {r["name"] for r in cur.fetchall()}

    extra = db_names - lifecycle.ALL_VALID_STATES
    assert not extra, f"DB wi_status vocab has entries not in lifecycle: {sorted(extra)}"


# ---------------------------------------------------------------------------
# 2. lifecycle.VALID_TRANSITIONS vs canonical.workflow.yaml (no DB)
# ---------------------------------------------------------------------------


# regista 0.7.2 ``canonical.workflow.yaml`` (name ``canonical``, version 3),
# every transition except the ``amend`` self-transitions, which are field-only
# events rather than status changes.
_CANONICAL_WORKFLOW_TRANSITIONS: dict[tuple[str, str], str] = {
    ("open", "in_progress"): "start",
    ("deferred", "in_progress"): "start",
    ("in_progress", "blocked"): "block",
    ("blocked", "in_progress"): "unblock",
    ("open", "deferred"): "defer",
    ("in_progress", "deferred"): "defer",
    ("deferred", "open"): "resume",
    ("in_progress", "in_review"): "submit_for_review",
    ("in_review", "in_human_review"): "adversarial_pass",
    ("in_review", "in_progress"): "request_changes",
    ("in_human_review", "done"): "accept",
    ("in_human_review", "in_progress"): "reject",
    ("done", "open"): "reopen",
    ("open", "done"): "close_from_open",
}


def _extract_workflow_transitions():
    """Return (from, to) -> name for the canonical workflow's transitions."""
    return dict(_CANONICAL_WORKFLOW_TRANSITIONS)


def test_valid_transitions_match_workflow_yaml():
    """Every non-amend transition in the YAML must appear in
    VALID_TRANSITIONS and vice versa."""
    yaml_transitions = _extract_workflow_transitions()

    # Every YAML transition must be in VALID_TRANSITIONS.
    for pair, name in yaml_transitions.items():
        assert pair in lifecycle.VALID_TRANSITIONS, (
            f"workflow transition ({pair[0]!r} -> {pair[1]!r}, name={name!r}) "
            f"is missing from lifecycle.VALID_TRANSITIONS"
        )
        assert lifecycle.VALID_TRANSITIONS[pair] == name, (
            f"transition name mismatch for ({pair[0]!r} -> {pair[1]!r}): "
            f"YAML={name!r}, lifecycle={lifecycle.VALID_TRANSITIONS[pair]!r}"
        )

    # Every VALID_TRANSITIONS entry must be in the YAML.
    for pair, name in lifecycle.VALID_TRANSITIONS.items():
        assert pair in yaml_transitions, (
            f"lifecycle.VALID_TRANSITIONS entry ({pair[0]!r} -> {pair[1]!r}, "
            f"name={name!r}) is not in the canonical workflow YAML"
        )
        assert yaml_transitions[pair] == name, (
            f"transition name mismatch for ({pair[0]!r} -> {pair[1]!r}): "
            f"lifecycle={name!r}, YAML={yaml_transitions[pair]!r}"
        )


def test_valid_transitions_no_amend_entries():
    """VALID_TRANSITIONS must not contain amend (self-transitions)."""
    for old, new in lifecycle.VALID_TRANSITIONS:
        assert old != new, (
            f"VALID_TRANSITIONS contains a self-transition: "
            f"({old!r} -> {new!r}) — amend entries must be excluded"
        )


def test_valid_transitions_only_canonical_states():
    """Both sides of every transition must be canonical states (not legacy)."""
    for old, new in lifecycle.VALID_TRANSITIONS:
        assert old in lifecycle.CANONICAL_STATES, f"non-canonical 'from' state: {old!r}"
        assert new in lifecycle.CANONICAL_STATES, f"non-canonical 'to' state: {new!r}"


# ---------------------------------------------------------------------------
# 4. Lattice rank completeness (no DB)
# ---------------------------------------------------------------------------


def test_every_valid_state_has_a_rank():
    """Every ALL_VALID_STATES entry must have an explicit STATUS_RANK entry.

    This is the direct guard for the P0 bug: the old _STATUS_LATTICE
    missed canonical states, so they resolved to rank -1.
    """
    for state in lifecycle.ALL_VALID_STATES:
        assert state in lifecycle.STATUS_RANK, (
            f"state {state!r} is in ALL_VALID_STATES but has no STATUS_RANK entry "
            f"(P0 regression — would resolve to rank -1 in concurrent resolution)"
        )


def test_terminal_states_rank_zero():
    """Terminal states must have rank 0 (they are the most 'finished')."""
    for state in lifecycle.IS_TERMINAL:
        assert lifecycle.rank(state) == 0, (
            f"terminal state {state!r} has rank {lifecycle.rank(state)} (expected 0)"
        )


# ---------------------------------------------------------------------------
# 5. DB CHECK constraint vs lifecycle.ALL_VALID_STATES (DB-backed)
# ---------------------------------------------------------------------------


def test_db_check_constraint_matches_lifecycle():
    """The work_items.status CHECK constraint must accept exactly
    ALL_VALID_STATES.

    This catches a missed SQL edit: if someone adds a status to lifecycle.py
    and the wi_status vocab but forgets to update the CHECK constraint, the
    kernel will write rows the DB rejects.
    """
    dsn = os.environ["AGENT_NOTES_DSN"]
    with psycopg.connect(dsn) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conname = 'work_items_status_check'
              AND conrelid = 'work_items'::regclass
            """
        )
        row = cur.fetchone()

    assert row is not None, "work_items_status_check constraint not found"
    def_text = row[0]

    # Extract the status values from the CHECK constraint definition.
    # The definition looks like:
    #   CHECK ((status)::text = ANY (
    #     (ARRAY['open'::character varying, ...])::text[]
    #   ))
    # or:
    #   CHECK (status IN ('open', 'claimed', ...))
    import re

    quoted = re.findall(r"'([^']+)'", def_text)
    constraint_statuses = set(quoted)

    assert constraint_statuses == lifecycle.ALL_VALID_STATES, (
        f"DB CHECK constraint statuses {sorted(constraint_statuses)} != "
        f"lifecycle.ALL_VALID_STATES {sorted(lifecycle.ALL_VALID_STATES)}"
    )


# ---------------------------------------------------------------------------
# 7. bc_files._STATUS_FLAGS agrees with lifecycle for overlapping statuses
# ---------------------------------------------------------------------------


def test_bc_files_status_flags_match_lifecycle():
    """For any status in bc_files._STATUS_FLAGS that is also in
    lifecycle.ALL_VALID_STATES, the (is_open, is_terminal) flags must match.
    """
    from agent_notes.core.bc_files import _STATUS_FLAGS

    for name, (is_open, is_terminal) in _STATUS_FLAGS.items():
        if name in lifecycle.ALL_VALID_STATES:
            assert is_open == (name in lifecycle.IS_OPEN), (
                f"_STATUS_FLAGS is_open mismatch for {name!r}: "
                f"file={is_open}, lifecycle={name in lifecycle.IS_OPEN}"
            )
            assert is_terminal == (name in lifecycle.IS_TERMINAL), (
                f"_STATUS_FLAGS is_terminal mismatch for {name!r}: "
                f"file={is_terminal}, lifecycle={name in lifecycle.IS_TERMINAL}"
            )
