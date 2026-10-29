"""JSON shapes for `agent-notes review request|show|list` (Plan 025 WI-5.5)."""

from __future__ import annotations

import argparse
import json
import subprocess
import uuid
from pathlib import Path

import pytest

from agent_notes.cli import review as review_cli
from agent_notes.core import db as coredb
from agent_notes.core import messaging as m

pytestmark = pytest.mark.usefixtures("ephemeral_db")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def vm() -> str:
    return f"cli-vm-{uuid.uuid4().hex[:6]}"


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch, vm) -> dict:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "review@example.invalid")
    _git(repo, "config", "user.name", "Review Test")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "base commit")
    _git(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "Make it two")
    head = _git(repo, "rev-parse", "HEAD")

    ws = coredb.get_or_create_workspace("default", "Default Workspace").id
    slug = f"cli-{uuid.uuid4().hex[:8]}"
    coredb.get_or_create_project(ws, slug=slug, name=slug, repo_root=str(repo))
    session = f"{vm}/codex/{uuid.uuid4().hex}"
    monkeypatch.chdir(repo)
    for var in (m.SESSION_ENV, m.PASEO_ENV, review_cli.REVIEW_VM_ENV):
        monkeypatch.delenv(var, raising=False)
    for var, _ in m.HARNESS_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGENT_NOTES_VM", vm)
    monkeypatch.setenv(m.SESSION_ENV, session)
    return {"repo": repo, "base": base, "head": head, "ws": ws, "slug": slug, "session": session}


def _cli(capsys, *argv: str) -> tuple[int, dict]:
    parser = argparse.ArgumentParser()
    review_cli.register_review_parsers(parser.add_subparsers(dest="command"))
    args = parser.parse_args(list(argv))
    code = args.func(args)
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


def _request(capsys, *extra: str) -> tuple[int, dict]:
    base = ("review", "request", "--reviewer", "claude", "--base", "origin/main", "--head", "HEAD")
    return _cli(capsys, *base, *extra)


def _push(repo: Path) -> None:
    _git(repo, "push", "-q", "origin", "HEAD:refs/heads/feature")


def test_request_shape_and_row(capsys, checkout, vm):
    _push(checkout["repo"])
    code, out = _request(
        capsys,
        "--on",
        "mvmcc02",
        "--json",
    )
    assert code == 0, out
    assert set(out) >= {"request_id", "request", "to_vm", "base", "head", "instruction", "reply"}
    assert out["to_vm"] == "mvmcc02"
    assert out["base"] == {"ref": "origin/main", "sha": checkout["base"]}
    assert out["head"] == {"ref": "HEAD", "sha": checkout["head"], "pushed": True}
    assert out["session"] == checkout["session"]
    assert out["instruction"] == review_cli.INSTRUCTION
    assert out["reply"] is None and out["warnings"] == []
    row = m.get_message(out["request_id"])
    assert row["kind"] == "request" and row["to_vm"] == "mvmcc02"
    assert row["from_session"] == checkout["session"]
    assert row["from_project"] == checkout["slug"]
    payload = row["payload"]
    assert payload["job"] == "review" and payload["reviewer"] == "claude"
    assert payload["project"] == checkout["slug"] and payload["workspace_id"] == checkout["ws"]
    assert payload["title"] == "Make it two"
    assert "- Make it two" in payload["brief"]


def test_unpushed_head_is_refused_unless_allowed(capsys, checkout):
    code, out = _request(
        capsys,
        "--json",
    )
    assert code == 4
    assert out["error"]["code"] == "HEAD_NOT_PUSHED"
    assert m.list_requests(checkout["ws"], from_session=checkout["session"]) == []

    code, out = _request(
        capsys,
        "--allow-unpushed",
        "--brief",
        "Check the arithmetic.\nMore.",
        "--json",
    )
    assert code == 0
    assert out["head"]["pushed"] is False
    assert any("not pushed" in w for w in out["warnings"])
    payload = m.get_message(out["request_id"])["payload"]
    assert payload["title"] == "Check the arithmetic."
    assert payload["brief"] == "Check the arithmetic.\nMore."


