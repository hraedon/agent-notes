"""WI-4 (dossier Plan 010) — anti-drift guard for the convergence.

The convergence gap that motivated Plan 010 was the two faces (dossier,
agent-notes) registering DIFFERENT workflows, so a work-item was never shared.
This guard asserts agent-notes registers regista's single canonical workflow
*verbatim* — if anyone re-forks a local workflow, this fails. The companion
guard lives in dossier (tests/test_gateway.py).
"""

from __future__ import annotations

import regista

from agent_notes.core.regista_face import (
    WORK_ITEM_TYPE,
    WORKFLOW_NAME,
    packaged_workflow_yaml,
)


def test_face_registers_regista_canonical_verbatim():
    assert packaged_workflow_yaml() == regista.canonical_workflow_yaml()


def test_face_targets_the_canonical_workflow_name():
    assert WORKFLOW_NAME == "canonical"


def test_breadcrumb_remains_the_work_item_type():
    # The lifecycle converged; the `breadcrumb` work-item type did not change.
    assert WORK_ITEM_TYPE == "breadcrumb"
