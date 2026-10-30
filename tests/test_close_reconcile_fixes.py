"""Regression tests for two agent-notes defects surfaced during the
agent-suite WI-010/011/013/015/016/017 closure tranche (2026-07-25).

1. ``close --force`` writes the legacy terminal op on the native path (the
   regista-connected half of this fix went with the write-through, Plan 026).

2. ``breadcrumb reconcile --apply`` crashed the whole run on the first work
   item whose status had no direct terminal transition (e.g. ``in_review`` →
   ``closed`` is unsupported). Root cause: the per-item closure had no error
   handling. Fix: catch per-item failures, record them, and continue.
"""

from __future__ import annotations

import argparse
import json
import os
from types import SimpleNamespace

import pytest

from agent_notes.cli.breadcrumbs import cmd_bc_reconcile
from agent_notes.core import db as coredb
from agent_notes.core.work_item_model import WorkItemModel
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


@pytest.fixture
def default_project():
    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    return coredb.get_or_create_project(
        ws.id, slug="sf2", name="sf2", repo_root=os.path.abspath("/projects/sf2")
    )


def _vec768():
    return [0.0] * 768


# ── Fix 1: force-close on a regista-connected project ───────────────────


def test_force_close_native_path_still_writes_legacy_terminal(default_project, monkeypatch):
    """Force writes the terminal 'closed' op directly (Plan 014 A(b))."""
    WorkItemModel.file_work_item(
        project_id=default_project.id,
        identifier="FC-NATIVE-01",
        title="native force-close",
        status="open",
        embedding=_vec768(),
    )
    closed = WorkItemModel.close_work_item(default_project.id, "FC-NATIVE-01", force=True)
    assert closed["status"] == "closed"
    assert closed["closed_at"] is not None


# ── Fix 2: reconcile tolerates per-item closure failures ────────────────


def _ns(**kw):
    base = dict(
        workspace=None, project=None, path="/projects/x", json=True, apply=False, lookback=500
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_reconcile_apply_continues_past_an_unclosable_item(monkeypatch, capsys):
    """An in_review item (no direct terminal transition) must not abort the
    whole reconcile; it is reported as an error while closable items proceed."""
    open_wi = {"identifier": "X-1", "status": "open", "external_refs": {}}
    review_wi = {"identifier": "X-2", "status": "in_review", "external_refs": {}}

    monkeypatch.setattr(
        "agent_notes.cli.breadcrumbs._resolve",
        lambda *a, **k: (1, 1, "ws", "proj"),
    )
    monkeypatch.setattr(
        "agent_notes.core.db.list_projects",
        lambda workspace_id=None: [SimpleNamespace(id=1, repo_root="/projects/x")],
    )
    monkeypatch.setattr(
        "agent_notes.core.work_item_model.WorkItemModel.query_work_items",
        lambda **k: [open_wi, review_wi],
    )
    monkeypatch.setattr(
        "agent_notes.core.git_reconcile.scan_git_for_resolutions",
        lambda root, ids, lookback=500, project_slug=None: {
            "X-1": {"commit": "aaaaaaa", "subject": "resolve X-1"},
            "X-2": {"commit": "bbbbbbb", "subject": "resolve X-2"},
        },
    )

    calls = []

    def _fake_update(proj_id, identifier, **kw):
        calls.append(identifier)
        if identifier == "X-2":
            raise ValueError("Unsupported status transition: 'in_review' -> 'closed'")

    monkeypatch.setattr(
        "agent_notes.core.work_item_model.WorkItemModel.update_work_item",
        _fake_update,
    )

    rc = cmd_bc_reconcile(_ns(apply=True))
    # Post-eeb8eb2 (9ab81ab): apply errors now return EXIT_CONFLICT (4), not
    # EXIT_SUCCESS (0). The reconcile ran and reported per-item errors; the
    # nonzero exit signals "some items could not be auto-closed" without
    # aborting the whole run.
    assert rc == 4  # EXIT_CONFLICT
    out = json.loads(capsys.readouterr().out)
    by_id = {r["identifier"]: r for r in out["results"]}
    assert by_id["X-1"]["applied"] is True
    assert by_id["X-1"]["error"] is None
    assert by_id["X-2"]["applied"] is False
    assert "Unsupported status transition" in by_id["X-2"]["error"]
    # Both items were attempted (the failure did not abort the loop).
    assert set(calls) == {"X-1", "X-2"}


def test_reconcile_dry_run_reports_no_errors(monkeypatch, capsys):
    """Without --apply, nothing is closed and no error fields appear."""
    monkeypatch.setattr(
        "agent_notes.cli.breadcrumbs._resolve",
        lambda *a, **k: (1, 1, "ws", "proj"),
    )
    monkeypatch.setattr(
        "agent_notes.core.db.list_projects",
        lambda workspace_id=None: [SimpleNamespace(id=1, repo_root="/projects/x")],
    )
    monkeypatch.setattr(
        "agent_notes.core.work_item_model.WorkItemModel.query_work_items",
        lambda **k: [{"identifier": "X-9", "status": "in_review", "external_refs": {}}],
    )
    monkeypatch.setattr(
        "agent_notes.core.git_reconcile.scan_git_for_resolutions",
        lambda root, ids, lookback=500, project_slug=None: {
            "X-9": {"commit": "ccccccc", "subject": "s"}
        },
    )

    rc = cmd_bc_reconcile(_ns(apply=False))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["results"][0]["applied"] is False
    assert out["results"][0]["error"] is None


# ── Fix 3 (9ab81ab): memory add rejects empty body ───────────────────────


def test_mem_add_empty_body_returns_validation_error(capsys, monkeypatch):
    """cmd_mem_add with an empty --body returns a VALIDATION_FAILED envelope
    and a nonzero exit code (not a silent success or a traceback)."""
    from agent_notes.cli.memory import cmd_mem_add

    # The body check runs before scope resolution, so nothing is resolved.
    ns = argparse.Namespace(
        workspace=None,
        project=None,
        path="/projects/x",
        json=True,
        name="test-mem",
        type="note",
        body="   ",  # whitespace-only body
        attributes=None,
    )
    rc = cmd_mem_add(ns)
    assert rc != 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"]["code"] == "VALIDATION_FAILED"
