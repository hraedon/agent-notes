"""CLI shapes for `who`, `msg` and `session policy` (Plan 025 Phase 3)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from agent_notes.core import db as coredb
from agent_notes.core import messaging as m
from tests.cli_harness import build_cli_env
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")

# The host harness's own identity must never leak into a CLI subprocess.
_STRIP = (
    m.SESSION_ENV,
    m.PASEO_ENV,
    "AGENT_NOTES_VM",
    *(var for var, _ in m.HARNESS_ENV_VARS),
)


def _env(extra: dict, strip: tuple[str, ...] = _STRIP) -> dict[str, str]:
    # The subprocesses run from tmp dirs, so a relative PYTHONPATH (src) must
    # be made absolute to keep importing this tree.
    env = build_cli_env(extra, strip_keys=strip)
    if env.get("PYTHONPATH"):
        env["PYTHONPATH"] = os.pathsep.join(
            str(Path(p).resolve()) for p in env["PYTHONPATH"].split(os.pathsep) if p
        )
    return env


def _uid() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture
def vm() -> str:
    return f"vm-{_uid()}"


@pytest.fixture
def ws() -> int:
    return coredb.get_or_create_workspace("default", "Default Workspace").id


def _cli(
    *args: str, vm: str, cwd: Path, env: dict | None = None, input: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agent_notes.cli", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        input=input,
        env=_env({"AGENT_NOTES_VM": vm, **(env or {})}),
        timeout=60,
        check=False,
    )


def _json(proc: subprocess.CompletedProcess[str]) -> dict:
    assert "Traceback" not in proc.stderr, proc.stderr
    return json.loads(proc.stdout)


def test_send_inbox_reply_await_round_trip(vm, tmp_path):
    asker, answerer = f"{vm}/codex/ask", f"{vm}/claude/ans"

    sent = _cli(
        "msg",
        "send",
        "--to",
        answerer,
        "--kind",
        "question",
        "--subject",
        "s?",
        "--body",
        "is it green?",
        "--json",
        "--as",
        asker,
        vm=vm,
        cwd=tmp_path,
    )
    assert sent.returncode == 0, sent.stderr
    out = _json(sent)
    assert set(out) == {"message", "warnings"}
    msg = out["message"]
    assert (msg["from_session"], msg["to_session"], msg["kind"]) == (asker, answerer, "question")
    assert msg["from_project"] is None  # outside a registered repo
    assert out["warnings"] and "registered" in out["warnings"][0]

    inbox = _json(_cli("msg", "inbox", "--claim", "--json", "--as", answerer, vm=vm, cwd=tmp_path))
    assert set(inbox) == {"session", "project", "vm", "claimed", "messages"}
    assert (inbox["session"], inbox["project"], inbox["vm"], inbox["claimed"]) == (
        answerer,
        None,
        vm,
        True,
    )
    assert [r["id"] for r in inbox["messages"]] == [msg["id"]]
    assert inbox["messages"][0]["claimed_by"] == answerer

    rep = _cli(
        "msg",
        "reply",
        str(msg["id"]),
        "--body",
        "-",
        "--json",
        "--as",
        answerer,
        vm=vm,
        cwd=tmp_path,
        input="yes, green",
    )
    assert rep.returncode == 0, rep.stderr
    rep_out = _json(rep)
    assert rep_out["routed"] == "direct"
    assert (rep_out["message"]["to_session"], rep_out["message"]["body"]) == (asker, "yes, green")

    got = _cli(
        "msg",
        "await",
        "--reply-to",
        str(msg["id"]),
        "--timeout",
        "5",
        "--json",
        "--as",
        asker,
        vm=vm,
        cwd=tmp_path,
    )
    assert got.returncode == 0, got.stderr
    assert _json(got)["message"]["id"] == rep_out["message"]["id"]

    human = _cli("msg", "inbox", "--all", "--as", asker, vm=vm, cwd=tmp_path)
    assert human.returncode == 0
    assert m.PEER_MESSAGE_MARKER + answerer in human.stdout
    assert "yes, green" in human.stdout


def test_await_timeout_exits_2_with_envelope(vm, tmp_path):
    proc = _cli(
        "msg", "await", "--timeout", "0.5", "--json", "--as", f"{vm}/claude/w", vm=vm, cwd=tmp_path
    )
    assert proc.returncode == 2
    err = _json(proc)
    assert err["ok"] is False
    assert err["error"]["code"] == "AWAIT_TIMEOUT"
    assert err["error"]["retryable"] is True


def test_send_validation_envelope(vm, tmp_path):
    proc = _cli(
        "msg", "send", "--to-project", "p", "--wake", "--body", "x", "--json", vm=vm, cwd=tmp_path
    )
    assert proc.returncode == 4
    assert _json(proc)["error"]["code"] == "VALIDATION_FAILED"


def test_msg_inside_registered_repo_carries_project(ws, vm, tmp_path):
    slug = f"repo-{_uid()}"
    root = tmp_path / slug
    root.mkdir()
    coredb.get_or_create_project(ws, slug=slug, name=slug, repo_root=str(root))
    proc = _cli(
        "msg",
        "send",
        "--to-vm",
        vm,
        "--body",
        "hi",
        "--json",
        vm=vm,
        cwd=root,
        env={"CLAUDE_CODE_SESSION_ID": "c-1"},
    )
    msg = _json(proc)["message"]
    assert (msg["from_project"], msg["from_session"]) == (slug, f"{vm}/claude/c-1")
    row = m.get_session(f"{vm}/claude/c-1")
    assert (row["project"], row["cwd"]) == (slug, str(root))


def test_msg_from_unregistered_dir_keeps_the_sessions_workspace(ws, vm, tmp_path):
    other_ws = coredb.get_or_create_workspace(f"ws-{_uid()}", "Other")
    slug = f"repo-{_uid()}"
    home = coredb.get_or_create_project(other_ws.id, slug=slug, name=slug, repo_root=f"/x/{slug}")
    sid = f"c-{_uid()}"
    m.register_session(vm, "claude", sid, workspace_id=other_ws.id, project_id=home.id, cwd="/x")
    mine = m.send_message(workspace_id=other_ws.id, from_vm="v", to_project=slug, body="b")["id"]

    env = {"CLAUDE_CODE_SESSION_ID": sid}
    inbox = _json(_cli("msg", "inbox", "--json", vm=vm, cwd=tmp_path, env=env))
    assert mine in {r["id"] for r in inbox["messages"]}
    sent = _json(
        _cli("msg", "send", "--to-vm", vm, "--body", "x", "--json", vm=vm, cwd=tmp_path, env=env)
    )
    assert sent["message"]["workspace_id"] == other_ws.id
    assert m.get_session(f"{vm}/claude/{sid}")["workspace_id"] == other_ws.id


def test_who_lists_registered_sessions(ws, vm, tmp_path):
    m.register_session(vm, "codex", "w1", workspace_id=ws, paseo_agent_id="pa", cwd="/x")
    m.register_session(f"vm-{_uid()}", "claude", "w2", workspace_id=ws)
    out = _json(_cli("who", "--vm", vm, "--json", vm=vm, cwd=tmp_path))
    assert [s["session_key"] for s in out["sessions"]] == [f"{vm}/codex/w1"]
    row = out["sessions"][0]
    for field in (
        "vm_id",
        "harness",
        "session_id",
        "project",
        "cwd",
        "wake_policy",
        "trust",
        "paseo_agent_id",
        "last_seen_at",
    ):
        assert field in row
    assert row["wake_policy"] == "direct"

    human = _cli("who", "--vm", vm, vm=vm, cwd=tmp_path)
    assert f"{vm}/codex/w1" in human.stdout and "paseo" in human.stdout


def test_who_me(vm, tmp_path):
    anon = _json(_cli("who", "--me", "--json", vm=vm, cwd=tmp_path))
    assert anon["session"] is None and "AGENT_NOTES_SESSION" in anon["reason"]

    env = {"OPENCODE_SESSION_ID": "oc-1", "PASEO_AGENT_ID": "pa-9"}
    me = _json(_cli("who", "--me", "--json", vm=vm, cwd=tmp_path, env=env))
    assert me["session"]["session_key"] == f"{vm}/opencode/oc-1"
    assert (me["session"]["source"], me["session"]["paseo_agent_id"]) == ("env", "pa-9")
    assert me["session"]["registered"] is False  # who --me is read-only


def test_ambiguous_cwd_session_fails_with_candidates(ws, vm, tmp_path):
    a = m.register_session(vm, "agy", "g1", workspace_id=ws, cwd=str(tmp_path))["session_key"]
    b = m.register_session(vm, "agy", "g2", workspace_id=ws, cwd=str(tmp_path))["session_key"]
    proc = _cli("msg", "inbox", "--json", vm=vm, cwd=tmp_path)
    assert proc.returncode == 4
    err = _json(proc)["error"]
    assert err["code"] == "AMBIGUOUS_SESSION"
    assert sorted(json.loads(err["detail"])["candidates"]) == sorted([a, b])
    ok = _cli("msg", "inbox", "--json", "--as", a, vm=vm, cwd=tmp_path)
    assert _json(ok)["session"] == a


def test_session_policy(vm, tmp_path):
    env = {"CODEX_THREAD_ID": "t-1", "PASEO_AGENT_ID": "pa"}
    shown = _json(_cli("session", "policy", "--json", vm=vm, cwd=tmp_path, env=env))
    assert shown["session"]["wake_policy"] == "direct"
    assert shown["session"]["wake_policy_explicit"] is False

    set_ = _json(
        _cli(
            "session",
            "policy",
            "--wake-policy",
            "none",
            "--trust",
            "untrusted-input",
            "--json",
            vm=vm,
            cwd=tmp_path,
            env=env,
        )
    )
    assert set_["session"]["wake_policy"] == "none"
    assert set_["session"]["wake_policy_explicit"] is True
    assert set_["session"]["trust"] == "untrusted-input"

    again = _json(_cli("session", "policy", "--json", vm=vm, cwd=tmp_path, env=env))
    assert again["session"]["wake_policy"] == "none"

    # From the same cwd D5 step 3 would find the session just registered there.
    elsewhere = tmp_path.parent / f"elsewhere-{_uid()}"
    elsewhere.mkdir()
    anon = _cli("session", "policy", "--json", vm=vm, cwd=elsewhere)
    assert anon.returncode == 2 and _json(anon)["error"]["code"] == "NO_SESSION"
    via_cwd = _json(_cli("session", "policy", "--json", vm=vm, cwd=tmp_path))
    assert via_cwd["session"]["session_key"] == f"{vm}/codex/t-1"


def test_prune_shape(vm, tmp_path):
    out = _json(_cli("msg", "prune", "--json", vm=vm, cwd=tmp_path))
    assert set(out["pruned"]) == {"messages", "sessions"}


def test_no_store_gives_envelope_not_traceback(vm, tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "agent_notes.cli", "msg", "inbox", "--json"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=_env({"AGENT_NOTES_VM": vm}, (*_STRIP, "AGENT_NOTES_DSN")),
        timeout=60,
        check=False,
    )
    assert proc.returncode == 3
    assert _json(proc)["error"]["code"] == "STORE_NOT_CONFIGURED"
