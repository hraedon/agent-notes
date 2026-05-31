"""Integration tests for Phase 2a — breadcrumbs server (DB-only, no projection)."""

from __future__ import annotations

import pytest

from agent_notes.core import db as coredb
from agent_notes.core.bc_files import parse_breadcrumb_file, sync_breadcrumbs_from_dir
from agent_notes.core.breadcrumbs_model import BreadcrumbModel

# Import the fixture so pytest discovers it
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


@pytest.fixture
def default_project():
    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    proj = coredb.get_or_create_project(
        ws.id,
        slug="sf2",
        name="sf2",
        repo_root="/projects/sf2",
    )
    return proj


def _vec768():
    """Return a dummy 768-dim numpy-compatible vector (float list)."""
    return [0.0] * 768


# ---------------------------------------------------------------------------
# file_breadcrumb / get_breadcrumb
# ---------------------------------------------------------------------------


def test_file_breadcrumb_roundtrip(default_project):
    bc = BreadcrumbModel.file_breadcrumb(
        project_id=default_project.id,
        identifier="BC-001",
        title="Test BC",
        body="Body text",
        kind="bug",
        status="new",
        severity="high",
        embedding=_vec768(),
    )
    assert bc["identifier"] == "BC-001"
    assert bc["title"] == "Test BC"
    assert bc["status"] == "new"

    fetched = BreadcrumbModel.get_breadcrumb(default_project.id, "BC-001")
    assert fetched is not None
    assert fetched["title"] == "Test BC"


def test_file_breadcrumb_unknown_vocab(default_project):
    with pytest.raises(ValueError, match="Unknown bc_status"):
        BreadcrumbModel.file_breadcrumb(
            project_id=default_project.id,
            identifier="BC-002",
            title="Bad status",
            status="nonexistent",
            embedding=_vec768(),
        )


# ---------------------------------------------------------------------------
# update_breadcrumb + status trigger
# ---------------------------------------------------------------------------


def test_update_status_to_terminal_sets_closed_at(default_project):
    BreadcrumbModel.file_breadcrumb(
        project_id=default_project.id,
        identifier="BC-003",
        title="To be resolved",
        status="new",
        embedding=_vec768(),
    )
    updated = BreadcrumbModel.update_breadcrumb(
        project_id=default_project.id,
        identifier="BC-003",
        status="resolved",
    )
    assert updated["status"] == "resolved"
    assert updated["closed_at"] is not None


def test_update_status_away_from_terminal_clears_closed_at(default_project):
    BreadcrumbModel.file_breadcrumb(
        project_id=default_project.id,
        identifier="BC-004",
        title="Re-open me",
        status="closed",
        embedding=_vec768(),
    )
    # closed is terminal → closed_at set by trigger
    bc = BreadcrumbModel.get_breadcrumb(default_project.id, "BC-004")
    assert bc["closed_at"] is not None

    updated = BreadcrumbModel.update_breadcrumb(
        project_id=default_project.id,
        identifier="BC-004",
        status="open",
    )
    assert updated["status"] == "open"
    assert updated["closed_at"] is None


# ---------------------------------------------------------------------------
# query_breadcrumbs
# ---------------------------------------------------------------------------


def test_query_by_status(default_project):
    BreadcrumbModel.file_breadcrumb(
        project_id=default_project.id,
        identifier="BC-005",
        title="Open one",
        status="open",
        embedding=_vec768(),
    )
    BreadcrumbModel.file_breadcrumb(
        project_id=default_project.id,
        identifier="BC-006",
        title="Resolved one",
        status="resolved",
        embedding=_vec768(),
    )
    rows = BreadcrumbModel.query_breadcrumbs(project_id=default_project.id, status="resolved")
    identifiers = {r["identifier"] for r in rows}
    assert "BC-006" in identifiers


