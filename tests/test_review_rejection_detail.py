"""Regression tests for surfacing review-rejection detail (WI-050).

The Plan 020 Linux qualification hit a cross-lineage gate rejection whose
message listed three possible causes without saying which applied, while the
``detail`` dict that named the actual cause (``agent_author_undeclared=True``)
was discarded: the CLI envelope printed ``"detail": null``. regista's
``ReviewRejected`` carries the facts (``actor_id``, ``reviewer_lineage``,
``author_lineages``, ...); the hook runner wraps it in a ``RegistaError`` whose
own ``detail`` is ``None`` but whose ``__cause__`` is the original exception.

These tests need no database: the review command handlers are driven with the
model layer stubbed to raise exactly the exception shape regista produces.
"""

from __future__ import annotations

import argparse
import json

import pytest
from regista._errors import ErrorCode, RegistaError
from regista._review_validators import ReviewRejected

from agent_notes.cli import work_items
from agent_notes.cli.common import exception_detail
from agent_notes.core.work_item_model import WorkItemModel

_DETAIL = {
    "actor_id": "agent-qual",
    "reviewer_lineage": None,
    "author_lineages": ["claude"],
    "agent_author_undeclared": True,
}


def _wrapped_rejection() -> RegistaError:
    """Build the exception exactly as regista's hook runner raises it.

    ``_hooks.py`` wraps any validator exception via ``raise RegistaError(...)
    from e`` without forwarding ``e.detail`` — so the facts survive only on
    ``__cause__``.
    """
    try:
        try:
            raise ReviewRejected(
                "adversarial_review: the reviewer's model lineage is not "
                "confirmed distinct from an author",
                detail=dict(_DETAIL),
            )
        except ReviewRejected as e:
            raise RegistaError(
                ErrorCode.VALIDATOR_FAILED,
                f"Validator 'adversarial_review' failed: {e}",
            ) from e
    except RegistaError as wrapped:
        return wrapped


class TestExceptionDetail:
    def test_finds_detail_on_the_cause(self):
        assert exception_detail(_wrapped_rejection()) == _DETAIL

    def test_finds_detail_on_the_exception_itself(self):
        exc = RegistaError(ErrorCode.VALIDATOR_FAILED, "boom", detail={"k": "v"})
        assert exception_detail(exc) == {"k": "v"}

    def test_no_detail_anywhere_returns_none(self):
        assert exception_detail(ValueError("plain")) is None

    def test_empty_detail_dict_is_treated_as_absent(self):
        exc = ReviewRejected("no facts", detail={})
        assert exception_detail(exc) is None


@pytest.fixture
def _review_cmd(monkeypatch):
    """Stub resolution and make the model raise the wrapped rejection."""

    def _configure(use_json: bool) -> argparse.Namespace:
        monkeypatch.setattr(
            work_items, "_review_resolve", lambda args: (1, "proj", use_json)
        )

        def _raise(cls, *args, **kwargs):
            raise _wrapped_rejection()

        monkeypatch.setattr(
            WorkItemModel, "review_transition", classmethod(_raise)
        )
        return argparse.Namespace(
            identifier="QL-E2E-1", note="looks wrong", json=use_json
        )

    return _configure


def test_json_envelope_carries_the_rejection_detail(_review_cmd, capsys):
    """--json: error.detail carries the full dict (WI-050).

    The envelope schema types ``error.detail`` as string-or-null, so the dict
    arrives JSON-serialized in that slot — but every fact is present and
    machine-recoverable, instead of ``"detail": null``.
    """
    args = _review_cmd(use_json=True)
    rc = work_items.cmd_wi_review_pass(args)
    assert rc != 0

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "VALIDATION_FAILED"
    detail = envelope["error"]["detail"]
    assert isinstance(detail, str), "envelope contract: detail is string-or-null"
    assert json.loads(detail) == _DETAIL


def test_human_output_names_the_rejection_facts(_review_cmd, capsys):
    """Text mode: the facts print to stderr, one key per line."""
    args = _review_cmd(use_json=False)
    rc = work_items.cmd_wi_review_pass(args)
    assert rc != 0

    err = capsys.readouterr().err
    assert "agent_author_undeclared: true" in err
    assert "author_lineages" in err
    assert "reviewer_lineage" in err


@pytest.mark.parametrize(
    "command",
    [
        work_items.cmd_wi_review_accept,
        work_items.cmd_wi_review_reject,
        work_items.cmd_wi_review_request_changes,
    ],
)
def test_every_review_transition_surfaces_detail(_review_cmd, capsys, command):
    args = _review_cmd(use_json=True)
    rc = command(args)
    assert rc != 0
    envelope = json.loads(capsys.readouterr().out)
    assert json.loads(envelope["error"]["detail"]) == _DETAIL
