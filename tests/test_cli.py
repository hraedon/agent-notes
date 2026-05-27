"""CLI subprocess tests (Plan 004 Phase 9a, decision 59).

Tests the noun/verb argparse surface via subprocess.run against ephemeral Postgres.
Keeps existing model-layer tests intact; these test the CLI wiring.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from agent_notes.core import db as coredb

# ephemeral_db is session-scoped from conftest
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")

_CLI = [sys.executable, "-m", "agent_notes.cli"]


def _run(*args: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run the CLI and return CompletedProcess."""
    merged_env = {**os.environ, **(env or {})}
    return subprocess.run(
        _CLI + list(args),
        capture_output=True,
        text=True,
        env=merged_env,
        check=check,
    )


@pytest.fixture
def default_project():
    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    proj = coredb.get_or_create_project(ws.id, slug="sf2", name="sf2", repo_root="/projects/sf2")
    return proj


def test_help():
    result = _run("--help", check=False)
    assert result.returncode == 0
    assert "agent-notes" in result.stdout.lower()


def test_init_creates_project():
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()
        result = _run("init", str(repo), check=False)
        assert result.returncode == 0
        assert "registered" in result.stdout.lower()


def test_resolve_not_configured():
    with tempfile.TemporaryDirectory() as td:
        result = _run("resolve", "--path", td, "--json", check=False)
        assert result.returncode == 3  # EXIT_NOT_CONFIGURED
        data = json.loads(result.stdout)
        assert data["code"] == 3