def test_query_open_filter(default_project):
    BreadcrumbModel.file_breadcrumb(
        project_id=default_project.id,
        identifier="BC-007",
        title="Open",
        status="open",
        embedding=_vec768(),
    )
    BreadcrumbModel.file_breadcrumb(
        project_id=default_project.id,
        identifier="BC-008",
        title="Closed",
        status="closed",
        embedding=_vec768(),
    )
    open_rows = BreadcrumbModel.query_breadcrumbs(project_id=default_project.id, is_open=True)
    closed_rows = BreadcrumbModel.query_breadcrumbs(project_id=default_project.id, is_open=False)
    assert any(r["identifier"] == "BC-007" for r in open_rows)
    assert any(r["identifier"] == "BC-008" for r in closed_rows)


# ---------------------------------------------------------------------------
# change_log
# ---------------------------------------------------------------------------


def test_change_log_written_on_file(default_project):
    BreadcrumbModel.file_breadcrumb(
        project_id=default_project.id,
        identifier="BC-009",
        title="Audit me",
        status="new",
        embedding=_vec768(),
    )
    from agent_notes.core.change_log import history

    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    rows = history("breadcrumb", ws.id, default_project.id, "BC-009")
    assert len(rows) >= 1
    assert rows[-1].event == "filed"


def test_change_log_written_on_status_change(default_project):
    BreadcrumbModel.file_breadcrumb(
        project_id=default_project.id,
        identifier="BC-010",
        title="Status audit",
        status="new",
        embedding=_vec768(),
    )
    BreadcrumbModel.update_breadcrumb(
        project_id=default_project.id,
        identifier="BC-010",
        status="resolved",
    )
    from agent_notes.core.change_log import history

    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    rows = history("breadcrumb", ws.id, default_project.id, "BC-010")
    events = [r.event for r in rows]
    assert "filed" in events
    assert "status_changed" in events


# ---------------------------------------------------------------------------
# suggest_duplicates
# ---------------------------------------------------------------------------


def test_suggest_duplicates_no_embedding(default_project):
    BreadcrumbModel.file_breadcrumb(
        project_id=default_project.id,
        identifier="BC-014",
        title="No embed",
        status="new",
        # leave embedding NULL
    )
    dups = BreadcrumbModel.suggest_duplicates(default_project.id, "BC-014")
    assert dups == []


# ---------------------------------------------------------------------------
# Vocabulary reference check
# ---------------------------------------------------------------------------


def test_delete_vocab_referenced_by_bc(default_project):
    BreadcrumbModel.file_breadcrumb(
        project_id=default_project.id,
        identifier="BC-015",
        title="Ref guard",
        kind="bug",
        status="new",
        embedding=_vec768(),
    )
    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    with pytest.raises(ValueError, match="still referenced"):
        coredb.delete_vocabulary(ws.id, "bc_kind", "bug")
    with pytest.raises(ValueError, match="still referenced"):
        coredb.delete_vocabulary(ws.id, "bc_status", "new")


# ---------------------------------------------------------------------------
# Server tool wiring (smoke)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# find_breadcrumbs (BC-007 regression: parametric placeholder/param tests)
# ---------------------------------------------------------------------------


def _setup_two_bcs(default_project):
    """File two breadcrumbs with distinct titles for find_breadcrumbs tests."""
    BreadcrumbModel.file_breadcrumb(
        project_id=default_project.id,
        identifier="BC-FIND-01",
        title="First searchable breadcrumb about pooling",
        status="open",
        embedding=[float(i) for i in range(768)],
    )
    BreadcrumbModel.file_breadcrumb(
        project_id=default_project.id,
        identifier="BC-FIND-02",
        title="Second searchable breadcrumb about vectors",
        status="open",
        # Different vector to ensure a deterministic ordering.
        embedding=[float(i + 100) for i in range(768)],
    )


# ---------------------------------------------------------------------------
# files -> DB importer (Plan 007: retire md-as-source-of-truth)
# ---------------------------------------------------------------------------


