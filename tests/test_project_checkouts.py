"""Per-VM project checkouts (Plan 025 WI-2.6): schema, matching, resolve_project, init, CLI.

Windows-style matching is tested on every OS: paths are compared as text, so a
``C:\\projects\\x`` checkout resolves the same on Linux CI as on the workstation.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_notes import cli
from agent_notes.core import checkouts
from agent_notes.core import db as coredb
from tests.cli_harness import run_cli
from tests.conftest import SCHEMA_DIR, ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")

WIN = "winbox"
LIN = "linbox"


def _local(posix_path: str) -> str:
    """How this OS stores a POSIX-looking literal (a drive is prepended on Windows)."""
    return checkouts.canonical_path(posix_path)


@pytest.fixture
def ws():
    return coredb.get_or_create_workspace("ckout-ws", "Ckout WS")


def _proj(ws, slug, repo_root=None, kind=None):
    return coredb.get_or_create_project(ws.id, slug=slug, name=slug, repo_root=repo_root, kind=kind)


def _git_init(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    return path


@pytest.fixture
def vm(monkeypatch):
    monkeypatch.setenv("AGENT_NOTES_VM", LIN)
    return LIN


# --- schema -----------------------------------------------------------------


def test_schema_is_idempotent(ephemeral_db):  # noqa: F811
    import psycopg

    sql = (SCHEMA_DIR / "852_project_checkouts.sql").read_text(encoding="utf-8")
    with psycopg.connect(ephemeral_db) as conn:
        conn.execute(sql)
        conn.execute(sql)


def test_checkouts_cascade_with_project(ws):
    p = _proj(ws, "cascade-me")
    checkouts.register_checkout(p.id, LIN, "/projects/cascade-me")
    with coredb._conn() as conn:
        conn.execute("DELETE FROM projects WHERE id = %s", (p.id,))
        conn.commit()
    assert checkouts.list_checkouts(project_id=p.id) == []


# --- matching ---------------------------------------------------------------


def test_posix_longest_prefix_and_resolved_via(ws):
    outer = _proj(ws, "mono")
    inner = _proj(ws, "mono-pkg")
    checkouts.register_checkout(outer.id, LIN, "/work/mono")
    checkouts.register_checkout(inner.id, LIN, "/work/mono/packages/pkg")

    exact = checkouts.find_project_by_checkout("/work/mono", LIN)
    assert (exact["project"], exact["resolved_via"], exact["repo_root"]) == (
        "mono",
        "exact",
        _local("/work/mono"),
    )
    deep = checkouts.find_project_by_checkout("/work/mono/packages/pkg/src/x.py", LIN)
    assert (deep["project"], deep["resolved_via"]) == ("mono-pkg", "ancestor")
    assert checkouts.find_project_by_checkout("/work/mono/packages/other", LIN)["project"] == "mono"
    assert checkouts.find_project_by_checkout("/elsewhere", LIN) is None
    assert deep["workspace"] == "ckout-ws" and deep["vm_id"] == LIN


def test_component_boundary(ws):
    p = _proj(ws, "agent-notes-x")
    checkouts.register_checkout(p.id, LIN, "/projects/agent-notes-x")
    assert checkouts.find_project_by_checkout("/projects/agent-notes-x-coord", LIN) is None
    assert checkouts.find_project_by_checkout("/projects/agent-notes-x-coord/sub", LIN) is None
    assert checkouts.find_project_by_checkout("/projects/agent-notes-x/sub", LIN) is not None

    w = _proj(ws, "winboundary")
    checkouts.register_checkout(w.id, WIN, "C:\\projects\\winboundary")
    assert checkouts.find_project_by_checkout("C:\\projects\\winboundary-coord", WIN) is None
    assert checkouts.find_project_by_checkout("C:\\projects\\winboundary\\sub", WIN) is not None


def test_windows_case_and_separator_insensitive(ws):
    p = _proj(ws, "wproj")
    checkouts.register_checkout(p.id, WIN, "C:\\Projects\\WProj")
    for query in (
        "c:\\projects\\wproj",
        "C:/projects/wproj",
        "C:/PROJECTS/WPROJ/src/mod.py",
        "c:\\Projects\\WProj\\",
        "C:\\projects\\wproj\\..\\wproj\\x",
    ):
        found = checkouts.find_project_by_checkout(query, WIN)
        assert found is not None and found["project"] == "wproj", query
    assert checkouts.find_project_by_checkout("D:\\projects\\wproj", WIN) is None


def test_posix_paths_stay_case_sensitive(ws):
    p = _proj(ws, "casey")
    checkouts.register_checkout(p.id, LIN, "/projects/Casey")
    if os.name != "nt":
        assert checkouts.find_project_by_checkout("/projects/casey", LIN) is None
    assert checkouts.find_project_by_checkout("/projects/Casey", LIN) is not None


def test_matches_are_per_vm(ws):
    p = _proj(ws, "pervm")
    checkouts.register_checkout(p.id, WIN, "D:\\data\\pervm")
    checkouts.register_checkout(p.id, LIN, "/srv/pervm")
    assert checkouts.find_project_by_checkout("/srv/pervm", WIN) is None
    assert checkouts.find_project_by_checkout("D:\\data\\pervm", LIN) is None
    assert checkouts.find_project_by_checkout("D:\\data\\pervm", WIN) is not None


def test_synthetic_scope_projects_are_not_matched(ws):
    vmproj = coredb.get_or_create_scope_project(ws.id, coredb.SCOPE_VM, vm_id="scopevm")
    checkouts.register_checkout(vmproj.id, LIN, "/projects/synthetic")
    assert checkouts.find_project_by_checkout("/projects/synthetic", LIN) is None
    assert checkouts.list_checkouts(project_id=vmproj.id) == []


def test_register_dedupes_case_variants_and_refreshes(ws):
    p = _proj(ws, "dedupe")
    checkouts.register_checkout(p.id, WIN, "C:\\projects\\Dedupe")
    first = checkouts.list_checkouts(project_id=p.id)[0]
    checkouts.register_checkout(p.id, WIN, "c:/projects/dedupe")
    rows = checkouts.list_checkouts(project_id=p.id)
    assert len(rows) == 1
    assert rows[0]["path"] == "C:\\projects\\Dedupe"
    assert rows[0]["last_seen_at"] >= first["last_seen_at"]


def test_resolve_checkout_prefers_most_recently_seen(ws):
    p = _proj(ws, "twoplaces")
    checkouts.register_checkout(p.id, LIN, "/projects/twoplaces")
    checkouts.register_checkout(p.id, LIN, "/scratch/twoplaces")
    assert checkouts.resolve_checkout(ws.id, "twoplaces", LIN) == _local("/scratch/twoplaces")
    checkouts.register_checkout(p.id, LIN, "/projects/twoplaces")  # seen again
    assert checkouts.resolve_checkout(ws.id, "twoplaces", LIN) == _local("/projects/twoplaces")
    assert checkouts.resolve_checkout(ws.id, "twoplaces", WIN) is None
    assert checkouts.resolve_checkout(ws.id, "no-such", LIN) is None


def test_list_checkouts_filters(ws):
    a, b = _proj(ws, "lista"), _proj(ws, "listb")
    checkouts.register_checkout(a.id, LIN, "/projects/lista")
    checkouts.register_checkout(a.id, WIN, "C:\\projects\\lista")
    checkouts.register_checkout(b.id, LIN, "/projects/listb")
    assert {r["vm_id"] for r in checkouts.list_checkouts(project_id=a.id)} == {LIN, WIN}
    assert [r["path"] for r in checkouts.list_checkouts(project_id=a.id, vm_id=WIN)] == [
        "C:\\projects\\lista"
    ]
    assert {r["project"] for r in checkouts.list_checkouts(vm_id=LIN)} >= {"lista", "listb"}


# --- resolve_project --------------------------------------------------------


def test_resolve_project_prefers_checkout_over_repo_root(ws, monkeypatch):
    monkeypatch.setenv("AGENT_NOTES_VM", WIN)
    p = _proj(ws, "winresolve", repo_root="/projects/winresolve")  # the Linux root
    checkouts.register_checkout(p.id, WIN, "C:\\projects\\winresolve")

    res = coredb.resolve_project("C:\\projects\\winresolve\\src")
    assert res["project"] == "winresolve"
    assert res["source"] == "checkout"
    assert res["resolved_via"] == "ancestor"
    assert res["project_repo_root"] == "/projects/winresolve"
    # and other VMs are unaffected
    monkeypatch.setenv("AGENT_NOTES_VM", LIN)
    with pytest.raises(ValueError, match="PROJECT_NOT_REGISTERED"):
        coredb.resolve_project("C:\\projects\\winresolve\\src")


def test_resolve_project_falls_back_to_repo_root_and_self_heals(ws, vm, tmp_path):
    root = tmp_path / "healme"
    (root / "sub").mkdir(parents=True)
    p = _proj(ws, "healme", repo_root=str(root))
    assert checkouts.list_checkouts(project_id=p.id) == []

    first = coredb.resolve_project(str(root / "sub"))
    assert first["project"] == "healme" and first["source"] == "repo_root"
    healed = checkouts.list_checkouts(project_id=p.id)
    assert [(r["vm_id"], r["path"]) for r in healed] == [(LIN, str(root))]

    second = coredb.resolve_project(str(root / "sub"))
    assert second["source"] == "checkout" and second["project"] == "healme"


def test_repo_root_match_for_a_missing_directory_is_not_recorded(ws, vm, tmp_path):
    root = tmp_path / "elsewhere-vm"  # never created: some other machine's path
    p = _proj(ws, "elsewhere-vm", repo_root=str(root))
    assert coredb.resolve_project(str(root))["source"] == "repo_root"
    assert checkouts.list_checkouts(project_id=p.id) == []


def test_resolve_project_still_filters_to_repo_projects(ws, vm, tmp_path):
    root = tmp_path / "scoperow"
    root.mkdir()
    vmproj = coredb.get_or_create_scope_project(ws.id, coredb.SCOPE_VM, vm_id="scopevm2")
    checkouts.register_checkout(vmproj.id, LIN, str(root))
    with pytest.raises(ValueError, match="PROJECT_NOT_REGISTERED"):
        coredb.resolve_project(str(root))


# --- init -------------------------------------------------------------------


@pytest.fixture
def init_env(monkeypatch):
    class _Off:
        enabled = False
        project = "agent_notes"

    monkeypatch.setattr("agent_notes.core.config.regista_config", lambda: _Off())


def test_init_records_a_checkout_for_the_current_vm(vm, init_env, tmp_path, capsys):
    repo = _git_init(tmp_path / "first-init")
    assert cli.cmd_init(str(repo), workspace_slug="ckout-init", install_hooks=False) == 0
    ws = coredb.get_or_create_workspace("ckout-init", "x")
    proj = coredb.get_project(ws.id, "first-init")
    assert proj.repo_root == str(repo)
    assert checkouts.resolve_checkout(ws.id, "first-init", LIN) == str(repo)


def test_init_attaches_a_second_checkout_without_stealing_repo_root(
    vm, init_env, tmp_path, monkeypatch, capsys
):
    a = _git_init(tmp_path / "a" / "dupapp")
    b = _git_init(tmp_path / "b" / "dupapp")
    assert cli.cmd_init(str(a), workspace_slug="ckout-attach", install_hooks=False) == 0

    monkeypatch.setenv("AGENT_NOTES_VM", "otherbox")
    capsys.readouterr()
    assert cli.cmd_init(str(b), workspace_slug="ckout-attach") == 0
    out = capsys.readouterr().out
    assert "checkout" in out and "repo_root is unchanged" in out
    assert not (b / ".claude").exists(), "attach must not rewrite the shared hook file"

    ws = coredb.get_or_create_workspace("ckout-attach", "x")
    assert coredb.get_project(ws.id, "dupapp").repo_root == str(a)
    assert checkouts.resolve_checkout(ws.id, "dupapp", "otherbox") == str(b)
    assert checkouts.resolve_checkout(ws.id, "dupapp", LIN) == str(a)
    monkeypatch.setenv("AGENT_NOTES_VM", "otherbox")
    res = coredb.resolve_project(str(b / "sub"))
    assert (res["project"], res["source"]) == ("dupapp", "checkout")


def test_init_on_the_same_vm_adds_a_second_path_for_the_slug(vm, init_env, tmp_path):
    a = _git_init(tmp_path / "a" / "samevm")
    b = _git_init(tmp_path / "b" / "samevm")
    assert cli.cmd_init(str(a), workspace_slug="ckout-same", install_hooks=False) == 0
    assert cli.cmd_init(str(b), workspace_slug="ckout-same", install_hooks=False) == 0
    ws = coredb.get_or_create_workspace("ckout-same", "x")
    paths = {r["path"] for r in checkouts.list_checkouts(vm_id=LIN) if r["project"] == "samevm"}
    assert paths == {str(a), str(b)}
    assert coredb.get_project(ws.id, "samevm").repo_root == str(a)


def test_init_relocate_still_moves_repo_root_and_records_checkout(vm, init_env, tmp_path):
    a = _git_init(tmp_path / "a" / "movable")
    b = _git_init(tmp_path / "b" / "movable")
    assert cli.cmd_init(str(a), workspace_slug="ckout-move", install_hooks=False) == 0
    assert (
        cli.cmd_init(str(b), workspace_slug="ckout-move", relocate=True, install_hooks=False) == 0
    )
    ws = coredb.get_or_create_workspace("ckout-move", "x")
    assert coredb.get_project(ws.id, "movable").repo_root == str(b)
    assert checkouts.resolve_checkout(ws.id, "movable", LIN) in {str(a), str(b)}
    assert str(b) in {r["path"] for r in checkouts.list_checkouts(vm_id=LIN)}


def test_init_refuses_a_path_that_is_already_another_projects_checkout(
    vm, init_env, tmp_path, capsys
):
    repo = _git_init(tmp_path / "owned")
    assert cli.cmd_init(str(repo), workspace_slug="ckout-own", install_hooks=False) == 0
    rc = cli.cmd_init(str(repo), workspace_slug="ckout-own", slug="intruder", install_hooks=False)
    assert rc == 4
    ws = coredb.get_or_create_workspace("ckout-own", "x")
    assert coredb.get_project(ws.id, "intruder") is None
    assert "already registered" in capsys.readouterr().err


def test_init_without_a_vm_id_still_registers_but_refuses_to_attach(
    init_env, tmp_path, monkeypatch, capsys
):
    from agent_notes.core import config

    def _no_vm(explicit=None):
        raise config.VmIdError("no stable VM ID")

    monkeypatch.setattr(config, "resolve_vm_id", _no_vm)
    a = _git_init(tmp_path / "a" / "novm")
    b = _git_init(tmp_path / "b" / "novm")
    assert cli.cmd_init(str(a), workspace_slug="ckout-novm", install_hooks=False) == 0
    assert cli.cmd_init(str(b), workspace_slug="ckout-novm", install_hooks=False) == 4
    assert "AGENT_NOTES_VM" in capsys.readouterr().err


# --- CLI --------------------------------------------------------------------


def _cli(*args, vm=LIN):
    res = run_cli(*args, env={"AGENT_NOTES_VM": vm}, check=False)
    return res, (json.loads(res.stdout) if res.stdout.strip().startswith(("{", "[")) else None)


def test_cli_add_and_list_json(ws, tmp_path):
    _proj(ws, "cliadd")
    target = tmp_path / "cliadd"
    target.mkdir()
    res, doc = _cli("checkout", "add", "cliadd", str(target), "--workspace", "ckout-ws", "--json")
    assert res.returncode == 0, res.stderr
    assert doc["ok"] is True and doc["exists"] is True and doc["vm_id"] == LIN

    res, doc = _cli("checkout", "list", "--project", "cliadd", "--workspace", "ckout-ws", "--json")
    assert res.returncode == 0, res.stderr
    assert [(c["project"], c["vm_id"], c["path"]) for c in doc["checkouts"]] == [
        ("cliadd", LIN, str(target))
    ]
    res, doc = _cli("checkout", "list", "--vm", "nobody", "--workspace", "ckout-ws", "--json")
    assert doc["checkouts"] == []


def test_cli_add_unknown_project_uses_the_error_envelope(ws, tmp_path):
    res, doc = _cli(
        "checkout", "add", "no-such-project", str(tmp_path), "--workspace", "ckout-ws", "--json"
    )
    assert res.returncode == 2
    assert doc["ok"] is False and doc["error"]["code"] == "NOT_FOUND"


def test_cli_add_refuses_to_move_another_projects_exact_path(ws, tmp_path):
    a, b = _proj(ws, "clia"), _proj(ws, "clib")
    checkouts.register_checkout(a.id, LIN, str(tmp_path))
    res, doc = _cli("checkout", "add", "clib", str(tmp_path), "--workspace", "ckout-ws", "--json")
    assert res.returncode == 4 and doc["error"]["code"] == "CHECKOUT_CONFLICT"
    assert b.id not in {r["project_id"] for r in checkouts.list_checkouts(vm_id=LIN)}


def test_cli_scan_dry_run_then_apply(tmp_path):
    ws = coredb.get_or_create_workspace("ckout-scan", "Ckout Scan")
    alpha = _proj(ws, "alpha-proj")
    _proj(ws, "beta-proj")
    root = tmp_path / "projects"
    _git_init(root / "Alpha-Proj")  # case-insensitive slug match
    _git_init(root / "beta-proj")
    _git_init(root / "not-a-project")
    (root / "plain-dir").mkdir()
    wt = root / "alpha-proj-wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /nowhere/.git/worktrees/wt\n", encoding="utf-8")

    res, doc = _cli("checkout", "scan", str(root), "--workspace", "ckout-scan", "--json")
    assert res.returncode == 0, res.stderr
    by_dir = {Path(e["dir"]).name: e for e in doc["entries"]}
    assert (
        by_dir["Alpha-Proj"]["status"] == "add" and by_dir["Alpha-Proj"]["project"] == "alpha-proj"
    )
    assert by_dir["beta-proj"]["status"] == "add"
    assert by_dir["not-a-project"]["status"] == "unmatched"
    assert by_dir["plain-dir"]["status"] == "not_git"
    assert by_dir["alpha-proj-wt"]["status"] == "skipped_worktree"
    assert doc["apply"] is False
    assert checkouts.list_checkouts(project_id=alpha.id) == [], "dry run must not write"

    res, doc = _cli("checkout", "scan", str(root), "--workspace", "ckout-scan", "--apply", "--json")
    assert res.returncode == 0, res.stderr
    rows = checkouts.list_checkouts(vm_id=LIN)
    registered = {
        (r["project"], Path(r["path"]).name) for r in rows if r["workspace"] == "ckout-scan"
    }
    assert registered == {("alpha-proj", "Alpha-Proj"), ("beta-proj", "beta-proj")}

    res, doc = _cli("checkout", "scan", str(root), "--workspace", "ckout-scan", "--json")
    statuses = {Path(e["dir"]).name: e["status"] for e in doc["entries"]}
    assert statuses["Alpha-Proj"] == "exists" and statuses["beta-proj"] == "exists"


def test_cli_scan_include_worktrees_and_human_output(tmp_path):
    ws = coredb.get_or_create_workspace("ckout-scan2", "Ckout Scan 2")
    _proj(ws, "wt-proj")
    root = tmp_path / "projects"
    wt = root / "wt-proj"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /nowhere/.git/worktrees/wt\n", encoding="utf-8")

    res, _ = _cli("checkout", "scan", str(root), "--workspace", "ckout-scan2")
    assert res.returncode == 0, res.stderr
    assert "skipped worktree" in res.stdout and "would register" not in res.stdout

    res, _ = _cli(
        "checkout", "scan", str(root), "--workspace", "ckout-scan2", "--include-worktrees"
    )
    assert "would register: wt-proj" in res.stdout and "Dry run" in res.stdout


def test_cli_scan_never_moves_a_path_owned_by_another_project(tmp_path):
    ws = coredb.get_or_create_workspace("ckout-scan3", "Ckout Scan 3")
    owner, _other = _proj(ws, "scan-owner"), _proj(ws, "scan-other")
    root = tmp_path / "projects"
    _git_init(root / "scan-other")
    checkouts.register_checkout(owner.id, LIN, str(root / "scan-other"))
    res, doc = _cli(
        "checkout", "scan", str(root), "--workspace", "ckout-scan3", "--apply", "--json"
    )
    assert doc["entries"][0]["status"] == "conflict"
    assert [
        r["project"] for r in checkouts.list_checkouts(vm_id=LIN) if str(root) in r["path"]
    ] == ["scan-owner"]


def test_cli_scan_missing_directory_is_an_error_envelope(tmp_path):
    res, doc = _cli("checkout", "scan", str(tmp_path / "nope"), "--json")
    assert res.returncode == 2 and doc["error"]["code"] == "NOT_FOUND"