def test_review_vm_comes_from_the_environment(capsys, checkout, monkeypatch):
    _push(checkout["repo"])
    monkeypatch.setenv(review_cli.REVIEW_VM_ENV, "MVMCC02")
    code, out = _request(
        capsys,
        "--json",
    )
    assert code == 0 and out["to_vm"] == "mvmcc02"


def test_a_bare_model_alias_is_refused_before_sending(capsys, checkout):
    _push(checkout["repo"])
    code, out = _request(capsys, "--model", "haiku", "--json")
    assert code == 4 and out["error"]["code"] == "MODEL_INVALID"
    assert "claude-haiku-" in out["error"]["message"]
    assert m.list_requests(checkout["ws"], from_session=checkout["session"]) == []


def test_bad_revision_is_refused(capsys, checkout):
    code, out = _cli(
        capsys,
        "review",
        "request",
        "--reviewer",
        "claude",
        "--base",
        "origin/main",
        "--head",
        "no-such-branch",
        "--json",
    )
    assert code == 4 and out["error"]["code"] == "REVISION_INVALID"


def test_work_item_brief(capsys, checkout, monkeypatch):
    _push(checkout["repo"])
    from agent_notes.core.work_item_model import WorkItemModel

    monkeypatch.setattr(
        WorkItemModel,
        "get_work_item",
        classmethod(lambda cls, pid, ident: {"identifier": ident, "title": "Fix it"}),
    )
    monkeypatch.setattr(
        WorkItemModel, "get_work_item_body", classmethod(lambda cls, pid, ident: "The body.")
    )
    code, out = _request(
        capsys,
        "--work-item",
        "WI-7",
        "--json",
    )
    assert code == 0
    payload = m.get_message(out["request_id"])["payload"]
    assert payload["work_item"] == "WI-7"
    assert payload["title"] == "WI-7: Fix it" and payload["brief"] == "The body."


def test_wait_times_out_with_the_request_id(capsys, checkout):
    _push(checkout["repo"])
    code, out = _request(
        capsys,
        "--wait",
        "0.3",
        "--json",
    )
    assert code == 2
    assert out["error"]["code"] == "AWAIT_TIMEOUT"
    assert "review show" in out["error"]["message"]


def test_show_and_list_track_status(capsys, checkout, vm):
    _push(checkout["repo"])
    _, out = _request(
        capsys,
        "--on",
        "rev-box",
        "--json",
    )
    rid = out["request_id"]

    code, shown = _cli(capsys, "review", "show", str(rid), "--json")
    assert code == 0
    assert set(shown) == {"request", "status", "reply", "attempt", "artifact"}
    assert shown["status"] == "queued" and shown["reply"] is None

    assert m.claim_requests("rev-box", claimer="rev-box/dispatcher")
    assert _cli(capsys, "review", "show", str(rid), "--json")[1]["status"] == "claimed"

    m.reply(
        rid,
        from_vm="rev-box",
        from_project=checkout["slug"],
        body="Review: ACCEPT",
        payload={"job": "review", "status": "ok", "verdict": "accept"},
        wake=True,
    )
    shown = _cli(capsys, "review", "show", str(rid), "--json")[1]
    assert shown["status"] == "replied"
    assert shown["reply"]["payload"]["verdict"] == "accept"
    assert shown["reply"]["to_session"] == checkout["session"]

    code, listed = _cli(capsys, "review", "list", "--mine", "--json")
    assert code == 0
    assert [r["request_id"] for r in listed["requests"]] == [rid]
    item = listed["requests"][0]
    assert item["status"] == "replied" and item["head"] == checkout["head"]
    assert item["to_vm"] == "rev-box" and item["project"] == checkout["slug"]


def test_show_unknown_request(capsys, checkout):
    code, out = _cli(capsys, "review", "show", "999999999", "--json")
    assert code == 2 and out["error"]["code"] == "NOT_FOUND"
