"""Plan 025 Phase 2: memory tiers (repo / vm / global), against real Postgres."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import psycopg
import pytest

from agent_notes.cli.common import _resolve
from agent_notes.cli.memory import (
    cmd_mem_add,
    cmd_mem_delete,
    cmd_mem_get,
    cmd_mem_list,
    cmd_mem_recall,
    cmd_mem_search,
    cmd_mem_update,
)
from agent_notes.cli.orient import cmd_orient
from agent_notes.core import config as cfg
from agent_notes.core import db as coredb
from tests.conftest import SCHEMA_DIR, ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")

_ROOT = os.path.abspath(tempfile.mkdtemp(prefix="tiers-"))
REPO_ROOT = os.path.join(_ROOT, "repo")
VM_ROOT = os.path.join(_ROOT, "vmroot")
WS = "tiers-ws"
REPO = "tiers-repo"
VM = "tiersvm"


class _Vec:
    def __init__(self, values: list[float]) -> None:
        self._values = values

    def tolist(self) -> list[float]:
        return list(self._values)


def _axis(*weights: float) -> _Vec:
    values = [0.0] * 768
    for i, w in enumerate(weights):
        values[i] = w
    return _Vec(values)


# body prefix -> embedding; the query "q" sits on axis 0.
_EMBEDDINGS = {
    "repo-setup": _axis(1.0, 0.1),
    "vm-setup": _axis(1.0, 0.3),
    "global-setup": _axis(1.0, 0.5),
    "global-unique": _axis(1.0, 0.0),  # closer to the query than the repo row
    "vm-only": _axis(0.2, 1.0),
    "other-vm": _axis(1.0, 0.2),
    "q": _axis(1.0),
}


def _fake_embed(text: str, task: str = "document") -> _Vec:
    for prefix, vec in _EMBEDDINGS.items():
        if text.startswith(prefix):
            return vec
    return _axis(0.0, 0.0, 1.0)


@pytest.fixture(scope="module")
def env(ephemeral_db):  # noqa: F811
    ws = coredb.get_or_create_workspace(WS, "Tiers WS")
    coredb.add_vocabulary(ws.id, "memory_type", "note")
    repo = coredb.get_or_create_project(ws.id, REPO, REPO, repo_root=REPO_ROOT)
    return {"dsn": ephemeral_db, "ws": ws, "repo": repo}


@pytest.fixture(autouse=True)
def _vm_env(monkeypatch):
    monkeypatch.setenv("AGENT_NOTES_VM", VM)


def _args(**kw) -> argparse.Namespace:
    base = {
        "workspace": WS,
        "project": None,
        "path": None,
        "scope": "repo",
        "vm": None,
        "json": True,
        "type": "note",
        "attributes": None,
        "limit": None,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _run(cmd, capsys, **kw):
    code = cmd(_args(**kw))
    out = capsys.readouterr().out
    return code, (json.loads(out) if out.strip().startswith("{") else out)


def _add(capsys, scope, name, body, **kw):
    project = REPO if scope == "repo" else None
    with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
        code, out = _run(
            cmd_mem_add, capsys, scope=scope, project=project, name=name, body=body, **kw
        )
    assert code == 0, out
    return out["memory"]


@pytest.fixture(scope="module")
def seeded(env):
    """Same name 'setup' in all three scopes, plus per-tier unique names."""
    mp = pytest.MonkeyPatch()
    mp.setenv("AGENT_NOTES_VM", VM)
    try:
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            for scope, name, body in (
                ("repo", "setup", "repo-setup: run make dev " + "x" * 200),
                ("vm", "setup", "vm-setup: the GPU is on cuda:1"),
                ("global", "setup", "global-setup: prefer uv"),
                ("global", "convention", "global-unique: kebab-case names"),
                ("vm", "mounts", "vm-only: /data is NFS"),
            ):
                args = _args(
                    scope=scope, project=REPO if scope == "repo" else None, name=name, body=body
                )
                with contextlib.redirect_stdout(io.StringIO()):
                    assert cmd_mem_add(args) == 0
            # An unrelated VM holds a near-identical memory that must stay invisible.
            foreign = _args(scope="vm", vm="other", name="foreign", body="other-vm: not yours")
            with contextlib.redirect_stdout(io.StringIO()):
                assert cmd_mem_add(foreign) == 0
        yield env
    finally:
        mp.undo()


# ---------------------------------------------------------------------------
# Scope rows and CRUD
# ---------------------------------------------------------------------------


def test_tier_rows_have_kinds_and_no_repo_root(seeded):
    ws = seeded["ws"]
    rows = {p.slug: p for p in coredb.list_projects(ws.id, kinds=("repo", "vm", "global"))}
    assert rows[f"vm:{VM}"].kind == "vm"
    assert rows[f"vm:{VM}"].name == f"VM {VM}"
    assert rows[f"vm:{VM}"].repo_root is None
    assert rows["global"].kind == "global"
    assert rows[REPO].kind == "repo"


def test_same_name_in_all_three_scopes_is_independent(seeded, capsys):
    bodies = {}
    for scope in ("repo", "vm", "global"):
        code, out = _run(
            cmd_mem_get,
            capsys,
            scope=scope,
            project=REPO if scope == "repo" else None,
            name="setup",
        )
        assert code == 0
        bodies[scope] = out["memory"]["body"]
        assert out["memory"]["scope"] == scope
    assert bodies["repo"].startswith("repo-setup")
    assert bodies["vm"].startswith("vm-setup")
    assert bodies["global"].startswith("global-setup")


def test_scope_key_in_results(seeded, capsys):
    _, out = _run(cmd_mem_get, capsys, scope="vm", name="setup")
    assert (out["memory"]["scope"], out["memory"]["scope_key"]) == ("vm", VM)
    _, out = _run(cmd_mem_get, capsys, scope="global", name="setup")
    assert (out["memory"]["scope"], out["memory"]["scope_key"]) == ("global", "global")
    _, out = _run(cmd_mem_get, capsys, scope="repo", project=REPO, name="setup")
    assert (out["memory"]["scope"], out["memory"]["scope_key"]) == ("repo", REPO)


def test_vm_scope_works_outside_any_registered_repo(seeded, capsys, tmp_path):
    code, out = _run(
        cmd_mem_get, capsys, workspace=None, scope="vm", path=str(tmp_path), name="setup"
    )
    # No --workspace and an unregistered path: the 'default' workspace holds no
    # such memory, but resolution itself must succeed (NOT_FOUND, not NOT_CONFIGURED).
    assert code == 2
    assert out["error"]["code"] == "NOT_FOUND"


def test_vm_flag_selects_another_vm(seeded, capsys):
    code, out = _run(cmd_mem_get, capsys, scope="vm", vm="other", name="foreign")
    assert code == 0 and out["memory"]["scope_key"] == "other"
    code, out = _run(cmd_mem_get, capsys, scope="vm", name="foreign")
    assert code == 2  # this VM never filed it


def test_update_and_delete_respect_scope(seeded, capsys):
    _add(capsys, "vm", "scratch", "vm-only: temp")
    code, out = _run(cmd_mem_update, capsys, scope="vm", name="scratch", body="vm-only: edited")
    assert code == 0 and out["memory"]["scope"] == "vm"
    # The repo scope has no such memory.
    code, _ = _run(cmd_mem_delete, capsys, scope="repo", project=REPO, name="scratch")
    assert code == 2
    code, out = _run(cmd_mem_delete, capsys, scope="vm", name="scratch")
    assert code == 0 and out["scope_key"] == VM


def test_project_flag_conflicts_with_vm_scope(seeded, capsys):
    code, out = _run(cmd_mem_get, capsys, scope="vm", project=REPO, name="setup")
    assert code == 4
    assert out["error"]["code"] == "VALIDATION_FAILED"


# ---------------------------------------------------------------------------
# Listing and composed search
# ---------------------------------------------------------------------------


def test_list_defaults_to_repo_scope(seeded, capsys):
    _, out = _run(cmd_mem_list, capsys, project=REPO)
    names = {m["name"] for m in out["memories"]}
    assert names == {"setup"}
    assert {m["scope"] for m in out["memories"]} == {"repo"}


def test_list_composed_shadows_by_name(seeded, capsys):
    _, out = _run(cmd_mem_list, capsys, scope="composed", project=REPO)
    by_name = {m["name"]: m for m in out["memories"]}
    assert set(by_name) == {"setup", "convention", "mounts"}
    assert by_name["setup"]["scope"] == "repo"
    assert {s["scope"] for s in by_name["setup"]["shadows"]} == {"vm", "global"}


def test_composed_search_ranks_by_similarity_without_tier_boost(seeded, capsys):
    with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
        code, out = _run(cmd_mem_search, capsys, scope="composed", project=REPO, query="q")
    assert code == 0
    names = [m["name"] for m in out["memories"]]
    # global 'convention' (closest to the query) outranks the repo 'setup'.
    assert names.index("convention") < names.index("setup")
    scopes = {m["name"]: m["scope"] for m in out["memories"]}
    assert scopes["convention"] == "global"
    assert scopes["setup"] == "repo"
    assert out["scopes"] == [
        {"scope": "repo", "scope_key": REPO},
        {"scope": "vm", "scope_key": VM},
        {"scope": "global", "scope_key": "global"},
    ]
    scores = [m["score"] for m in out["memories"]]
    assert scores == sorted(scores, reverse=True)


def test_composed_search_returns_shadows_with_previews(seeded, capsys):
    with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
        _, out = _run(cmd_mem_search, capsys, scope="composed", project=REPO, query="q")
    setup = [m for m in out["memories"] if m["name"] == "setup"]
    assert len(setup) == 1  # one row per name
    assert setup[0]["scope"] == "repo"
    shadows = setup[0]["shadows"]
    assert [s["scope"] for s in shadows] == ["vm", "global"]  # most specific first
    assert shadows[0]["scope_key"] == VM
    assert shadows[0]["body_preview"].startswith("vm-setup")
    assert shadows[1]["scope_key"] == "global"
    for s in shadows:
        assert set(s) == {"scope", "scope_key", "body_preview"}
        assert len(s["body_preview"]) <= 120


def test_composed_search_never_leaks_another_vm(seeded, capsys):
    with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
        _, out = _run(cmd_mem_search, capsys, scope="composed", project=REPO, query="q")
        assert "foreign" not in {m["name"] for m in out["memories"]}
        _, out = _run(cmd_mem_search, capsys, scope="composed", project=REPO, query="q", vm="other")
    by_name = {m["name"]: m for m in out["memories"]}
    assert by_name["foreign"]["scope_key"] == "other"


def test_composed_search_outside_repo_uses_vm_and_global(seeded, capsys):
    with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
        _, out = _run(cmd_mem_search, capsys, scope="composed", query="q")
    assert [s["scope"] for s in out["scopes"]] == ["vm", "global"]
    by_name = {m["name"]: m for m in out["memories"]}
    assert by_name["setup"]["scope"] == "vm"
    assert [s["scope"] for s in by_name["setup"]["shadows"]] == ["global"]


def test_single_scope_search_is_not_composed(seeded, capsys):
    with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
        _, out = _run(cmd_mem_search, capsys, scope="global", query="q")
    assert {m["scope"] for m in out["memories"]} == {"global"}
    assert "scopes" not in out


def test_composed_search_skips_vm_when_unresolvable(seeded, capsys, monkeypatch):
    monkeypatch.delenv("AGENT_NOTES_VM")
    monkeypatch.setattr(cfg.socket, "gethostname", lambda: "localhost")
    with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
        code, out = _run(cmd_mem_search, capsys, scope="composed", query="q")
    assert code == 0
    assert [s["scope"] for s in out["scopes"]] == ["global"]
    assert "AGENT_NOTES_VM" in out["warnings"][0]


def test_vm_scope_errors_when_unresolvable(seeded, capsys, monkeypatch):
    monkeypatch.delenv("AGENT_NOTES_VM")
    monkeypatch.setattr(cfg.socket, "gethostname", lambda: "localhost")
    code, out = _run(cmd_mem_get, capsys, scope="vm", name="setup")
    assert code == 3
    assert out["error"]["code"] == "VM_ID_UNRESOLVED"


def test_composed_recall_goes_through_the_engine_with_shadows(seeded, capsys):
    with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
        code, out = _run(
            cmd_mem_recall, capsys, scope="composed", project=REPO, query="q", budget="high"
        )
    assert code == 0, out
    assert out["engine"] == "native"
    assert [s["scope"] for s in out["scopes"]] == ["repo", "vm", "global"]
    by_ref = {r["source_ref"]: r for r in out["results"]}
    assert by_ref["setup"]["scope"] == "repo"
    assert {s["scope"] for s in by_ref["setup"]["shadows"]} == {"vm", "global"}
    assert by_ref["convention"]["scope"] == "global"
    scores = [r["score"] for r in out["results"]]
    assert scores == sorted(scores, reverse=True)


def test_single_scope_recall_stays_in_its_tier(seeded, capsys):
    with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
        _, out = _run(cmd_mem_recall, capsys, scope="vm", query="q", budget="high")
    assert {r["scope"] for r in out["results"]} == {"vm"}


# ---------------------------------------------------------------------------
# VM identity
# ---------------------------------------------------------------------------


def test_resolve_vm_id_precedence(monkeypatch, tmp_path):
    suite = tmp_path / "suite.env"
    suite.write_text("AGENT_NOTES_VM=FromSuite\n")
    monkeypatch.delenv("AGENT_NOTES_VM")
    monkeypatch.setenv("AGENT_SUITE_CONFIG", str(suite))
    monkeypatch.setattr(cfg.socket, "gethostname", lambda: "Host1.example.com")
    assert cfg.resolve_vm_id() == "fromsuite"
    monkeypatch.setenv("AGENT_NOTES_VM", "FromEnv")
    assert cfg.resolve_vm_id() == "fromenv"
    assert cfg.resolve_vm_id("Explicit") == "explicit"
    monkeypatch.delenv("AGENT_NOTES_VM")
    suite.write_text("")
    assert cfg.resolve_vm_id() == "host1"


@pytest.mark.parametrize(
    "hostname",
    [
        "localhost",
        "a1b2c3d4e5f6",  # container ID
        "agent-notes-web-7d9f8b6c5d-x2k4q",  # Deployment pod
        "nightly-28391234-x7k2p",  # CronJob pod
        "reindex-x7k2p",  # Job pod
    ],
)
def test_generated_hostnames_are_refused(monkeypatch, hostname):
    monkeypatch.delenv("AGENT_NOTES_VM")
    monkeypatch.setattr(cfg.socket, "gethostname", lambda: hostname)
    with pytest.raises(cfg.VmIdError, match="AGENT_NOTES_VM"):
        cfg.resolve_vm_id()
    # An explicit ID always works on such a host.
    assert cfg.resolve_vm_id("mvmcc02") == "mvmcc02"


@pytest.mark.parametrize("hostname", ["mvmcc02", "mac-studio", "home-office-dell1", "web-0"])
def test_ordinary_hostnames_are_accepted(monkeypatch, hostname):
    monkeypatch.delenv("AGENT_NOTES_VM")
    monkeypatch.setattr(cfg.socket, "gethostname", lambda: hostname)
    assert cfg.resolve_vm_id() == hostname


def test_invalid_vm_id_is_rejected():
    with pytest.raises(cfg.VmIdError):
        cfg.resolve_vm_id("bad id/with:chars")


def test_doctor_reports_vm_identity(monkeypatch):
    from agent_notes.scripts import doctor

    assert doctor._check_vm_identity() == ("ok", f"vm id: {VM}")
    monkeypatch.delenv("AGENT_NOTES_VM")
    monkeypatch.setattr(cfg.socket, "gethostname", lambda: "localhost")
    status, detail = doctor._check_vm_identity()
    assert status == "warn" and "AGENT_NOTES_VM" in detail
    payload, _ = doctor.run_json()
    check = next(c for c in payload["checks"] if c["name"] == "vm_identity")
    assert check["status"] == "warn"


# ---------------------------------------------------------------------------
# orient
# ---------------------------------------------------------------------------


def _orient_args(**kw) -> argparse.Namespace:
    base = {
        "workspace": WS,
        "project": None,
        "path": None,
        "days": 7,
        "limit": None,
        "full": False,
        "stale_after_days": 30,
        "reconcile": False,
        "recall": False,
        "vm": None,
        "json": True,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def test_orient_inside_repo_adds_tier_sections(seeded, capsys):
    assert cmd_orient(_orient_args(workspace=WS, project=REPO)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["project"] == REPO
    assert [m["name"] for m in out["memories"]] == ["setup"]  # project list unchanged
    assert {m["name"] for m in out["memories_vm"]} == {"setup", "mounts"}
    assert {m["name"] for m in out["memories_global"]} == {"setup", "convention"}
    assert out["vm"] == VM
    assert "inbox" not in out
    assert out["has_more"]["memories_vm"] is False
    assert out["has_more"]["memories_global"] is False


def test_orient_tier_caps_and_has_more(seeded, capsys):
    for i in range(4):
        _add(capsys, "global", f"cap-{i}", f"global-cap {i}")
    assert cmd_orient(_orient_args(project=REPO)) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["memories_global"]) == 3
    assert out["has_more"]["memories_global"] is True
    assert cmd_orient(_orient_args(project=REPO, full=True)) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["memories_global"]) > 3
    assert out["has_more"]["memories_global"] is False


def test_orient_outside_repo_succeeds_with_tiers_only(seeded, capsys, tmp_path):
    assert cmd_orient(_orient_args(workspace=None, path=str(tmp_path))) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["project"] is None
    assert out["workspace"] == "default"
    assert out["open_work_items"] == [] and out["memories"] == []
    assert out["vm"] == VM


def test_orient_outside_repo_human_output(seeded, capsys, tmp_path):
    assert cmd_orient(_orient_args(workspace=WS, path=str(tmp_path), json=False)) == 0
    text = capsys.readouterr().out
    assert "no registered project here" in text
    assert f"VM memories (vm {VM})" in text
    assert "Global memories" in text
    assert "Open work items" not in text


def test_orient_inside_repo_human_output(seeded, capsys):
    assert cmd_orient(_orient_args(project=REPO, json=False)) == 0
    text = capsys.readouterr().out
    assert f"Orientation for {REPO}" in text
    assert f"VM memories (vm {VM})" in text
    assert "Global memories" in text


def test_orient_unknown_explicit_project_still_errors(seeded, capsys):
    assert cmd_orient(_orient_args(project="nope")) == 2


def test_orient_vm_unresolvable_is_reported_not_fatal(seeded, capsys, monkeypatch):
    monkeypatch.delenv("AGENT_NOTES_VM")
    monkeypatch.setattr(cfg.socket, "gethostname", lambda: "localhost")
    assert cmd_orient(_orient_args(project=REPO)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["vm"] is None and "AGENT_NOTES_VM" in out["vm_error"]
    assert out["memories_vm"] == []


# ---------------------------------------------------------------------------
# Synthetic-row audit (WI-2.4)
# ---------------------------------------------------------------------------


def test_tier_rows_absent_from_project_listings(seeded, capsys):
    ws = seeded["ws"]
    assert {p.slug for p in coredb.list_projects(ws.id)} == {REPO}
    assert all(p.kind == "repo" for p in coredb.list_projects())

    from agent_notes.cli.workspace import cmd_ws_list

    cmd_ws_list(argparse.Namespace(json=True))
    rows = {r["slug"]: r for r in json.loads(capsys.readouterr().out)["workspaces"]}
    assert rows[WS]["project_count"] == 1


def test_resolution_by_slug_never_returns_tier_rows(seeded, capsys):
    for slug in (f"vm:{VM}", "global"):
        with pytest.raises(SystemExit) as exc:
            _resolve(WS, slug, None)
        assert exc.value.code == 2
    assert "memory tiers are not projects" in capsys.readouterr().err
    assert coredb.get_project(seeded["ws"].id, "global") is None


def test_resolve_project_by_path_skips_tier_rows(seeded):
    ws = seeded["ws"]
    vm_proj = coredb.get_or_create_scope_project(ws.id, "vm", VM)
    with psycopg.connect(seeded["dsn"]) as conn:
        conn.execute("UPDATE projects SET repo_root = %s WHERE id = %s", (VM_ROOT, vm_proj.id))
    try:
        with pytest.raises(ValueError, match="PROJECT_NOT_REGISTERED"):
            coredb.resolve_project(os.path.join(VM_ROOT, "sub"))
        from agent_notes.core.project_discovery import discover_project

        assert discover_project(VM_ROOT) is None
        assert coredb.resolve_project(os.path.join(REPO_ROOT, "x"))["project"] == REPO
    finally:
        with psycopg.connect(seeded["dsn"]) as conn:
            conn.execute("UPDATE projects SET repo_root = NULL WHERE id = %s", (vm_proj.id,))


def test_cross_project_link_target_ignores_tier_rows(seeded):
    from agent_notes.core.work_item import _cross_project  # noqa: F401  (import check)

    with psycopg.connect(seeded["dsn"]) as conn:
        rows = conn.execute(
            "SELECT count(*) FROM projects WHERE slug = %s AND kind = 'repo'", ("global",)
        ).fetchone()
    assert rows[0] == 0


def test_web_project_lookup_excludes_tiers(seeded):
    from agent_notes.web import app as web

    ws = seeded["ws"]
    assert web._find_project(ws.id, REPO) is not None
    assert web._find_project(ws.id, "global") is None
    assert web._find_project(ws.id, f"vm:{VM}") is None


def test_export_keeps_tiers_out_of_projects_but_backs_them_up(seeded, capsys):
    from agent_notes.cli.export import cmd_export

    cmd_export(argparse.Namespace(path=None, workspace=WS, project=None))
    data = json.loads(capsys.readouterr().out)
    assert [p["project"] for p in data["projects"]] == [REPO]
    scopes = {(s["scope"], s["scope_key"]) for s in data["memory_scopes"]}
    assert ("vm", VM) in scopes and ("global", "global") in scopes
    vm_scope = next(s for s in data["memory_scopes"] if s["scope_key"] == VM)
    assert "setup" in {m["name"] for m in vm_scope["memories"]}


def test_init_refuses_reserved_slugs(tmp_path, capsys):
    from agent_notes.cli import cmd_init

    assert cmd_init(str(tmp_path), slug="global") == 4
    assert cmd_init(str(tmp_path), slug="vm:x") == 4
    assert "reserved" in capsys.readouterr().err


def test_get_or_create_project_infers_kind_from_slug(seeded):
    ws = coredb.get_or_create_workspace("tiers-infer", "Tiers Infer")
    assert coredb.get_or_create_project(ws.id, "global", "Global").kind == "global"
    assert coredb.get_or_create_project(ws.id, "plain", "Plain").kind == "repo"
    assert coredb.get_or_create_scope_project(ws.id, "global").id == (
        coredb.get_or_create_project(ws.id, "global", "Global").id
    )


# ---------------------------------------------------------------------------
# Migration (WI-2.1)
# ---------------------------------------------------------------------------


def test_migration_850_is_idempotent_and_backfills(seeded):
    sql = (Path(SCHEMA_DIR) / "850_projects_kind.sql").read_text(encoding="utf-8")
    ws = seeded["ws"]
    with psycopg.connect(seeded["dsn"]) as conn:
        # Simulate pre-850 rows: synthetic slugs carrying the default kind.
        conn.execute(
            "INSERT INTO projects (workspace_id, slug, name, kind)"
            " VALUES (%s, 'vm:legacy', 'x', 'repo')"
            " ON CONFLICT (workspace_id, slug) DO UPDATE SET kind = 'repo'",
            (ws.id,),
        )
        conn.execute("UPDATE projects SET kind = 'repo' WHERE slug = 'global'")
        conn.commit()
        for _ in range(2):  # re-running must be a no-op
            conn.execute(sql)
            conn.commit()
        kinds = dict(
            conn.execute(
                "SELECT slug, kind FROM projects WHERE slug IN ('vm:legacy', 'global')"
            ).fetchall()
        )
        idx = conn.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = 'idx_projects_workspace_kind'"
        ).fetchone()
    assert kinds == {"vm:legacy": "vm", "global": "global"}
    assert idx is not None


def test_kind_check_constraint_rejects_unknown_values(seeded):
    with psycopg.connect(seeded["dsn"]) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO projects (workspace_id, slug, name, kind)"
                " VALUES (%s, 'bad', 'bad', 'galaxy')",
                (seeded["ws"].id,),
            )
