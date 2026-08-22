"""CLI subprocess tests (Plan 004 Phase 9a, decision 59).

Tests the noun/verb argparse surface via subprocess.run against ephemeral Postgres.
Keeps existing model-layer tests intact; these test the CLI wiring.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from agent_notes.core import db as coredb
from tests.cli_harness import run_cli

# ephemeral_db is session-scoped from conftest
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


def _run(*args: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run the CLI in a hermetic environment (WI-030); see tests.cli_harness."""
    return run_cli(*args, env=env, check=check)


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


def test_init_explicit_slug_overrides_basename(default_project):
    """`init --slug` registers under the given slug, not the basename (WI-049)."""
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "some-directory-name"
        repo.mkdir()
        (repo / ".git").mkdir()
        result = _run("init", str(repo), "--slug", "chosen-slug", check=False)
        assert result.returncode == 0
        assert "Project 'chosen-slug' registered" in result.stdout
        assert "some-directory-name" not in result.stdout.split("registered")[0]


def _session_orient_cmds(settings: dict) -> list[str]:
    return [
        h["command"]
        for entry in settings.get("hooks", {}).get("SessionStart", [])
        for h in entry.get("hooks", [])
    ]


def _stop_reconcile_cmds(settings: dict) -> list[str]:
    return [
        h["command"]
        for entry in settings.get("hooks", {}).get("Stop", [])
        for h in entry.get("hooks", [])
    ]


def test_init_wires_session_hook_and_preserves_settings():
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        (repo / ".claude").mkdir(parents=True)
        (repo / ".git").mkdir()
        # Pre-existing unrelated setting must survive the merge.
        (repo / ".claude" / "settings.json").write_text(json.dumps({"model": "x"}))
        assert _run("init", str(repo), check=False).returncode == 0
        settings = json.loads((repo / ".claude" / "settings.json").read_text())
        assert settings["model"] == "x"
        assert any(c.startswith("agent-notes orient") for c in _session_orient_cmds(settings))
        assert any(
            c.startswith("agent-notes outbox reconcile") for c in _stop_reconcile_cmds(settings)
        )
        # Hook commands must be cross-platform (no POSIX-only shell syntax).
        for c in _session_orient_cmds(settings) + _stop_reconcile_cmds(settings):
            assert "2>/dev/null" not in c, f"POSIX-only redirect in hook: {c}"
            assert "|| true" not in c, f"POSIX-only fallback in hook: {c}"
            assert ">/dev/null" not in c, f"POSIX-only redirect in hook: {c}"
        # Idempotent: re-running does not duplicate either hook.
        _run("init", str(repo), check=False)
        settings2 = json.loads((repo / ".claude" / "settings.json").read_text())
        assert sum(c.startswith("agent-notes orient") for c in _session_orient_cmds(settings2)) == 1
        assert (
            sum(
                c.startswith("agent-notes outbox reconcile")
                for c in _stop_reconcile_cmds(settings2)
            )
            == 1
        )


def test_init_no_hooks_skips_settings():
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo2"
        repo.mkdir()
        (repo / ".git").mkdir()
        assert _run("init", str(repo), "--no-hooks", check=False).returncode == 0
        assert not (repo / ".claude" / "settings.json").exists()


def test_init_upgrades_old_posix_hook_commands():
    """Old-format hook commands (with POSIX-only shell syntax) are upgraded
    to the cross-platform bare command on re-init."""
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo3"
        (repo / ".claude").mkdir(parents=True)
        (repo / ".git").mkdir()
        old_orient = f"agent-notes orient --path {repo} 2>/dev/null || true"
        old_reconcile = "agent-notes outbox reconcile >/dev/null || true"
        (repo / ".claude" / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [{"hooks": [{"type": "command", "command": old_orient}]}],
                        "Stop": [{"hooks": [{"type": "command", "command": old_reconcile}]}],
                    }
                }
            )
        )
        assert _run("init", str(repo), check=False).returncode == 0
        settings = json.loads((repo / ".claude" / "settings.json").read_text())
        orient_cmds = _session_orient_cmds(settings)
        reconcile_cmds = _stop_reconcile_cmds(settings)
        assert len(orient_cmds) == 1
        assert len(reconcile_cmds) == 1
        assert "2>/dev/null" not in orient_cmds[0]
        assert "|| true" not in orient_cmds[0]
        assert ">/dev/null" not in reconcile_cmds[0]
        assert "|| true" not in reconcile_cmds[0]


