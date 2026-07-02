"""Unit tests for the lifecycle module (Plan 013 WI-1).

Pure unit tests — no DB. These verify the static properties of the status
vocabulary: state sets, lattice ranks, transitions, and legacy synonym
mapping. The cross-layer consistency guards (module vs DB vocab, module vs
workflow YAML) live in ``test_lifecycle_consistency.py``.
"""

from __future__ import annotations

import pytest

from agent_notes.core import lifecycle

# ---------------------------------------------------------------------------
# State sets
# ---------------------------------------------------------------------------


class TestStateSets:
    def test_canonical_states_match_workflow(self):
        # The canonical states must match regista's canonical.workflow.yaml.
        assert lifecycle.CANONICAL_STATES == frozenset(
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

    def test_legacy_states(self):
        assert lifecycle.LEGACY_STATES == frozenset({"claimed", "closed"})

    def test_canonical_and_legacy_disjoint(self):
        assert lifecycle.CANONICAL_STATES.isdisjoint(lifecycle.LEGACY_STATES)

    def test_all_valid_states_is_union(self):
        assert lifecycle.ALL_VALID_STATES == (lifecycle.CANONICAL_STATES | lifecycle.LEGACY_STATES)

    def test_all_valid_states_has_nine_members(self):
        # 7 canonical + 2 legacy = 9
        assert len(lifecycle.ALL_VALID_STATES) == 9


# ---------------------------------------------------------------------------
# Terminal / open flags
# ---------------------------------------------------------------------------


class TestFlags:
    def test_terminal_states(self):
        assert lifecycle.IS_TERMINAL == frozenset({"done", "closed"})

    def test_open_states(self):
        assert lifecycle.IS_OPEN == frozenset({"open", "in_progress", "claimed"})

    def test_terminal_and_open_disjoint(self):
        assert lifecycle.IS_TERMINAL.isdisjoint(lifecycle.IS_OPEN)

    @pytest.mark.parametrize("status", ["done", "closed"])
    def test_is_terminal_true(self, status):
        assert lifecycle.is_terminal(status) is True

    @pytest.mark.parametrize(
        "status",
        ["open", "in_progress", "blocked", "deferred", "in_review", "in_human_review", "claimed"],
    )
    def test_is_terminal_false(self, status):
        assert lifecycle.is_terminal(status) is False

    @pytest.mark.parametrize("status", ["open", "in_progress", "claimed"])
    def test_is_open_true(self, status):
        assert lifecycle.is_open(status) is True

    @pytest.mark.parametrize(
        "status", ["done", "closed", "blocked", "deferred", "in_review", "in_human_review"]
    )
    def test_is_open_false(self, status):
        assert lifecycle.is_open(status) is False


# ---------------------------------------------------------------------------
# Lattice rank (Plan 013 §5 — the P0 fix)
# ---------------------------------------------------------------------------


class TestRank:
    @pytest.mark.parametrize(
        "status,expected",
        [
            ("open", 8),
            ("in_progress", 7),
            ("blocked", 7),
            ("in_review", 6),
            ("in_human_review", 5),
            ("claimed", 4),
            ("deferred", 2),
            ("done", 0),
            ("closed", 0),
        ],
    )
    def test_known_rank(self, status, expected):
        assert lifecycle.rank(status) == expected

    def test_unknown_rank_is_minus_one(self):
        assert lifecycle.rank("nonexistent") == -1

    def test_fail_safe_ordering(self):
        # More-unfinished states must rank higher (the fail-safe direction).
        # open > in_progress == blocked > in_review > in_human_review >
        # claimed > deferred > done == closed
        assert lifecycle.rank("open") > lifecycle.rank("in_progress")
        assert lifecycle.rank("in_progress") == lifecycle.rank("blocked")
        assert lifecycle.rank("blocked") > lifecycle.rank("in_review")
        assert lifecycle.rank("in_review") > lifecycle.rank("in_human_review")
        assert lifecycle.rank("in_human_review") > lifecycle.rank("claimed")
        assert lifecycle.rank("claimed") > lifecycle.rank("deferred")
        assert lifecycle.rank("deferred") > lifecycle.rank("done")
        assert lifecycle.rank("done") == lifecycle.rank("closed")

    def test_all_valid_states_have_rank(self):
        # Every valid state must have an explicit rank (not -1).
        # This is the core guarantee that fixes the P0 bug.
        for status in lifecycle.ALL_VALID_STATES:
            assert lifecycle.rank(status) != -1, f"status {status!r} has no rank"

    def test_terminal_states_rank_zero(self):
        for status in lifecycle.IS_TERMINAL:
            assert lifecycle.rank(status) == 0


# ---------------------------------------------------------------------------
# Valid transitions
# ---------------------------------------------------------------------------


class TestTransitions:
    @pytest.mark.parametrize(
        "old,new,expected",
        [
            ("open", "in_progress", "start"),
            ("deferred", "in_progress", "start"),
            ("in_progress", "blocked", "block"),
            ("blocked", "in_progress", "unblock"),
            ("open", "deferred", "defer"),
            ("in_progress", "deferred", "defer"),
            ("deferred", "open", "resume"),
            ("in_progress", "in_review", "submit_for_review"),
            ("in_review", "in_human_review", "adversarial_pass"),
            ("in_review", "in_progress", "request_changes"),
            ("in_human_review", "done", "accept"),
            ("in_human_review", "in_progress", "reject"),
            ("done", "open", "reopen"),
            ("open", "done", "close_from_open"),
        ],
    )
    def test_valid_transition(self, old, new, expected):
        assert lifecycle.transition_for(old, new) == expected

    def test_same_status_returns_none(self):
        assert lifecycle.transition_for("open", "open") is None
        assert lifecycle.transition_for("done", "done") is None

    def test_closed_alias_for_done(self):
        # closed (legacy terminal) is accepted as an alias for done.
        assert lifecycle.transition_for("closed", "open") == "reopen"
        assert lifecycle.transition_for("open", "closed") == "close_from_open"

    @pytest.mark.parametrize(
        "old,new",
        [
            # No valid transition from done to in_progress (must reopen first)
            ("done", "in_progress"),
            # No direct path from open to in_review (must start first)
            ("open", "in_review"),
            # No direct path from blocked to done
            ("blocked", "done"),
            # No path from deferred to in_review (must start/resume first)
            ("deferred", "in_review"),
            # claimed has no transitions (legacy liveness, not workflow)
            ("claimed", "open"),
            ("open", "claimed"),
        ],
    )
    def test_invalid_transition_raises(self, old, new):
        with pytest.raises(ValueError, match="Unsupported status transition"):
            lifecycle.transition_for(old, new)

    def test_all_transitions_reference_canonical_states(self):
        # Both sides of every transition must be canonical states.
        for old, new in lifecycle.VALID_TRANSITIONS:
            assert old in lifecycle.CANONICAL_STATES, f"non-canonical old: {old!r}"
            assert new in lifecycle.CANONICAL_STATES, f"non-canonical new: {new!r}"


# ---------------------------------------------------------------------------
# Legacy synonym mapping
# ---------------------------------------------------------------------------


class TestLegacyMapping:
    @pytest.mark.parametrize(
        "legacy,canonical",
        [
            ("new", "open"),
            ("active", "in_progress"),
            ("under_review", "in_review"),
            ("proposed", "open"),
            ("decision-pending", "open"),
            ("resolved", "done"),
            ("closed", "done"),
            ("implemented", "done"),
            ("accepted", "done"),
            ("wont_fix", "deferred"),
            ("wontfix", "deferred"),
            ("duplicate", "deferred"),
            ("obsolete", "deferred"),
            ("rejected", "deferred"),
        ],
    )
    def test_legacy_maps_to_canonical(self, legacy, canonical):
        assert lifecycle.map_legacy_to_canonical(legacy) == canonical

    @pytest.mark.parametrize("status", list(lifecycle.CANONICAL_STATES))
    def test_canonical_passes_through(self, status):
        assert lifecycle.map_legacy_to_canonical(status) == status

    def test_no_legacy_maps_to_claimed(self):
        # claimed is a liveness axis, not a workflow state.
        for legacy in lifecycle.LEGACY_TO_CANONICAL:
            assert lifecycle.map_legacy_to_canonical(legacy) != "claimed"

    def test_all_canonical_values_in_mapping_are_valid(self):
        for canonical in lifecycle.LEGACY_TO_CANONICAL.values():
            assert canonical in lifecycle.CANONICAL_STATES


# ---------------------------------------------------------------------------
# is_valid
# ---------------------------------------------------------------------------


class TestIsValid:
    @pytest.mark.parametrize("status", list(lifecycle.ALL_VALID_STATES))
    def test_valid_states(self, status):
        assert lifecycle.is_valid(status) is True

    def test_invalid_status(self):
        assert lifecycle.is_valid("nonexistent") is False
        assert lifecycle.is_valid("") is False
