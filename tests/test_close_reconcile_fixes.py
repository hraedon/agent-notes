"""Regression tests for two agent-notes defects surfaced during the
agent-suite WI-010/011/013/015/016/017 closure tranche (2026-07-25).

1. ``close --force`` on a regista-connected project raised
   ``fold_work_item returned None after close op``. Root cause:
   ``close_work_item(force=True)`` routed to the native path unconditionally,
   bypassing the regista face — but regista-synced items have no native op
   chain, so the native fold returned None. Fix: route the face check first
   (matching ``update_work_item``) and terminalize through the workflow.

2. ``breadcrumb reconcile --apply`` crashed the whole run on the first work
   item whose status had no direct terminal transition (e.g. ``in_review`` →
   ``closed`` is unsupported). Root cause: the per-item closure had no error
   handling. Fix: catch per-item failures, record them, and continue.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from regista.testing import InMemoryRegista

from agent_notes.cli.breadcrumbs import cmd_bc_reconcile
from agent_notes.core import db as coredb
from agent_notes.core.face_factory import reset_face, set_face_for_test
from agent_notes.core.regista_face import RegistaFace
from agent_notes.core.work_item_model import WorkItemModel
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


@pytest.fixture
def default_project():
    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    return coredb.get_or_create_project(ws.id, slug="sf2", name="sf2", repo_root="/projects/sf2")


@pytest.fixture
def hmac_key_path(tmp_path: Path):
    path = tmp_path / "keys.json"
    path.write_text(
        json.dumps(
            {
                "keys": [
                    {
                        "key_id": "test-key-001",
                        "secret": "dGhpcyBpcyBhIHRlc3Qgc2VjcmV0IGtleSBmb3Igc3Vic3RyYXRl",
                        "status": "active",
                    }
                ]
            }
        )
    )
    return str(path)


def _vec768():
    return [0.0] * 768


# ── Fix 1: force-close on a regista-connected project ───────────────────


def test_force_close_on_regista_path_terminalizes_instead_of_fold_crash(
    default_project, hmac_key_path, monkeypatch
):
    """A regista-synced item (no native op chain) force-closes through the
    workflow instead of raising fold_work_item-returned-None."""
    monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
    monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "author-agent")
    monkeypatch.setenv("AGENT_NOTES_PRINCIPAL_ID", "author@example.com")
    monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")

    reg = InMemoryRegista(hmac_key_path=hmac_key_path)
    face = RegistaFace(reg)
    reset_face()
    set_face_for_test(face)
    try:
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="FC-REG-01",
            title="regista force-close",
            body="body",
            kind="bug",
            status="open",
            severity="medium",
            embedding=_vec768(),
        )
        row = WorkItemModel.get_work_item(default_project.id, "FC-REG-01")
        # Precondition: this is a regista-synced item (the bug class).
        assert row["regista_work_item_id"] is not None

        closed = WorkItemModel.close_work_item(
            default_project.id, "FC-REG-01", force=True
        )
        # Terminalized through the workflow (done), not a cryptic fold crash.
        assert closed["status"] in ("done", "closed")
        assert closed["closed_at"] is not None
    finally:
        reset_face()
        reg.close()


def test_force_close_native_path_still_writes_legacy_terminal(default_project):
    """The degrade (no-face) path is unchanged: force writes the terminal
    'closed' op directly (Plan 014 A(b))."""
    reset_face()
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
    base = dict(workspace=None, project=None, path="/projects/x", json=True,
                apply=False, lookback=500)
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
            raise ValueError(
                "Unsupported status transition: 'in_review' -> 'closed'"
            )

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

    monkeypatch.setattr(
        "agent_notes.cli.memory._resolve",
        lambda *a, **k: (1, 1, "ws", "proj"),
    )
    ns = argparse.Namespace(
        workspace=None, project=None, path="/projects/x",
        json=True, name="test-mem", type="note",
        body="   ",  # whitespace-only body
        attributes=None,
    )
    rc = cmd_mem_add(ns)
    assert rc != 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"]["code"] == "VALIDATION_FAILED"


# ── Fix 4 (9ab81ab): review CLI catches RegistaError ─────────────────────


def test_review_pass_cli_catches_regista_error(default_project, hmac_key_path, monkeypatch, capsys):
    """When the review gate raises RegistaError (e.g. undeclared author
    lineage), the CLI catches it and emits a VALIDATION_FAILED envelope
    instead of a traceback."""
    from agent_notes.cli.work_items import cmd_wi_review_pass

    monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
    monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "author-agent")
    monkeypatch.setenv("AGENT_NOTES_PRINCIPAL_ID", "author@example.com")
    monkeypatch.delenv("AGENT_NOTES_MODEL_LINEAGE", raising=False)

    reg = InMemoryRegista(hmac_key_path=hmac_key_path)
    face = RegistaFace(reg)
    reset_face()
    set_face_for_test(face)
    try:
        # File and drive to in_review without declaring model_lineage.
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="RV-CLI-ERR",
            title="regista error test",
            body="body",
            kind="bug",
            status="open",
            severity="medium",
            embedding=_vec768(),
        )
        WorkItemModel.update_work_item(
            project_id=default_project.id,
            identifier="RV-CLI-ERR",
            status="in_progress",
        )
        WorkItemModel.close_work_item(default_project.id, "RV-CLI-ERR")

        # The adversarial_pass should raise RegistaError (undeclared lineage).
        # The CLI must catch it and return a VALIDATION_FAILED envelope.
        ns = argparse.Namespace(
            workspace=None, project=None, path="/projects/sf2",
            json=True, identifier="RV-CLI-ERR",
            note="This should fail cleanly",
            actor_id="reviewer-kimi",
            model_lineage="kimi",
            same_lineage_acknowledged=False,
        )
        rc = cmd_wi_review_pass(ns)
        assert rc != 0, "CLI must not return exit 0 on RegistaError"
        captured = capsys.readouterr()
        # structlog may write key-loading info to stdout in the full suite.
        # The CLI error envelope is a pretty-printed JSON document (indent=2)
        # so it starts with '{\n  "ok":'. Find it by locating the last
        # '"ok":' marker and backing up to the preceding '{'.
        ok_pos = captured.out.rindex('"ok":')
        brace_pos = captured.out.rindex("{", 0, ok_pos)
        out = json.loads(captured.out[brace_pos:])
        assert out["ok"] is False
        assert out["error"]["code"] == "VALIDATION_FAILED"
        assert "Traceback" not in captured.err
    finally:
        reset_face()
        reg.close()