def test_resolve_not_configured():
    with tempfile.TemporaryDirectory() as td:
        result = _run("resolve", "--path", td, "--json", check=False)
        assert result.returncode == 3  # EXIT_NOT_CONFIGURED
        data = json.loads(result.stdout)
        assert data["ok"] is False
        assert data["error"]["code"] == "PROJECT_NOT_RESOLVED"


def test_resolve_reports_resolved_via():
    """resolve_via distinguishes an exact project-root match from an 'ancestor'
    match (path inside a registered project / broad librarian root) so an
    unregistered project can't masquerade as an exact match."""
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "myrepo"
        (repo / "sub" / "deep").mkdir(parents=True)
        (repo / ".git").mkdir()
        # --relocate because the slug is the basename ("myrepo") while the temp
        # path differs every run: this genuinely re-points the same project.
        # Without it, init now refuses (EXIT_CONFLICT) rather than silently
        # stealing a registered root — see test_project_repo_root_collision.
        assert _run("init", str(repo), "--relocate", check=False).returncode == 0
        exact = json.loads(_run("resolve", "--path", str(repo), "--json", check=False).stdout)
        assert exact["resolved_via"] == "exact"
        anc = json.loads(
            _run("resolve", "--path", str(repo / "sub" / "deep"), "--json", check=False).stdout
        )
        assert anc["resolved_via"] == "ancestor"
        assert anc["project"] == exact["project"]


@pytest.mark.parametrize(
    "subcmd",
    [
        ["breadcrumb", "find"],
        ["breadcrumb", "get", "X"],
        ["memory", "list"],
        ["memory", "get", "X"],
        ["memory", "search", "q"],
        ["search", "all", "q"],
    ],
)
def test_resolve_failure_is_structured_not_silent(subcmd):
    """Contract: a command whose project resolution fails must emit a structured
    error (parseable JSON, exit 3) — never empty stdout. Empty-stdout-as-success
    in --json mode is how a caller misreads 'not configured' as 'no results' and
    files a duplicate."""
    with tempfile.TemporaryDirectory() as td:
        result = _run(*subcmd, "--path", td, "--json", check=False)
        assert result.returncode == 3, result.stderr
        data = json.loads(result.stdout)  # must be parseable JSON, not empty
        assert data["ok"] is False
        assert data["error"]["code"] == "PROJECT_NOT_RESOLVED"
        assert "PROJECT_NOT_REGISTERED" in data["error"]["message"]


def test_export_unregistered_path_emits_json_error():
    """export is JSON-native (no --json flag); a resolution failure must still be
    parseable JSON on stdout, not empty."""
    with tempfile.TemporaryDirectory() as td:
        result = _run("export", "--path", td, check=False)
        assert result.returncode == 3, result.stderr
        envelope = json.loads(result.stdout)
        assert "PROJECT_NOT_REGISTERED" in envelope["error"]["message"]


@pytest.mark.parametrize(
    "argv",
    [
        ["vocabulary", "list", "--workspace", "no-such-ws", "--json"],
        ["changes", "since", "not-a-timestamp", "--json"],
        ["link", "trace", "bogus-ref-no-colon", "--json"],
    ],
)
def test_bad_input_emits_parseable_json_error(argv):
    """Contract: in --json mode every failure path returns parseable JSON with an
    'error' key — never plain text and never a traceback."""
    result = _run(*argv, check=False)
    assert result.returncode != 0, result.stdout
    assert "error" in json.loads(result.stdout)