def test_breadcrumb_file(default_project):
    result = _run(
        "breadcrumb",
        "file",
        "--workspace",
        "default",
        "--project",
        "sf2",
        "--title",
        "CLI test BC",
        "--type",
        "bug",
        "--status",
        "new",
        "--json",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["breadcrumb"]["title"] == "CLI test BC"


def test_breadcrumb_get(default_project):
    from agent_notes.core.breadcrumbs_model import BreadcrumbModel

    BreadcrumbModel.file_breadcrumb(
        default_project.id,
        identifier="BC-CLI-001",
        title="Get me",
        kind="observation",
        status="open",
        embedding=[0.0] * 768,
    )
    result = _run(
        "breadcrumb",
        "get",
        "BC-CLI-001",
        "--workspace",
        "default",
        "--project",
        "sf2",
        "--json",
        check=False,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["breadcrumb"]["identifier"] == "BC-CLI-001"


def test_breadcrumb_find(default_project):
    from agent_notes.core.breadcrumbs_model import BreadcrumbModel

    BreadcrumbModel.file_breadcrumb(
        default_project.id,
        identifier="BC-CLI-002",
        title="Findable BC",
        kind="observation",
        status="open",
        embedding=[0.0] * 768,
    )
    result = _run(
        "breadcrumb",
        "find",
        "--workspace",
        "default",
        "--project",
        "sf2",
        "--status",
        "open",
        "--json",
        check=False,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    identifiers = {r["identifier"] for r in data["breadcrumbs"]}
    assert "BC-CLI-002" in identifiers


def test_memory_add(default_project):
    result = _run(
        "memory",
        "add",
        "--workspace",
        "default",
        "--project",
        "sf2",
        "--name",
        "cli-memory-1",
        "--body",
        "A memory from the CLI.",
        "--type",
        "note",
        "--json",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["memory"]["name"] == "cli-memory-1"


def test_memory_get(default_project):
    from agent_notes.core.memory_model import add_memory

    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    add_memory(
        workspace_id=ws.id,
        project_id=default_project.id,
        name="cli-memory-get",
        memory_type="note",
        body="Retrievable via CLI.",
        embedding=[0.0] * 768,
    )
    result = _run(
        "memory",
        "get",
        "cli-memory-get",
        "--workspace",
        "default",
        "--project",
        "sf2",
        "--json",
        check=False,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["memory"]["name"] == "cli-memory-get"


def test_link_trace_all(default_project):
    """`link trace --all` should walk across kinds (breadcrumb + memory)."""
    from agent_notes.core import links as lnk
    from agent_notes.core.breadcrumbs_model import BreadcrumbModel
    from agent_notes.core.memory_model import add_memory

    ws = coredb.get_or_create_workspace("default", "Default Workspace")

    BreadcrumbModel.file_breadcrumb(
        default_project.id,
        identifier="BC-TRACE-ALL-1",
        title="Trace start",
        kind="observation",
        status="open",
        embedding=[0.0] * 768,
    )
    BreadcrumbModel.file_breadcrumb(
        default_project.id,
        identifier="BC-TRACE-ALL-2",
        title="Trace mid",
        kind="observation",
        status="open",
        embedding=[0.0] * 768,
    )
    add_memory(
        workspace_id=ws.id,
        project_id=default_project.id,
        name="mem-trace-all-1",
        memory_type="note",
        body="Linked memory.",
        embedding=[0.0] * 768,
    )

    # breadcrumb -> breadcrumb -> memory (cross-kind)
    lnk.add_link(
        from_kind="breadcrumb",
        from_workspace=ws.id,
        from_project=default_project.id,
        from_identifier="BC-TRACE-ALL-1",
        to_kind="breadcrumb",
        to_workspace=ws.id,
        to_project=default_project.id,
        to_identifier="BC-TRACE-ALL-2",
        relationship="relates_to",
    )
    lnk.add_link(
        from_kind="breadcrumb",
        from_workspace=ws.id,
        from_project=default_project.id,
        from_identifier="BC-TRACE-ALL-2",
        to_kind="memory",
        to_workspace=ws.id,
        to_project=default_project.id,
        to_identifier="mem-trace-all-1",
        relationship="relates_to",
    )

    result = _run(
        "link",
        "trace",
        "breadcrumb:default/sf2/BC-TRACE-ALL-1",
        "--all",
        "--depth",
        "5",
        "--json",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    identifiers = {n["identifier"] for n in data["nodes"]}
    kinds = {n["kind"] for n in data["nodes"]}
    assert "BC-TRACE-ALL-2" in identifiers
    assert "mem-trace-all-1" in identifiers
    # cross-kind traversal: both breadcrumb and memory should appear
    assert "breadcrumb" in kinds
    assert "memory" in kinds


def test_install_skills_claude_dry_run():
    """install-skills --target claude --dry-run reports what would happen, writes nothing."""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "skills"
        dest = Path(td) / "claude-skills"
        # Build a small fixture skill tree
        skill_dir = src / "demo-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: demo-skill\n---\nbody\n")
        # opencode subtree must be skipped, even if it contains a SKILL.md
        oc = src / "opencode" / "demo"
        oc.mkdir(parents=True)
        (oc / "SKILL.md").write_text("opencode\n")

        result = _run(
            "install-skills",
            "--target",
            "claude",
            "--dry-run",
            "--source",
            str(src),
            "--dest",
            str(dest),
            "--json",
            check=False,
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        names = {s["name"] for s in data["skills"]}
        assert names == {"demo-skill"}
        assert data["dry_run"] is True
        assert data["skills"][0]["status"] == "created"
        # Dry run must not write
        assert not (dest / "demo-skill" / "SKILL.md").exists()


def test_install_skills_claude_idempotent():
    """Second run on identical content reports unchanged."""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "skills"
        dest = Path(td) / "claude-skills"
        skill_dir = src / "demo-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: demo-skill\n---\nbody\n")

        common = [
            "install-skills",
            "--target",
            "claude",
            "--source",
            str(src),
            "--dest",
            str(dest),
            "--json",
        ]
        first = _run(*common, check=False)
        assert first.returncode == 0
        assert (dest / "demo-skill" / "SKILL.md").is_file()
        data1 = json.loads(first.stdout)
        assert data1["skills"][0]["status"] == "created"

        second = _run(*common, check=False)
        assert second.returncode == 0
        data2 = json.loads(second.stdout)
        assert data2["skills"][0]["status"] == "unchanged"


def test_install_skills_claude_updates_changed_content():
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "skills"
        dest = Path(td) / "claude-skills"
        skill_dir = src / "demo-skill"
        skill_dir.mkdir(parents=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("v1\n")

        common = [
            "install-skills",
            "--target",
            "claude",
            "--source",
            str(src),
            "--dest",
            str(dest),
            "--json",
        ]
        _run(*common, check=False)
        skill_file.write_text("v2\n")
        result = _run(*common, check=False)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["skills"][0]["status"] == "updated"
        installed = (dest / "demo-skill" / "SKILL.md").read_text()
        assert installed == "v2\n"


def test_vocabulary_add_creates_and_is_idempotent(default_project):
    """vocabulary add inserts a new entry; running again upserts without error."""
    result = _run(
        "vocabulary", "add", "--workspace", "default", "memory_type", "reflection",
        "--json", check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["vocabulary"]["kind_namespace"] == "memory_type"
    assert data["vocabulary"]["name"] == "reflection"

    # Idempotent: re-running upserts.
    result2 = _run(
        "vocabulary", "add", "--workspace", "default", "memory_type", "reflection",
        "--json", check=False,
    )
    assert result2.returncode == 0

    # And appears in list.
    listed = _run("vocabulary", "list", "--workspace", "default", "--kind", "memory_type", "--json", check=False)
    assert listed.returncode == 0
    names = {v["name"] for v in json.loads(listed.stdout)["vocabulary"]}
    assert "reflection" in names


def test_install_skills_opencode_strips_name_and_writes_flat():
    """install-skills --target opencode writes <name>.md without the name: frontmatter line."""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "skills"
        dest = Path(td) / "opencode-command"
        skill_dir = src / "demo-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: do a thing\n---\n# Body\n\ncontent\n"
        )

        result = _run(
            "install-skills",
            "--target",
            "opencode",
            "--source",
            str(src),
            "--dest",
            str(dest),
            "--json",
            check=False,
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["target"] == "opencode"
        assert data["skills"][0]["name"] == "demo-skill"
        assert data["skills"][0]["status"] == "created"
        installed = (dest / "demo-skill.md").read_text()
        assert installed == "---\ndescription: do a thing\n---\n# Body\n\ncontent\n"


def test_install_skills_opencode_idempotent():
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "skills"
        dest = Path(td) / "opencode-command"
        skill_dir = src / "demo-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: x\n---\nbody\n"
        )
        common = [
            "install-skills", "--target", "opencode",
            "--source", str(src), "--dest", str(dest), "--json",
        ]
        first = _run(*common, check=False)
        assert first.returncode == 0
        second = _run(*common, check=False)
        data2 = json.loads(second.stdout)
        assert data2["skills"][0]["status"] == "unchanged"


def test_install_skills_opencode_skips_opencode_subdir():
    """The legacy skills/opencode/ placeholder must not be installed as a skill."""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "skills"
        dest = Path(td) / "opencode-command"
        skill_dir = src / "demo-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: demo-skill\n---\nbody\n")
        oc = src / "opencode"
        oc.mkdir(parents=True)
        (oc / "SKILL.md").write_text("---\nname: opencode\n---\nshould-be-skipped\n")

        result = _run(
            "install-skills", "--target", "opencode",
            "--source", str(src), "--dest", str(dest), "--json",
            check=False,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        names = {s["name"] for s in data["skills"]}
        assert names == {"demo-skill"}


def test_breadcrumb_update_append_body(default_project):
    from agent_notes.core.breadcrumbs_model import BreadcrumbModel

    BreadcrumbModel.file_breadcrumb(
        default_project.id,
        identifier="BC-APPEND-1",
        title="Append target",
        body="initial line",
        kind="observation",
        status="open",
        embedding=[0.0] * 768,
    )
    result = _run(
        "breadcrumb",
        "update",
        "BC-APPEND-1",
        "--workspace",
        "default",
        "--project",
        "sf2",
        "--append-body",
        "added context",
        "--json",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["breadcrumb"]["body"] == "initial line\n\nadded context"

    # And --body still replaces.
    result = _run(
        "breadcrumb",
        "update",
        "BC-APPEND-1",
        "--workspace",
        "default",
        "--project",
        "sf2",
        "--body",
        "replaced",
        "--json",
        check=False,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["breadcrumb"]["body"] == "replaced"

    # Mutual exclusion.
    result = _run(
        "breadcrumb",
        "update",
        "BC-APPEND-1",
        "--workspace",
        "default",
        "--project",
        "sf2",
        "--body",
        "x",
        "--append-body",
        "y",
        "--json",
        check=False,
    )
    assert result.returncode == 4  # EXIT_CONFLICT


def test_breadcrumb_find_scopes(default_project):
    from agent_notes.core import db as _db
    from agent_notes.core.breadcrumbs_model import BreadcrumbModel

    ws = _db.get_or_create_workspace("default", "Default Workspace")
    other_proj = _db.get_or_create_project(
        ws.id, slug="other", name="other", repo_root="/projects/other"
    )

    BreadcrumbModel.file_breadcrumb(
        default_project.id,
        identifier="BC-SCOPE-PROJ",
        title="Scope proj",
        kind="observation",
        status="open",
        embedding=[0.0] * 768,
    )
    BreadcrumbModel.file_breadcrumb(
        other_proj.id,
        identifier="BC-SCOPE-OTHER",
        title="Scope other",
        kind="observation",
        status="open",
        embedding=[0.0] * 768,
    )

    # project scope: only sees sf2's BC
    r = _run(
        "breadcrumb",
        "find",
        "--workspace",
        "default",
        "--project",
        "sf2",
        "--scope",
        "project",
        "--status",
        "open",
        "--json",
        check=False,
    )
    assert r.returncode == 0
    ids = {b["identifier"] for b in json.loads(r.stdout)["breadcrumbs"]}
    assert "BC-SCOPE-PROJ" in ids
    assert "BC-SCOPE-OTHER" not in ids

    # workspace scope: sees both
    r = _run(
        "breadcrumb",
        "find",
        "--workspace",
        "default",
        "--scope",
        "workspace",
        "--status",
        "open",
        "--json",
        check=False,
    )
    assert r.returncode == 0, r.stderr
    ids = {b["identifier"] for b in json.loads(r.stdout)["breadcrumbs"]}
    assert "BC-SCOPE-PROJ" in ids
    assert "BC-SCOPE-OTHER" in ids

    # global scope: sees both (no --workspace required)
    r = _run(
        "breadcrumb",
        "find",
        "--scope",
        "global",
        "--status",
        "open",
        "--json",
        check=False,
    )
    assert r.returncode == 0, r.stderr
    ids = {b["identifier"] for b in json.loads(r.stdout)["breadcrumbs"]}
    assert {"BC-SCOPE-PROJ", "BC-SCOPE-OTHER"}.issubset(ids)


def test_workspace_list(default_project):
    r = _run("workspace", "list", "--json", check=False)
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    slugs = {w["slug"] for w in data["workspaces"]}
    assert "default" in slugs
    default_entry = next(w for w in data["workspaces"] if w["slug"] == "default")
    assert "id" in default_entry
    assert "name" in default_entry
    assert default_entry["project_count"] >= 1


def test_changes_since(default_project):
    result = _run(
        "changes",
        "since",
        "2000-01-01T00:00:00",
        "--json",
        check=False,
    )
    # Should succeed even if empty
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert "changes" in data
