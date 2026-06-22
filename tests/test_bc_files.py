"""Integration tests for breadcrumb file import (bc_files).

Covers WI-007: sync_breadcrumbs_from_dir must be idempotent — re-importing
already-imported files upserts instead of crashing with UniqueViolation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_notes.core import db as coredb
from agent_notes.core.bc_files import sync_breadcrumbs_from_dir
from agent_notes.core.work_item_model import WorkItemModel
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


def _fake_embed(text, task="document"):
    return [0.0] * 768


@pytest.fixture
def sync_project():
    ws = coredb.get_or_create_workspace("bcfiles-ws", "BC Files WS")
    proj = coredb.get_or_create_project(ws.id, slug="bcfiles-proj", name="BC Files Proj")
    for ns, names in {
        "wi_kind": ["todo"],
        "wi_status": ["open", "closed"],
        "wi_severity": ["medium"],
    }.items():
        for n in names:
            coredb.add_vocabulary(ws.id, ns, n)
    return proj


def _write_bc(
    path: Path,
    identifier: str,
    title: str,
    body: str = "body",
    status: str = "open",
    tags: list[str] | None = None,
) -> Path:
    tags_line = f"tags: [{', '.join(tags)}]\n" if tags else ""
    path.write_text(
        f"---\n"
        f"identifier: {identifier}\n"
        f"title: {title}\n"
        f"kind: todo\n"
        f"status: {status}\n"
        f"severity: medium\n"
        f"{tags_line}"
        f"---\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return path


def test_sync_is_idempotent_on_reimport(tmp_path, sync_project):
    _write_bc(tmp_path / "BC-001.md", "BC-001", "First breadcrumb")

    first = sync_breadcrumbs_from_dir(sync_project.id, tmp_path, _fake_embed)
    assert first["errors"] == []
    assert first["imported"] == ["BC-001"]
    assert WorkItemModel.get_work_item(sync_project.id, "BC-001") is not None

    second = sync_breadcrumbs_from_dir(sync_project.id, tmp_path, _fake_embed)
    assert second["errors"] == [], f"re-import crashed: {second['errors']!r}"
    assert second["imported"] == ["BC-001"]
    rows = WorkItemModel.query_work_items(project_id=sync_project.id, limit=100)
    assert len(rows) == 1


def test_sync_upserts_changed_body(tmp_path, sync_project):
    f = _write_bc(tmp_path / "BC-002.md", "BC-002", "Upsert bc", "original body")
    sync_breadcrumbs_from_dir(sync_project.id, tmp_path, _fake_embed)

    _write_bc(f, "BC-002", "Upsert bc", "edited body")
    second = sync_breadcrumbs_from_dir(sync_project.id, tmp_path, _fake_embed)
    assert second["errors"] == []
    body = WorkItemModel.get_work_item_body(sync_project.id, "BC-002") or ""
    assert "edited body" in body


def test_sync_merges_external_refs_preserving_out_of_band(tmp_path, sync_project):
    _write_bc(tmp_path / "BC-003.md", "BC-003", "Provenance bc", tags=["frontend"])
    sync_breadcrumbs_from_dir(sync_project.id, tmp_path, _fake_embed)

    WorkItemModel.update_work_item(
        project_id=sync_project.id,
        identifier="BC-003",
        external_refs={"resolved_by_commit": "abc123"},
    )

    second = sync_breadcrumbs_from_dir(sync_project.id, tmp_path, _fake_embed)
    assert second["errors"] == []
    refs = WorkItemModel.get_work_item(sync_project.id, "BC-003")["external_refs"]
    assert refs.get("resolved_by_commit") == "abc123"
    assert "frontend" in refs.get("tags", [])


def test_sync_does_not_reopen_closed_item(tmp_path, sync_project):
    _write_bc(tmp_path / "BC-004.md", "BC-004", "Closed bc", status="open")
    sync_breadcrumbs_from_dir(sync_project.id, tmp_path, _fake_embed)

    WorkItemModel.update_work_item(
        project_id=sync_project.id,
        identifier="BC-004",
        status="closed",
        external_refs={"resolved_by_commit": "def456"},
    )

    second = sync_breadcrumbs_from_dir(sync_project.id, tmp_path, _fake_embed)
    assert second["errors"] == []
    wi = WorkItemModel.get_work_item(sync_project.id, "BC-004")
    assert wi["status"] == "closed"
    assert wi["external_refs"].get("resolved_by_commit") == "def456"
