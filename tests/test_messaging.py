"""Mailbox, presence and await (Plan 025 Phase 3) against real Postgres."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest

from agent_notes.core import db as coredb
from agent_notes.core import messaging as m

pytestmark = pytest.mark.usefixtures("ephemeral_db")


def _uid() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture
def ws() -> int:
    return coredb.get_or_create_workspace("default", "Default Workspace").id


@pytest.fixture
def vm() -> str:
    return f"vm-{_uid()}"


def _project(ws: int, tmp_path: Path | None = None) -> tuple[int, str]:
    slug = f"proj-{_uid()}"
    root = str(tmp_path / slug) if tmp_path else f"/nonexistent/{slug}"
    proj = coredb.get_or_create_project(ws, slug=slug, name=slug, repo_root=root)
    return proj.id, slug


def _session(ws: int, vm: str, *, project_id: int | None = None, **kw) -> str:
    sid = kw.pop("session_id", None) or _uid()
    harness = kw.pop("harness", "claude")
    row = m.register_session(vm, harness, sid, workspace_id=ws, project_id=project_id, **kw)
    return row["session_key"]


def _send(ws: int, **kw) -> dict:
    kw.setdefault("from_vm", "sender-vm")
    kw.setdefault("body", "hello")
    return m.send_message(workspace_id=ws, **kw)


def _ids(rows: list[dict]) -> set[int]:
    return {r["id"] for r in rows}


def _dsn() -> str:
    # The DSN the module under test connects with. Other test modules swap the
    # pool's database, so the session fixture's value can be stale.
    from agent_notes.core.config import resolve_dsn

    return resolve_dsn()


def _sql(dsn: str, sql: str, params: tuple = ()) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql, params)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_direct_message_reaches_only_its_session(ws, vm):
    a, b = _session(ws, vm), _session(ws, vm)
    msg = _send(ws, to_session=b, kind="question")
    assert msg["recipient_registered"] is True
    assert msg["id"] in _ids(m.inbox(b))
    assert msg["id"] not in _ids(m.inbox(a))


def test_inbox_routing_by_project_vm_and_both(ws):
    vm1, vm2 = f"vm-{_uid()}", f"vm-{_uid()}"
    px, slug_x = _project(ws)
    py, slug_y = _project(ws)
    s1 = _session(ws, vm1, project_id=px)
    s2 = _session(ws, vm1, project_id=py)
    s3 = _session(ws, vm2, project_id=px)
    s4 = _session(ws, vm1)  # no project

    to_x = _send(ws, to_project=slug_x)["id"]
    to_vm1 = _send(ws, to_vm=vm1)["id"]
    to_x_vm1 = _send(ws, to_project=slug_x, to_vm=vm1)["id"]

    seen = {s: _ids(m.inbox(s)) for s in (s1, s2, s3, s4)}
    assert {to_x, to_vm1, to_x_vm1} <= seen[s1]
    assert to_vm1 in seen[s2] and not {to_x, to_x_vm1} & seen[s2]
    assert to_x in seen[s3] and not {to_vm1, to_x_vm1} & seen[s3]
    assert to_vm1 in seen[s4] and not {to_x, to_x_vm1} & seen[s4]


def test_anonymous_inbox_reads_project_and_vm(ws, vm):
    px, slug = _project(ws)
    to_x = _send(ws, to_project=slug)["id"]
    to_vm = _send(ws, to_vm=vm)["id"]
    rows = m.inbox(None, vm=vm, project=slug, workspace_id=ws, claim=True)
    assert {to_x, to_vm} <= _ids(rows)
    assert all(r["claimed_by"] == f"{vm}/anonymous" for r in rows if r["id"] in (to_x, to_vm))


def test_notice_is_seen_by_every_match_and_never_claimed(ws):
    px, slug = _project(ws)
    s1 = _session(ws, f"vm-{_uid()}", project_id=px)
    s2 = _session(ws, f"vm-{_uid()}", project_id=px)
    notice = _send(ws, to_project=slug, kind="notice", body="main is frozen")["id"]

    first = m.inbox(s1, claim=True)
    assert notice in _ids(first)
    assert next(r for r in first if r["id"] == notice)["claimed_by"] is None
    assert notice in _ids(m.inbox(s2))
    assert notice in _ids(m.inbox(s1))  # still there after a claim
    assert notice not in _ids(m.claim_deliverable(s2))


def test_request_is_never_visible_to_sessions(ws, vm):
    px, slug = _project(ws)
    s = _session(ws, vm, project_id=px)
    req = _send(ws, to_vm=vm, kind="request", payload={"job": "review"})["id"]
    req2 = _send(ws, to_vm=vm, to_project=slug, kind="request")["id"]

    assert not {req, req2} & _ids(m.inbox(s, claim=True, include_claimed=True))
    assert not {req, req2} & _ids(m.claim_deliverable(s))
    assert m.await_message(s, timeout=0.2) is None

    claimed = m.claim_requests(vm, limit=5)
    assert _ids(claimed) == {req, req2}
    assert all(r["claimed_by"] == f"{vm}/dispatcher" for r in claimed)
    assert m.claim_requests(vm) == []


def test_inbox_message_is_claimed_by_first_session_only(ws):
    px, slug = _project(ws)
    s1 = _session(ws, f"vm-{_uid()}", project_id=px)
    s2 = _session(ws, f"vm-{_uid()}", project_id=px)
    msg = _send(ws, to_project=slug)["id"]
    assert msg in _ids(m.claim_deliverable(s1))
    assert msg not in _ids(m.claim_deliverable(s2))
    assert msg not in _ids(m.inbox(s2, include_claimed=True))
    assert msg in _ids(m.inbox(s1, include_claimed=True))


def test_claim_deliverable_direct_and_wake_filters(ws, vm):
    px, slug = _project(ws)
    s = _session(ws, vm, project_id=px)
    inbox_msg = _send(ws, to_project=slug)["id"]
    plain = _send(ws, to_session=s)["id"]
    woken = _send(ws, to_session=s, wake=True)["id"]

    assert _ids(m.claim_deliverable(s, wake_only=True)) == {woken}
    assert _ids(m.claim_deliverable(s, direct_only=True)) == {plain}
    assert _ids(m.claim_deliverable(s)) == {inbox_msg}


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_claim_has_exactly_one_winner(ws):
    px, slug = _project(ws)
    s1 = _session(ws, f"vm-{_uid()}", project_id=px)
    s2 = _session(ws, f"vm-{_uid()}", project_id=px)
    for _ in range(10):
        msg = _send(ws, to_project=slug)["id"]
        barrier = threading.Barrier(2)
        results: dict[str, list[dict]] = {}

        def claim(key: str) -> None:
            barrier.wait()
            results[key] = m.claim_deliverable(key)

        threads = [threading.Thread(target=claim, args=(k,)) for k in (s1, s2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        winners = [k for k, rows in results.items() if msg in _ids(rows)]
        assert len(winners) == 1, results


def test_claim_skips_a_row_locked_by_another_connection(ws, vm):
    s = _session(ws, vm)
    msg = _send(ws, to_session=s)["id"]
    with psycopg.connect(_dsn()) as holder:
        holder.execute("SELECT id FROM agent_messages WHERE id = %s FOR UPDATE", (msg,))
        assert m.claim_deliverable(s) == []
        holder.rollback()
    assert _ids(m.claim_deliverable(s)) == {msg}
    assert m.claim_deliverable(s) == []


# ---------------------------------------------------------------------------
# Replies
# ---------------------------------------------------------------------------


def test_reply_goes_direct_to_sender_session(ws, vm):
    asker, answerer = _session(ws, vm), _session(ws, f"vm-{_uid()}")
    q = _send(
        ws, from_session=asker, from_vm=vm, to_session=answerer, kind="question", subject="schema?"
    )
    r = m.reply(q["id"], from_session=answerer, from_vm="x", body="yes", wake=True)
    assert r["routed"] == "direct"
    assert (r["to_session"], r["to_project"], r["to_vm"]) == (asker, None, None)
    assert r["kind"] == "reply" and r["reply_to"] == q["id"] and r["wake"] is True
    assert r["subject"] == "Re: schema?"
    assert "reply_routed" not in r["payload"]


def test_reply_without_sender_session_goes_to_project_inbox(ws, vm):
    px, slug = _project(ws)
    answerer = _session(ws, vm)
    q = _send(ws, from_vm="human-vm", from_project=slug, to_session=answerer)
    r = m.reply(q["id"], from_session=answerer, from_vm=vm, body="done", wake=True)
    assert r["routed"] == "project_inbox"
    assert (r["to_session"], r["to_project"]) == (None, slug)
    assert r["wake"] is False
    assert r["payload"]["reply_routed"] == "project_inbox"


def test_reply_without_session_or_project_goes_to_vm_inbox(ws, vm):
    answerer = _session(ws, vm)
    q = _send(ws, from_vm="lonely-vm", to_session=answerer)
    r = m.reply(q["id"], from_vm=vm, body="ok")
    assert (r["to_vm"], r["payload"]["reply_routed"]) == ("lonely-vm", "vm_inbox")


def test_reply_to_unknown_message(ws):
    with pytest.raises(LookupError):
        m.reply(10**12, from_vm="x", body="?")


# ---------------------------------------------------------------------------
# Session resolution (D5)
# ---------------------------------------------------------------------------


def test_resolve_explicit_full_key_beats_env(vm):
    env = {"CLAUDE_CODE_SESSION_ID": "c1", "AGENT_NOTES_SESSION": f"{vm}/codex/t9"}
    got = m.resolve_session(f"{vm}/agy/x1", env=env, vm=vm)
    assert (got.session_key, got.source) == (f"{vm}/agy/x1", "explicit")
    got = m.resolve_session(None, env=env, vm=vm)
    assert (got.session_key, got.harness) == (f"{vm}/codex/t9", "codex")


def test_resolve_explicit_bare_id(ws, vm):
    key = _session(ws, vm, harness="agy", session_id="conv-1")
    assert m.resolve_session("conv-1", env={}, vm=vm).session_key == key
    env = {"CODEX_THREAD_ID": "thread-7"}
    assert m.resolve_session("thread-7", env=env, vm=vm).session_key == f"{vm}/codex/thread-7"
    with pytest.raises(m.SessionNotFoundError):
        m.resolve_session("nobody", env={}, vm=vm)
    _session(ws, vm, harness="codex", session_id="conv-1")
    with pytest.raises(m.AmbiguousSessionError) as exc:
        m.resolve_session("conv-1", env={}, vm=vm)
    assert len(exc.value.candidates) == 2


@pytest.mark.parametrize(
    ("var", "harness"),
    [
        ("CLAUDE_CODE_SESSION_ID", "claude"),
        ("CODEX_THREAD_ID", "codex"),
        ("CODEX_SESSION_ID", "codex"),
        ("OPENCODE_SESSION_ID", "opencode"),
        ("ANTIGRAVITY_CONVERSATION_ID", "agy"),
        ("HERMES_SESSION_ID", "hermes"),
    ],
)
def test_resolve_harness_env(var, harness, vm):
    got = m.resolve_session(None, env={var: "abc", "PASEO_AGENT_ID": "pa-1"}, vm=vm)
    assert got.session_key == f"{vm}/{harness}/abc"
    assert (got.source, got.paseo_agent_id) == ("env", "pa-1")


def test_resolve_by_cwd(ws, vm, tmp_path):
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    key = _session(ws, vm, harness="agy", cwd=str(repo))

    got = m.resolve_session(None, cwd=str(repo / "sub"), env={}, vm=vm)
    assert (got.session_key, got.source) == (key, "cwd")
    assert m.resolve_session(None, cwd=str(repo), env={}, vm=vm).session_key == key
    assert m.resolve_session(None, cwd=str(tmp_path), env={}, vm=vm) is None
    assert m.resolve_session(None, cwd=str(repo), env={}, vm=f"vm-{_uid()}") is None

    _sql(
        _dsn(),
        "UPDATE agent_sessions SET last_seen_at = now() - interval '11 minutes' "
        "WHERE session_key = %s",
        (key,),
    )
    assert m.resolve_session(None, cwd=str(repo), env={}, vm=vm) is None


def test_resolve_by_cwd_ambiguity_names_candidates(ws, vm, tmp_path):
    a = _session(ws, vm, harness="agy", cwd=str(tmp_path))
    b = _session(ws, vm, harness="claude", cwd=str(tmp_path / "inner"))
    with pytest.raises(m.AmbiguousSessionError) as exc:
        m.resolve_session(None, cwd=str(tmp_path / "inner"), env={}, vm=vm)
    assert sorted(exc.value.candidates) == sorted([a, b])
    assert a in str(exc.value) or "--as" in str(exc.value)


def test_resolve_none(vm, tmp_path):
    assert m.resolve_session(None, cwd=str(tmp_path), env={}, vm=vm) is None


def test_resolve_codex_thread_and_session_ids_agree(vm):
    env = {"CODEX_THREAD_ID": "t-1", "CODEX_SESSION_ID": "t-1"}
    assert m.resolve_session(None, env=env, vm=vm).session_key == f"{vm}/codex/t-1"


# ---------------------------------------------------------------------------
# Wake policy, trust and loop guard
# ---------------------------------------------------------------------------


def test_wake_policy_defaults_and_paseo_upgrade(ws, vm):
    plain = m.register_session(vm, "claude", "p1", workspace_id=ws)
    assert (plain["wake_policy"], plain["wake_policy_explicit"]) == ("none", False)
    hosted = m.register_session(vm, "codex", "h1", workspace_id=ws, paseo_agent_id="pa")
    assert hosted["wake_policy"] == "direct"
    # Heartbeat without the Paseo ID keeps it.
    again = m.register_session(vm, "codex", "h1", workspace_id=ws)
    assert (again["paseo_agent_id"], again["wake_policy"]) == ("pa", "direct")
    # A plain session that later shows up under Paseo gets the Paseo default.
    later = m.register_session(vm, "claude", "p1", workspace_id=ws, paseo_agent_id="pb")
    assert later["wake_policy"] == "direct"


def test_paseo_agent_id_set_later(ws, vm):
    # `paseo import`-ed sessions have no PASEO_AGENT_ID; the dispatcher fills it.
    key = m.register_session(vm, "codex", "imp", workspace_id=ws)["session_key"]
    row = m.set_paseo_agent_id(key, "pa-imported")
    assert (row["paseo_agent_id"], row["wake_policy"]) == ("pa-imported", "direct")
    row = m.set_paseo_agent_id(key, None)
    assert (row["paseo_agent_id"], row["wake_policy"]) == (None, "none")
    m.set_session_policy(key, wake_policy="any-direct-from-project")
    row = m.set_paseo_agent_id(key, "pa-2")
    assert row["wake_policy"] == "any-direct-from-project"
    with pytest.raises(m.SessionNotFoundError):
        m.set_paseo_agent_id(f"{vm}/codex/none", "pa")


def test_explicit_wake_policy_is_never_recomputed(ws, vm):
    key = m.register_session(vm, "codex", "e1", workspace_id=ws, paseo_agent_id="pa")["session_key"]
    row = m.set_session_policy(key, wake_policy="none")
    assert (row["wake_policy"], row["wake_policy_explicit"]) == ("none", True)
    row = m.register_session(vm, "codex", "e1", workspace_id=ws, paseo_agent_id="pa2")
    assert row["wake_policy"] == "none"

    key2 = m.register_session(vm, "claude", "e2", workspace_id=ws)["session_key"]
    m.set_session_policy(key2, wake_policy="any-direct-from-project")
    row = m.register_session(vm, "claude", "e2", workspace_id=ws)
    assert row["wake_policy"] == "any-direct-from-project"
    row = m.set_session_policy(key2, trust="untrusted-input")
    assert (row["trust"], row["wake_policy"]) == ("untrusted-input", "any-direct-from-project")


def test_set_policy_validation(ws, vm):
    key = _session(ws, vm)
    with pytest.raises(m.MessagingError):
        m.set_session_policy(key, wake_policy="always")
    with pytest.raises(m.SessionNotFoundError):
        m.set_session_policy(f"{vm}/claude/missing", wake_policy="none")


def test_register_keeps_location_when_asked(ws, vm, tmp_path):
    px, _ = _project(ws)
    m.register_session(vm, "claude", "loc", workspace_id=ws, project_id=px, cwd=str(tmp_path))
    row = m.register_session(
        vm,
        "claude",
        "loc",
        workspace_id=ws,
        cwd=str(tmp_path / "deeper"),
        replace_location=False,
    )
    assert (row["cwd"], row["project_id"]) == (str(tmp_path), px)
    row = m.register_session(vm, "claude", "loc", workspace_id=ws, cwd=str(tmp_path / "x"))
    assert row["cwd"] == str(tmp_path / "x")


def test_auto_continue_bump_and_reset(ws, vm):
    key = _session(ws, vm)
    assert m.bump_auto_continue(key) == 1
    assert m.bump_auto_continue(key) == 2
    m.reset_auto_continue(key)
    assert m.get_session(key)["auto_continues"] == 0
    with pytest.raises(m.SessionNotFoundError):
        m.bump_auto_continue(f"{vm}/claude/gone")


def test_untrusted_session_cannot_send_requests(ws, vm):
    key = _session(ws, vm, trust="untrusted-input")
    with pytest.raises(m.MessagingError, match="untrusted"):
        _send(ws, from_session=key, to_vm=vm, kind="request")
    assert _send(ws, from_session=key, to_vm=vm)["kind"] == "message"


# ---------------------------------------------------------------------------
# Validation, expiry and prune
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kw",
    [
        {},
        {"to_session": "a/b/c", "to_project": "p"},
        {"to_session": "a/b/c", "kind": "notice"},
        {"to_session": "a/b/c", "kind": "request"},
        {"to_project": "p", "kind": "request"},
        {"to_project": "p", "wake": True},
        {"to_session": "not-a-key"},
        {"to_vm": "v", "kind": "bogus"},
        {"to_vm": "v", "body": "x" * (m.MAX_BODY + 1)},
        {"to_vm": "v", "body": "   "},
    ],
)
def test_send_validation(ws, kw):
    with pytest.raises(m.MessagingError):
        _send(ws, **kw)


def test_send_to_unregistered_session_is_allowed(ws, vm):
    msg = _send(ws, to_session=f"{vm}/claude/not-yet")
    assert msg["recipient_registered"] is False
    key = m.register_session(vm, "claude", "not-yet", workspace_id=ws)["session_key"]
    assert msg["id"] in _ids(m.inbox(key))


def test_expired_messages_are_filtered_and_pruned(ws, vm):
    s = _session(ws, vm)
    live = _send(ws, to_session=s)["id"]
    dead = _send(ws, to_session=s, expires_in=timedelta(seconds=-1))["id"]
    reply_to_dead = _send(ws, to_session=s, reply_to=dead)["id"]
    assert _ids(m.inbox(s)) >= {live, reply_to_dead}
    assert dead not in _ids(m.inbox(s, include_claimed=True))
    assert dead not in _ids(m.claim_deliverable(s, limit=100))

    stale = _session(ws, vm)
    _sql(
        _dsn(),
        "UPDATE agent_sessions SET last_seen_at = now() - interval '25 hours' "
        "WHERE session_key = %s",
        (stale,),
    )

    counts = m.prune()
    assert counts["messages"] >= 1 and counts["sessions"] >= 1
    assert m.get_message(dead) is None
    assert m.get_message(reply_to_dead)["reply_to"] is None
    assert m.get_session(stale) is None
    assert m.get_session(s) is not None


def test_prune_with_explicit_now(ws, vm):
    s = _session(ws, vm)
    msg = _send(ws, to_session=s, expires_in=timedelta(hours=1))["id"]
    m.prune()
    assert m.get_message(msg) is not None
    future = m.get_message(msg)["expires_at"] + timedelta(seconds=1)
    m.prune(future)
    assert m.get_message(msg) is None


# ---------------------------------------------------------------------------
# Await
# ---------------------------------------------------------------------------


def test_await_returns_existing_message_immediately(ws, vm):
    s = _session(ws, vm)
    msg = _send(ws, to_session=s)["id"]
    started = time.monotonic()
    got = m.await_message(s, timeout=5)
    assert got["id"] == msg and got["claimed_by"] == s
    assert time.monotonic() - started < 1.0


def test_await_is_woken_by_notify(ws, vm):
    s = _session(ws, vm)
    result: dict = {}

    def waiter() -> None:
        result["msg"] = m.await_message(s, timeout=15)
        result["at"] = time.monotonic()

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.7)  # let it LISTEN, SELECT nothing, and block on notifies
    assert t.is_alive()
    sent_at = time.monotonic()
    msg = _send(ws, to_session=s)["id"]
    t.join(timeout=15)
    assert result["msg"]["id"] == msg
    assert result["at"] - sent_at < 1.0


def test_await_filters_by_reply_to_and_sender(ws, vm):
    s = _session(ws, vm)
    other = _session(ws, vm)
    q = _send(ws, from_session=s, to_session=other)["id"]
    unrelated = _send(ws, from_session=other, to_session=s)["id"]
    assert m.await_message(s, reply_to=q, timeout=0.3) is None
    answer = m.reply(q, from_session=other, from_vm=vm, body="42")["id"]
    assert m.await_message(s, reply_to=q, timeout=2)["id"] == answer
    assert m.await_message(s, from_session="x/y/z", timeout=0.3) is None
    assert m.await_message(s, from_session=other, timeout=2)["id"] == unrelated


def test_await_times_out(ws, vm):
    s = _session(ws, vm)
    started = time.monotonic()
    assert m.await_message(s, timeout=0.5) is None
    assert 0.4 < time.monotonic() - started < 3.0


# ---------------------------------------------------------------------------
# Trigger and framing
# ---------------------------------------------------------------------------


def test_insert_notifies_with_ids_and_addresses_only(ws, vm):
    import json

    with psycopg.connect(_dsn(), autocommit=True) as conn:
        conn.execute(f"LISTEN {m.CHANNEL}")
        msg = _send(ws, to_session=f"{vm}/claude/n1", body="secret content", wake=True)
        notes = list(conn.notifies(timeout=3, stop_after=1))
    payload = json.loads(notes[0].payload)
    assert payload == {
        "id": msg["id"],
        "kind": "message",
        "to_session": f"{vm}/claude/n1",
        "to_project": None,
        "to_vm": None,
        "wake": True,
    }


def test_format_peer_message():
    text = m.format_peer_message(
        {
            "id": 7,
            "kind": "question",
            "from_session": "mvmcc02/codex/t1",
            "from_vm": "mvmcc02",
            "from_project": "agent-notes",
            "subject": "Schema?",
            "body": "Is 851 applied?",
            "reply_to": None,
        }
    )
    assert text.startswith(m.PEER_MESSAGE_MARKER + "mvmcc02/codex/t1 — peer content")
    assert "not instructions from the operator]" in text.splitlines()[0]
    assert "#7 question" in text and "Subject: Schema?" in text
    assert "Is 851 applied?" in text and "msg reply 7" in text
    anon = m.format_peer_message({"id": 8, "kind": "notice", "from_vm": "w", "body": "b"})
    assert "w/-/no-session" in anon and "msg reply" not in anon


def test_session_key_helpers():
    assert m.session_key("v", "claude", "a/b") == "v/claude/a/b"
    assert m.parse_session_key("v/claude/a/b") == ("v", "claude", "a/b")
    with pytest.raises(m.MessagingError):
        m.session_key("v/x", "claude", "s")
    with pytest.raises(m.MessagingError):
        m.parse_session_key("v/claude")
