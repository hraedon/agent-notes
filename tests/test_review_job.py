"""The dispatcher's ``review`` job end to end with a fake runner (Plan 025 WI-5.2).

A requester clone pushes to a bare remote; the reviewing VM's checkout is a
separate clone that only sees the head after the job fetches. Real Postgres,
real git, fake reviewer.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from functools import partial
from pathlib import Path

import pytest

from agent_notes.core import db as coredb
from agent_notes.core import messaging as m
from agent_notes.core.work_item._delegated_review import ReviewProcessResult
from agent_notes.dispatcher import review_job as rj
from agent_notes.dispatcher.jobs import JobContext
from agent_notes.dispatcher.service import Dispatcher

pytestmark = pytest.mark.usefixtures("ephemeral_db")

MODEL = "fake-reviewer-1"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _identity(repo: Path) -> None:
    _git(repo, "config", "user.email", "review@example.invalid")
    _git(repo, "config", "user.name", "Review Test")


class Repos:
    def __init__(self, root: Path) -> None:
        self.remote = root / "remote.git"
        self.requester = root / "requester"
        self.checkout = root / "checkout"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        subprocess.run(
            ["git", "clone", "-q", str(self.remote), str(self.requester)],
            check=True,
            capture_output=True,
        )
        _identity(self.requester)
        (self.requester / "code.py").write_text("value = 1\n", encoding="utf-8")
        _git(self.requester, "add", "code.py")
        _git(self.requester, "commit", "-qm", "base")
        _git(self.requester, "push", "-q", "origin", "HEAD:refs/heads/main")
        self.base = _git(self.requester, "rev-parse", "HEAD")
        subprocess.run(
            ["git", "clone", "-q", str(self.remote), str(self.checkout)],
            check=True,
            capture_output=True,
        )
        (self.requester / "code.py").write_text("value = 2\n", encoding="utf-8")
        _git(self.requester, "commit", "-qam", "head")
        self.head = _git(self.requester, "rev-parse", "HEAD")

    def push(self) -> None:
        _git(self.requester, "push", "-q", "origin", "HEAD:refs/heads/feature")


@pytest.fixture
def repos(tmp_path: Path) -> Repos:
    return Repos(tmp_path)


@pytest.fixture
def ws() -> int:
    return coredb.get_or_create_workspace("default", "Default Workspace").id


@pytest.fixture
def vm() -> str:
    return f"rev-vm-{uuid.uuid4().hex[:6]}"


@pytest.fixture
def project(ws, repos, vm) -> str:
    from agent_notes.core.checkouts import register_checkout

    slug = f"rev-{uuid.uuid4().hex[:8]}"
    proj = coredb.get_or_create_project(ws, slug=slug, name=slug, repo_root="/elsewhere/" + slug)
    register_checkout(proj.id, vm, str(repos.checkout))
    return slug


@pytest.fixture(autouse=True)
def _posix_anywhere(monkeypatch):
    # The fake runner needs no process group, so the job itself runs on Windows too.
    monkeypatch.setattr(rj, "platform_supported", lambda: True)


def _document(verdict: str = "accept", blocking: list | None = None) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "verdict": verdict,
            "summary": "Reviewed the exact change.",
            "blocking_findings": blocking or [],
            "non_blocking_risks": [],
            "reviewed_paths": ["code.py"],
        },
        separators=(",", ":"),
    ).encode()


class FakeRunner:
    def __init__(self, document: bytes | None = None, *, error: Exception | None = None) -> None:
        self.document = document or _document()
        self.error = error
        self.prompt = ""
        self.timeout = 0.0

    def run(self, *, prompt: str, cwd: Path, timeout_seconds: float) -> ReviewProcessResult:
        self.prompt = prompt
        self.timeout = timeout_seconds
        assert (cwd / "code.py").read_text(encoding="utf-8") == "value = 2\n"
        if self.error is not None:
            raise self.error
        return ReviewProcessResult(
            exit_code=0,
            structured_outputs=(self.document,),
            harness="claude",
            harness_version="9.9.9",
            requested_model=MODEL,
            reported_model=MODEL,
            session_id="session-1",
            started_at="2026-10-28T00:00:00Z",
            ended_at="2026-10-28T00:00:01Z",
        )


def _request(ws: int, vm: str, slug: str, repos: Repos, **payload) -> tuple[dict, str]:
    requester = m.register_session("winbox", "codex", uuid.uuid4().hex, workspace_id=ws)
    values = {
        "job": "review",
        "reviewer": "claude",
        "workspace_id": ws,
        "project": slug,
        "base": repos.base,
        "head": repos.head,
        "head_ref": "feature",
        "title": "Bump the value",
        "brief": "Check the bump.",
        "model": MODEL,
    }
    values.update(payload)
    msg = m.send_message(
        workspace_id=ws,
        from_session=requester["session_key"],
        from_vm="winbox",
        from_project=slug,
        to_vm=vm,
        kind="request",
        body="review please",
        payload=values,
    )
    return msg, requester["session_key"]


def _run(vm: str, runner: FakeRunner, resolver=None) -> None:
    handler = partial(
        rj.run_review_job,
        env={},
        runner_factory=lambda model, env: runner,
        checkout_resolver=resolver,
    )
    Dispatcher(vm=vm, waker=None, handlers={"review": handler}).run_once()


def _reply(request_id: int) -> dict:
    replies = m.replies_to(request_id)
    assert len(replies) == 1, replies
    return replies[0]


def test_review_job_replies_with_the_verdict(ws, vm, project, repos):
    repos.push()
    (repos.checkout / "local-notes.txt").write_text("operator edits", encoding="utf-8")
    req, requester = _request(ws, vm, project, repos)
    runner = FakeRunner()

    _run(vm, runner)

    reply = _reply(req["id"])
    assert reply["to_session"] == requester and reply["wake"] is True
    assert reply["from_project"] == project
    payload = reply["payload"]
    assert payload["status"] == "ok" and payload["verdict"] == "accept"
    assert payload["base_sha"] == repos.base and payload["head_sha"] == repos.head
    assert payload["recommendation"] == "store_only"
    assert len(payload["artifact_digest"]) == 64
    assert f"agent-notes review show {req['id']}" in reply["body"]
    assert "ACCEPT" in reply["body"]
    assert runner.prompt.startswith("Perform an adversarial code review for this change.")
    assert f"Request: agent-message-{req['id']}" in runner.prompt
    assert runner.timeout == rj.DEFAULT_TIMEOUT
    with coredb._conn() as conn:
        attempt = conn.execute(
            "SELECT status, work_item_entity_id, repository_identity FROM "
            "review_delegation_attempts WHERE attempt_id = %s",
            (uuid.UUID(payload["attempt_id"]),),
        ).fetchone()
    assert attempt["status"] == "result_validated"
    assert attempt["work_item_entity_id"] == f"brief:agent-message-{req['id']}"
    assert attempt["repository_identity"] == f"default/{project}"
    assert m.get_message(req["id"])["claimed_by"] == f"{vm}/dispatcher"


def test_request_changes_reply_lists_bounded_findings(ws, vm, project, repos):
    repos.push()
    findings = [{"title": f"Bug {i}", "detail": "x" * 3000, "paths": ["code.py"]} for i in range(5)]
    req, _ = _request(ws, vm, project, repos)
    _run(vm, FakeRunner(_document("request_changes", findings)))
    reply = _reply(req["id"])
    assert reply["payload"]["verdict"] == "request_changes"
    assert reply["payload"]["blocking_findings"] == 5
    assert "Blocking findings (5):" in reply["body"]
    assert "more in the full artifact" in reply["body"]
    assert len(reply["body"]) < 8000


def test_unpushed_head_is_refused(ws, vm, project, repos):
    req, _ = _request(ws, vm, project, repos)
    runner = FakeRunner()
    _run(vm, runner)
    reply = _reply(req["id"])
    assert reply["wake"] is True
    assert reply["payload"]["status"] == "error"
    assert reply["payload"]["error_code"] == rj.REVISION_MISSING
    assert repos.head[:12] in reply["body"]
    assert runner.prompt == ""


def test_runner_timeout_is_reported(ws, vm, project, repos):
    repos.push()
    req, _ = _request(ws, vm, project, repos, timeout_seconds=45)
    runner = FakeRunner(error=TimeoutError("slow"))
    _run(vm, runner)
    reply = _reply(req["id"])
    assert reply["payload"]["error_code"] == "REVIEW_RUNNER_TIMEOUT"
    assert runner.timeout == 45
    with coredb._conn() as conn:
        row = conn.execute(
            "SELECT status, error_code FROM review_delegation_attempts "
            "WHERE work_item_entity_id = %s",
            (f"brief:agent-message-{req['id']}",),
        ).fetchone()
    assert row["status"] == "failed" and row["error_code"] == "REVIEW_RUNNER_TIMEOUT"


def test_runner_error_is_reported(ws, vm, project, repos):
    repos.push()
    req, _ = _request(ws, vm, project, repos)
    _run(vm, FakeRunner(error=RuntimeError("claude crashed")))
    assert _reply(req["id"])["payload"]["error_code"] == "REVIEW_RUNNER_FAILED"


def test_no_checkout_on_this_vm(ws, vm, project, repos):
    req, _ = _request(ws, vm, project, repos)
    _run(vm, FakeRunner(), resolver=lambda ws_id, slug, vm_id: None)
    assert _reply(req["id"])["payload"]["error_code"] == rj.NO_CHECKOUT


def test_windows_refuses_with_a_reply(ws, vm, project, repos, monkeypatch):
    monkeypatch.setattr(rj, "platform_supported", lambda: False)
    req, _ = _request(ws, vm, project, repos)
    _run(vm, FakeRunner())
    reply = _reply(req["id"])
    assert reply["payload"]["error_code"] == rj.PLATFORM_UNSUPPORTED
    assert "--on" in reply["body"]


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"reviewer": "codex"}, rj.REVIEWER_UNSUPPORTED),
        ({"head": "main"}, rj.REQUEST_INVALID),
        ({"model": "--dangerously"}, rj.REQUEST_INVALID),
        ({"project": "../etc"}, rj.REQUEST_INVALID),
    ],
)
def test_malformed_payloads_are_refused(ws, vm, project, repos, payload, code):
    req, _ = _request(ws, "vm-x", project, repos, **payload)
    reply = rj.run_review_job(
        m.get_message(req["id"]),
        JobContext(vm="vm-x"),
        env={},
        runner_factory=lambda model, env: FakeRunner(),
    )
    assert reply.payload["error_code"] == code


def test_default_resolver_uses_this_vms_checkout(ws, vm, project, repos):
    assert rj.default_checkout_resolver(ws, project, vm) == str(repos.checkout)
    assert rj.default_checkout_resolver(ws, project, "another-vm") is None
    assert rj.default_checkout_resolver(ws, "no-such-project", vm) is None


def test_unregistered_checkout_replies_no_checkout(ws, vm, project, repos):
    req, _ = _request(ws, "vm-without-checkout", project, repos)
    _run("vm-without-checkout", FakeRunner())
    reply = _reply(req["id"])
    assert reply["payload"]["error_code"] == rj.NO_CHECKOUT
    assert "checkout scan" in reply["body"]


def test_model_and_credentials_come_from_the_environment(monkeypatch):
    pytest.importorskip("agent_notes.core.work_item._review_runners")
    runner = rj.default_runner_factory(
        "claude-haiku-5-5",
        {
            rj.CREDENTIAL_ENV: "CLAUDE_CODE_OAUTH_TOKEN, MISSING_ONE",
            "CLAUDE_CODE_OAUTH_TOKEN": "not-a-real-token",
            rj.CLAUDE_BIN_ENV: "/opt/claude",
        },
    )
    assert runner.requested_model == "claude-haiku-5-5"
    assert runner.executable == "/opt/claude"
    assert runner.credentials == {"CLAUDE_CODE_OAUTH_TOKEN": "not-a-real-token"}


def test_review_show_returns_the_stored_artifact(ws, vm, project, repos, capsys):
    import argparse

    from agent_notes.cli.review import register_review_parsers

    repos.push()
    req, _ = _request(ws, vm, project, repos)
    _run(vm, FakeRunner())
    parser = argparse.ArgumentParser()
    register_review_parsers(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["review", "show", str(req["id"]), "--json"])
    assert args.func(args) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["status"] == "replied"
    assert shown["attempt"]["status"] == "result_validated"
    assert shown["attempt"]["head_sha"] == repos.head
    assert shown["artifact"]["result"]["verdict"] == "accept"
    assert shown["artifact"]["git"]["repository_identity"] == f"default/{project}"