def test_parse_breadcrumb_file_skips_non_breadcrumb(tmp_path):
    (tmp_path / "README.md").write_text("# Breadcrumbs\n\nIndex, not a breadcrumb.\n")
    assert parse_breadcrumb_file(tmp_path / "README.md") is None
    (tmp_path / "001-x.md").write_text(
        '---\nnumber: "001"\ntitle: Foo\nkind: bug\n---\n\nBody.\n'
    )
    parsed = parse_breadcrumb_file(tmp_path / "001-x.md")
    assert parsed["identifier"] == "001" and parsed["title"] == "Foo"


def test_sync_imports_skips_and_is_idempotent(default_project, tmp_path):
    (tmp_path / "001-foo.md").write_text(
        '---\nnumber: "001"\ntitle: First\nkind: bug\nstatus: new\nseverity: high\n---\n\nBody one.\n'
    )
    (tmp_path / "README.md").write_text("# Breadcrumbs\n\nNot a breadcrumb.\n")
    s = sync_breadcrumbs_from_dir(default_project.id, tmp_path, lambda t: _vec768())
    assert s["imported"] == ["001"]
    assert any("README.md" in x["file"] for x in s["skipped"])
    assert not s["errors"] and not s["missing_vocab"]
    assert BreadcrumbModel.get_breadcrumb(default_project.id, "001")["title"] == "First"

    # Re-running upserts (no duplicate, title updated).
    (tmp_path / "001-foo.md").write_text(
        '---\nnumber: "001"\ntitle: First (edited)\nkind: bug\nstatus: new\nseverity: high\n---\n\nB.\n'
    )
    s2 = sync_breadcrumbs_from_dir(default_project.id, tmp_path, lambda t: _vec768())
    assert s2["imported"] == ["001"]
    assert BreadcrumbModel.get_breadcrumb(default_project.id, "001")["title"] == "First (edited)"


def test_sync_missing_vocab_reported_then_created(default_project, tmp_path):
    (tmp_path / "010-x.md").write_text(
        '---\nnumber: "010"\ntitle: Design item\nkind: design\nstatus: proposed\nseverity: high\n---\n\nB.\n'
    )
    # 'design'/'proposed' are not seeded -> reported, not imported.
    s = sync_breadcrumbs_from_dir(default_project.id, tmp_path, lambda t: _vec768())
    assert "010" not in s["imported"]
    assert "design" in s["missing_vocab"].get("bc_kind", [])
    assert "proposed" in s["missing_vocab"].get("bc_status", [])
    # With --create-missing-vocab the vocab is added and the file imports.
    s2 = sync_breadcrumbs_from_dir(
        default_project.id, tmp_path, lambda t: _vec768(), create_missing_vocab=True
    )
    assert s2["imported"] == ["010"]
    assert not s2["missing_vocab"]
    assert BreadcrumbModel.get_breadcrumb(default_project.id, "010")["kind"] == "design"


def test_sync_prune_removes_absent(tmp_path):
    # Isolated project so prune can't touch other tests' breadcrumbs.
    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    proj = coredb.get_or_create_project(
        ws.id, slug="prune-proj", name="prune-proj", repo_root="/projects/prune"
    )
    BreadcrumbModel.file_breadcrumb(
        project_id=proj.id, identifier="OLD-1", title="stale", kind="bug",
        status="new", severity="high", embedding=_vec768(),
    )
    (tmp_path / "002-keep.md").write_text(
        '---\nnumber: "002"\ntitle: Keep\nkind: bug\nstatus: new\nseverity: high\n---\n\nB.\n'
    )
    s = sync_breadcrumbs_from_dir(proj.id, tmp_path, lambda t: _vec768(), prune=True)
    assert "002" in s["imported"]
    assert "OLD-1" in s["pruned"]
    assert BreadcrumbModel.get_breadcrumb(proj.id, "OLD-1") is None