def test_link_add_bad_ref_does_not_traceback():
    """link add has no --json; a malformed ref must be a clean error on stderr
    (exit !=0, empty stdout), never an uncaught traceback."""
    result = _run(
        "link",
        "add",
        "--from",
        "bad",
        "--to",
        "k:w/p/i",
        "--type",
        "relates_to",
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout.strip() == ""
    assert "Error" in result.stderr and "Traceback" not in result.stderr


@pytest.mark.slow
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


def test_file_without_canonical_actor_emits_a_named_error(default_project):
    """A write without ambient v6 identity fails before it reaches storage."""
    result = run_cli(
        "work-item",
        "file",
        "--workspace",
        "default",
        "--project",
        "sf2",
        "--identifier",
        "WI-NOLIN-01",
        "--title",
        "should not be filed",
        "--json",
        strip_keys=("AGENT_NOTES_ACTOR_ID", "REGISTA_PRINCIPAL_ID"),
        check=False,
    )
    assert result.returncode != 0
    data = json.loads(result.stdout)
    assert data["ok"] is False
    assert data["error"]["code"] == "ACTOR_ID_NOT_CONFIGURED"
    assert "AGENT_NOTES_ACTOR_ID" in data["error"]["message"]
    assert "Traceback" not in result.stderr

    # ...and the write really did not happen.
    got = _run(
        "work-item",
        "get",
        "WI-NOLIN-01",
        "--workspace",
        "default",
        "--project",
        "sf2",
        "--json",
        check=False,
    )
    assert got.returncode != 0

    # With the actor declared, the same command succeeds.
    ok = run_cli(
        "work-item",
        "file",
        "--workspace",
        "default",
        "--project",
        "sf2",
        "--identifier",
        "WI-NOLIN-02",
        "--title",
        "filed with a declared lineage",
        "--json",
        check=False,
    )
    assert ok.returncode == 0, ok.stderr


def test_breadcrumb_get(default_project):
    from agent_notes.core.work_item_model import WorkItemModel

    WorkItemModel.file_work_item(
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
    from agent_notes.core.work_item_model import WorkItemModel

    WorkItemModel.file_work_item(
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


@pytest.mark.slow
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
    from agent_notes.core.memory_model import add_memory
    from agent_notes.core.work_item_model import WorkItemModel

    ws = coredb.get_or_create_workspace("default", "Default Workspace")

    WorkItemModel.file_work_item(
        default_project.id,
        identifier="BC-TRACE-ALL-1",
        title="Trace start",
        kind="observation",
        status="open",
        embedding=[0.0] * 768,
    )
    WorkItemModel.file_work_item(
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
        "vocabulary",
        "add",
        "--workspace",
        "default",
        "memory_type",
        "reflection",
        "--json",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["vocabulary"]["kind_namespace"] == "memory_type"
    assert data["vocabulary"]["name"] == "reflection"

    # Idempotent: re-running upserts.
    result2 = _run(
        "vocabulary",
        "add",
        "--workspace",
        "default",
        "memory_type",
        "reflection",
        "--json",
        check=False,
    )
    assert result2.returncode == 0

    # And appears in list.
    listed = _run(
        "vocabulary",
        "list",
        "--workspace",
        "default",
        "--kind",
        "memory_type",
        "--json",
        check=False,
    )
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
        (skill_dir / "SKILL.md").write_text("---\nname: demo-skill\ndescription: x\n---\nbody\n")
        common = [
            "install-skills",
            "--target",
            "opencode",
            "--source",
            str(src),
            "--dest",
            str(dest),
            "--json",
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
        assert result.returncode == 0
        data = json.loads(result.stdout)
        names = {s["name"] for s in data["skills"]}
        assert names == {"demo-skill"}


@pytest.mark.slow
def test_breadcrumb_update_append_body(default_project):
    from agent_notes.core.work_item_model import WorkItemModel

    WorkItemModel.file_work_item(
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
    from agent_notes.core.work_item_model import WorkItemModel

    ws = _db.get_or_create_workspace("default", "Default Workspace")
    other_proj = _db.get_or_create_project(
        ws.id, slug="other", name="other", repo_root="/projects/other"
    )

    WorkItemModel.file_work_item(
        default_project.id,
        identifier="BC-SCOPE-PROJ",
        title="Scope proj",
        kind="observation",
        status="open",
        embedding=[0.0] * 768,
    )
    WorkItemModel.file_work_item(
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


# ---------------------------------------------------------------------------
# search all / breadcrumb / memory
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_search_all(default_project):
    from agent_notes.core.memory_model import add_memory
    from agent_notes.core.work_item_model import WorkItemModel

    ws = coredb.get_or_create_workspace("default", "Default Workspace")

    WorkItemModel.file_work_item(
        default_project.id,
        identifier="BC-SEARCH-CLI-1",
        title="PostgreSQL connection pooling",
        kind="observation",
        status="open",
        embedding=[0.0] * 768,
    )
    add_memory(
        workspace_id=ws.id,
        project_id=default_project.id,
        name="search-cli-mem-1",
        memory_type="note",
        body="A searchable memory.",
        embedding=[0.0] * 768,
    )

    result = _run(
        "search",
        "all",
        "connection pooling",
        "--workspace",
        "default",
        "--project",
        "sf2",
        "--json",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert "results" in data
    identifiers = {r["identifier"] for r in data["results"]}
    assert "BC-SEARCH-CLI-1" in identifiers


@pytest.mark.slow
def test_search_breadcrumb_only(default_project):
    from agent_notes.core.memory_model import add_memory
    from agent_notes.core.work_item_model import WorkItemModel

    ws = coredb.get_or_create_workspace("default", "Default Workspace")

    WorkItemModel.file_work_item(
        default_project.id,
        identifier="BC-SEARCH-ONLY",
        title="Unique search term alpha",
        kind="observation",
        status="open",
        embedding=[0.0] * 768,
    )
    add_memory(
        workspace_id=ws.id,
        project_id=default_project.id,
        name="mem-search-only",
        memory_type="note",
        body="Unique search term alpha memory.",
        embedding=[0.0] * 768,
    )

    result = _run(
        "search",
        "breadcrumb",
        "unique search term alpha",
        "--workspace",
        "default",
        "--project",
        "sf2",
        "--json",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    for r in data["results"]:
        assert r["kind"] == "breadcrumb"


@pytest.mark.slow
def test_search_memory_only(default_project):
    from agent_notes.core.memory_model import add_memory

    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    add_memory(
        workspace_id=ws.id,
        project_id=default_project.id,
        name="mem-search-term",
        memory_type="note",
        body="Unique memory search term beta.",
        embedding=[0.0] * 768,
    )

    result = _run(
        "search",
        "memory",
        "unique memory search term beta",
        "--workspace",
        "default",
        "--project",
        "sf2",
        "--json",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    for r in data["results"]:
        assert r["kind"] == "memory"


# ---------------------------------------------------------------------------
# export / import round-trip
# ---------------------------------------------------------------------------


def test_export_produces_valid_json(default_project):
    from agent_notes.core.memory_model import add_memory
    from agent_notes.core.work_item_model import WorkItemModel

    ws = coredb.get_or_create_workspace("default", "Default Workspace")

    WorkItemModel.file_work_item(
        default_project.id,
        identifier="BC-EXPORT-1",
        title="Exportable BC",
        body="Body text",
        kind="observation",
        status="open",
        embedding=[0.0] * 768,
    )
    add_memory(
        workspace_id=ws.id,
        project_id=default_project.id,
        name="export-mem-1",
        memory_type="note",
        body="Exportable memory.",
        embedding=[0.0] * 768,
    )

    result = _run("export", "--path", "/projects/sf2", check=False)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert "exported_at" in data
    assert len(data["projects"]) >= 1

    proj = data["projects"][0]
    wi_ids = {b["identifier"] for b in proj["work_items"]}
    mem_names = {m["name"] for m in proj["memories"]}
    assert "BC-EXPORT-1" in wi_ids
    assert "export-mem-1" in mem_names


@pytest.mark.slow
def test_import_round_trip(default_project):
    """Export, delete the original, re-import, verify data survives."""
    from agent_notes.core.memory_model import add_memory, delete_memory
    from agent_notes.core.work_item_model import WorkItemModel

    ws = coredb.get_or_create_workspace("default", "Default Workspace")

    WorkItemModel.file_work_item(
        default_project.id,
        identifier="BC-RT-1",
        title="Round trip BC",
        body="Round trip body",
        kind="observation",
        status="open",
        embedding=[0.0] * 768,
    )
    add_memory(
        workspace_id=ws.id,
        project_id=default_project.id,
        name="rt-mem-1",
        memory_type="note",
        body="Round trip memory.",
        embedding=[0.0] * 768,
    )

    # Export
    export_result = _run("export", "--path", "/projects/sf2", check=False)
    assert export_result.returncode == 0, export_result.stderr

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write(export_result.stdout)
        export_path = f.name

    try:
        # Soft-delete the originals so re-import creates new rows
        WorkItemModel.delete_work_item(
            project_id=default_project.id,
            identifier="BC-RT-1",
        )
        delete_memory(ws.id, default_project.id, "rt-mem-1")

        # Re-import (same workspace/project slugs in the export file)
        import_result = _run("import", export_path, check=False)
        assert import_result.returncode == 0, import_result.stderr
        assert "Imported" in import_result.stdout

        # Verify the data landed (a new active row for the memory)
        from agent_notes.core.memory_model import get_memory

        mem = get_memory(ws.id, default_project.id, "rt-mem-1")
        assert mem is not None
        assert mem["body"] == "Round trip memory."
    finally:
        os.unlink(export_path)


# ---------------------------------------------------------------------------
# memory list / memory search
# ---------------------------------------------------------------------------


def test_memory_list(default_project):
    from agent_notes.core.memory_model import add_memory

    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    add_memory(
        workspace_id=ws.id,
        project_id=default_project.id,
        name="list-test-mem",
        memory_type="note",
        body="Listable.",
        embedding=[0.0] * 768,
    )

    result = _run(
        "memory",
        "list",
        "--workspace",
        "default",
        "--project",
        "sf2",
        "--json",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    names = {m["name"] for m in data["memories"]}
    assert "list-test-mem" in names


@pytest.mark.slow
def test_memory_search(default_project):
    from agent_notes.core.memory_model import add_memory

    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    add_memory(
        workspace_id=ws.id,
        project_id=default_project.id,
        name="search-cli-mem",
        memory_type="note",
        body="Searchable via CLI subprocess.",
        embedding=[0.0] * 768,
    )

    result = _run(
        "memory",
        "search",
        "searchable CLI subprocess",
        "--workspace",
        "default",
        "--project",
        "sf2",
        "--json",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    names = {m["name"] for m in data["memories"]}
    assert "search-cli-mem" in names


# ---------------------------------------------------------------------------
# breadcrumb export-index gitignore guard (WI-011/WI-012)
# ---------------------------------------------------------------------------


def _git_init(repo: Path) -> None:
    """Initialise a real git repo so `git check-ignore` behaves as in production."""
    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, check=True)


def _register_repo_project(ws_id: int, repo: Path, slug: str):
    """Register a project whose repo_root is *repo* and return it."""
    return coredb.get_or_create_project(ws_id, slug=slug, name=slug, repo_root=str(repo))


def test_export_index_refuses_unignored_repo_root(default_project):
    """WI-011/WI-012: the default repo-root index must not be written when
    OPEN_WORK_ITEMS.txt is not gitignored — a `git add -A` would otherwise
    commit a churning STALE banner. The command refuses with exit 1 and a
    structured JSON error, and leaves no file behind."""
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        _git_init(repo)
        # No .gitignore entry for OPEN_WORK_ITEMS.txt.
        _register_repo_project(default_project.workspace_id, repo, "expidx-unguarded")

        result = _run(
            "breadcrumb",
            "export-index",
            "--path",
            str(repo),
            "--json",
            check=False,
        )
        assert result.returncode == 1, result.stderr
        data = json.loads(result.stdout)
        assert "gitignored" in data["error"]
        assert data["path"] == str(repo / "OPEN_WORK_ITEMS.txt")
        assert "gitignore" in data["hint"]
        # Refusal must not have side-effects: no file written.
        assert not (repo / "OPEN_WORK_ITEMS.txt").exists()


def test_export_index_writes_when_gitignored(default_project):
    """When OPEN_WORK_ITEMS.txt IS gitignored, the guard passes and the file is
    written to the repo root with exit 0."""
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        _git_init(repo)
        (repo / ".gitignore").write_text("OPEN_WORK_ITEMS.txt\n", encoding="utf-8")
        _register_repo_project(default_project.workspace_id, repo, "expidx-guarded")

        result = _run(
            "breadcrumb",
            "export-index",
            "--path",
            str(repo),
            "--json",
            check=False,
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["path"] == str(repo / "OPEN_WORK_ITEMS.txt")
        assert (repo / "OPEN_WORK_ITEMS.txt").is_file()


def test_export_index_output_flag_bypasses_guard(default_project):
    """An explicit --output is the operator's choice; the gitignore guard is
    skipped even when the repo root is unignored, writing wherever --output
    points."""
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        _git_init(repo)
        # Unignored repo root — would be refused by the default-path guard.
        _register_repo_project(default_project.workspace_id, repo, "expidx-output")
        out_file = Path(td) / "elsewhere.txt"

        result = _run(
            "breadcrumb",
            "export-index",
            "--path",
            str(repo),
            "--output",
            str(out_file),
            "--json",
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert out_file.is_file()
        # And the repo root was not touched.
        assert not (repo / "OPEN_WORK_ITEMS.txt").exists()


def test_export_index_no_git_repo_no_guard(default_project):
    """When repo_root is not inside a git repo, there is no tree to pollute;
    the guard is skipped and the file is written to the repo root."""
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        # Deliberately NOT a git repo (no `git init`).
        _register_repo_project(default_project.workspace_id, repo, "expidx-nogit")

        result = _run(
            "breadcrumb",
            "export-index",
            "--path",
            str(repo),
            "--json",
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert (repo / "OPEN_WORK_ITEMS.txt").is_file()


def test_export_index_refuses_when_repo_root_missing(default_project):
    """WI-011/WI-012: when the project has no repo_root, the default path
    cannot be gitignore-checked, so the command refuses rather than write to an
    arbitrary CWD unguarded. The operator must pass --output explicitly."""
    coredb.get_or_create_project(
        default_project.workspace_id,
        slug="expidx-noroot",
        name="expidx-noroot",
        repo_root=None,
    )
    result = _run(
        "breadcrumb",
        "export-index",
        "--workspace",
        "default",
        "--project",
        "expidx-noroot",
        "--json",
        check=False,
    )
    assert result.returncode == 1, result.stderr
    data = json.loads(result.stdout)
    assert "repo root" in data["error"]["message"].lower()


def test_cli_configures_structlog_to_stderr(capsys):
    """Stream discipline (suite CLI contract v1 §1): the CLI entry routes all
    structlog output (regista's included) to stderr, so --json stdout stays
    pure. WI-019's root cause was unconfigured structlog printing to stdout."""
    import structlog

    from agent_notes.cli import _configure_logging_stderr

    structlog.reset_defaults()
    try:
        _configure_logging_stderr()
        structlog.get_logger().warning("probe.event", k="v")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "probe.event" in captured.err
    finally:
        # The configured factory captured pytest's replaced sys.stderr;
        # leaving it bound would poison later tests once that stream is
        # torn down. Reset to structlog's lazy defaults.
        structlog.reset_defaults()


def test_write_parsers_reject_identity_override_flags():
    """Identity is ambient v6 configuration, never a CLI override."""
    import argparse

    from agent_notes.cli import work_items as wi_cli

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    wi_cli.register_work_item_parsers(sub)

    with pytest.raises(SystemExit):
        parser.parse_args(["work-item", "file", "--title", "x", "--actor-id", "agent:worker"])
    with pytest.raises(SystemExit):
        parser.parse_args(["work-item", "file", "--title", "x", "--model-lineage", "glm"])
