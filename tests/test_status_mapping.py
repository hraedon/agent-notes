"""Regression tests for bc_status → wi_status mapping (lifecycle bug fix).

Before the fix, the legacy breadcrumb vocabulary map intercepted the canonical
lifecycle states (`in_progress`, `blocked`) and remapped them to `claimed`
(a liveness/lease axis, not a workflow state). This made the canonical lifecycle
unreachable: an item could never enter `in_progress`, so `submit_for_review`
blocked and agents fell back to appending body notes. `claimed` must never be
emitted by a --status value (canonical.workflow.yaml lines 30-31, Plan 010 WI-2).
"""

from __future__ import annotations

from agent_notes.cli.breadcrumbs import _map_status
from agent_notes.core.bc_files import _map_bc_status_to_wi

_CANONICAL_STATES = (
    "open",
    "in_progress",
    "blocked",
    "deferred",
    "in_review",
    "in_human_review",
    "done",
)


def test_canonical_states_pass_through_cli():
    for s in _CANONICAL_STATES:
        assert _map_status(s) == s, f"canonical state {s!r} was remapped"


def test_canonical_states_pass_through_files():
    for s in _CANONICAL_STATES:
        assert _map_bc_status_to_wi(s) == s, f"canonical state {s!r} was remapped"


def test_no_status_maps_to_claimed():
    # `claimed` is a liveness axis, not a lifecycle state.
    legacy_inputs = (
        "in_progress",
        "blocked",
        "under_review",
        "active",
        "new",
        "open",
        "proposed",
        "resolved",
        "closed",
    )
    for s in legacy_inputs:
        assert _map_status(s) != "claimed", f"{s!r} mapped to claimed"
        assert _map_bc_status_to_wi(s) != "claimed", f"{s!r} mapped to claimed"


def test_legacy_synonyms_map_to_canonical_equivalent():
    cases = {
        "new": "open",
        "active": "in_progress",
        "under_review": "in_review",
        "proposed": "open",
        "decision-pending": "open",
        "resolved": "done",
        "closed": "done",
        "implemented": "done",
        "accepted": "done",
        "wont_fix": "deferred",
        "duplicate": "deferred",
        "obsolete": "deferred",
        "rejected": "deferred",
    }
    for legacy, canonical in cases.items():
        assert _map_status(legacy) == canonical
        assert _map_bc_status_to_wi(legacy) == canonical


def test_bc_files_unknown_status_defaults_to_open():
    # C1 fix: unknown statuses (not canonical, not a known legacy synonym)
    # must default to "open" in the file-import path, preserving v1 ingest
    # semantics. Without this, an on-disk status like "stabilized" would
    # pass through and fail wi_status vocab validation.
    assert _map_bc_status_to_wi("stabilized") == "open"
    assert _map_bc_status_to_wi("unknown_status") == "open"
    assert _map_bc_status_to_wi("") == "open"


def test_cli_unknown_status_passes_through():
    # The CLI path (--status) passes unknown statuses through unchanged
    # (different from the file-import path which defaults to "open").
    # This is intentional: the CLI validates against wi_status vocab, so an
    # invalid status will be caught by _validate_vocab.
    assert _map_status("stabilized") == "stabilized"
