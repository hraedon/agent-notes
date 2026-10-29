"""Dispatcher: L3 Paseo wake and request jobs (Plan 025 Phase 5), real Postgres."""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

import psycopg
import pytest

from agent_notes.core import db as coredb
from agent_notes.core import messaging as m
from agent_notes.dispatcher import jobs as dj
from agent_notes.dispatcher import wake as dw
from agent_notes.dispatcher.paseo import (
    PaseoClient,
    find_agent_for_session,
    find_paseo,
    paseo_home,
)
from agent_notes.dispatcher.service import AlreadyRunningError, Dispatcher
from tests.fake_paseo import FakePaseo, install

pytestmark = pytest.mark.usefixtures("ephemeral_db")


def _uid() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture
def ws() -> int:
    return coredb.get_or_create_workspace("default", "Default Workspace").id


@pytest.fixture
def vm() -> str:
    return f"vm-{_uid()}"


@pytest.fixture
def paseo(tmp_path: Path) -> FakePaseo:
    return install(tmp_path / "fake-paseo")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "paseo-home"
    (path / "agents").mkdir(parents=True)
    return path


def _waker(vm: str, paseo: FakePaseo, home: Path, cap: int = 3) -> dw.L3Waker:
    return dw.L3Waker(
        vm=vm,
        paseo=PaseoClient(str(paseo.binary)),
        paseo_home=home,
        max_auto_continues=cap,
    )


def _session(ws: int, vm: str, *, harness: str = "codex", **kw) -> str:
    sid = kw.pop("session_id", None) or _uid()
    return m.register_session(vm, harness, sid, workspace_id=ws, **kw)["session_key"]


def _wake_msg(ws: int, to: str, **kw) -> dict:
    kw.setdefault("from_vm", "other-vm")
    kw.setdefault("body", "the verdict is in")
    return m.send_message(workspace_id=ws, to_session=to, wake=True, kind="reply", **kw)


# ---------------------------------------------------------------------------
# L3 wake decisions
# ---------------------------------------------------------------------------


def test_idle_agent_is_sent_the_framed_message(ws, vm, paseo, home):
    key = _session(ws, vm, paseo_agent_id="agent-1")
    paseo.set_agent("agent-1", "idle")
    msg = _wake_msg(ws, key, from_session="other-vm/claude/abc", subject="Re: review")

    result = _waker(vm, paseo, home).wake(key, [msg["id"]])

    assert result.outcome == dw.SENT, result.reason
    assert result.agent_id == "agent-1"
    calls = paseo.calls()
    assert [c["args"][0] for c in calls] == ["inspect", "send"]
    assert calls[0]["args"] == ["inspect", "agent-1", "--json"]
    send = calls[1]
    assert send["args"][:5] == ["send", "agent-1", "--no-wait", "--behavior", "steer"]
    assert "--host" not in send["args"]
    assert send["prompt"].startswith(m.PEER_MESSAGE_MARKER + "other-vm/claude/abc")
    assert "the verdict is in" in send["prompt"]
    row = m.get_message(msg["id"])
    assert row["claimed_by"] == f"{vm}/dispatcher"
    assert m.get_session(key)["auto_continues"] == 1


def test_one_message_per_wake_and_the_rest_stay_pending(ws, vm, paseo, home):
    # Finding 2: at most one message per agent per pass; the second is never
    # sent while the woken turn runs.
    key = _session(ws, vm, paseo_agent_id="agent-1")
    paseo.set_agent("agent-1", "idle")
    a, b = _wake_msg(ws, key, body="first"), _wake_msg(ws, key, body="second")
    d = Dispatcher(vm=vm, waker=_waker(vm, paseo, home), handlers=None)
    d.sweep()
    (result,) = d.process_wakes(force=True)
    assert result.outcome == dw.SENT and result.message_ids == (a["id"],)
    assert result.remaining == (b["id"],)
    (send,) = paseo.sends()
    assert "first" in send["prompt"] and "second" not in send["prompt"]
    assert m.get_message(b["id"])["claimed_by"] is None
    assert b["id"] in d._pending and a["id"] not in d._pending
    assert m.get_session(key)["auto_continues"] == 1

    paseo.set_agent("agent-1", "running")
    (result,) = d.process_wakes(force=True)
    assert result.outcome == dw.DEFERRED
    assert len(paseo.sends()) == 1 and b["id"] in d._pending


@pytest.mark.parametrize("status", ["running", "initializing"])
def test_running_agent_is_deferred_and_never_interrupted(ws, vm, paseo, home, status):
    key = _session(ws, vm, paseo_agent_id="agent-1")
    paseo.set_agent("agent-1", status)
    msg = _wake_msg(ws, key)
    result = _waker(vm, paseo, home).wake(key, [msg["id"]])
    assert result.outcome == dw.DEFERRED
    assert not result.settled
    assert paseo.sends() == []
    assert m.get_message(msg["id"])["claimed_by"] is None


@pytest.mark.parametrize("status,archived", [("closed", False), ("error", False), ("idle", True)])
def test_closed_or_archived_agent_settles_for_l1(ws, vm, paseo, home, status, archived):
    key = _session(ws, vm, paseo_agent_id="agent-1")
    paseo.set_agent("agent-1", status, archived=archived)
    msg = _wake_msg(ws, key)
    result = _waker(vm, paseo, home).wake(key, [msg["id"]])
    assert result.outcome == dw.NOT_HOSTED
    assert paseo.sends() == []
    assert m.get_message(msg["id"])["claimed_by"] is None


def test_policy_none_refuses(ws, vm, paseo, home):
    key = _session(ws, vm, paseo_agent_id="agent-1")
    m.set_session_policy(key, wake_policy="none")
    paseo.set_agent("agent-1", "idle")
    msg = _wake_msg(ws, key)
    result = _waker(vm, paseo, home).wake(key, [msg["id"]])
    assert result.outcome == dw.REFUSED
    assert msg["id"] in result.refused
    assert paseo.calls() == []
    assert m.get_message(msg["id"])["claimed_by"] is None


def test_any_direct_from_project_needs_a_shared_project(ws, vm, paseo, home):
    proj = coredb.get_or_create_project(ws, slug=f"p-{_uid()}", name="p", repo_root=None)
    key = _session(ws, vm, paseo_agent_id="agent-1", project_id=proj.id)
    m.set_session_policy(key, wake_policy="any-direct-from-project")
    paseo.set_agent("agent-1", "idle")
    stranger = _wake_msg(ws, key, from_project="elsewhere")
    result = _waker(vm, paseo, home).wake(key, [stranger["id"]])
    assert result.outcome == dw.REFUSED
    peer = _wake_msg(ws, key, from_project=proj.slug)
    result = _waker(vm, paseo, home).wake(key, [peer["id"]])
    assert result.outcome == dw.SENT
    assert m.get_message(stranger["id"])["claimed_by"] is None


def test_untrusted_sender_never_wakes(ws, vm, paseo, home):
    key = _session(ws, vm, paseo_agent_id="agent-1")
    sender = _session(ws, "web-vm", harness="claude", trust="untrusted-input")
    paseo.set_agent("agent-1", "idle")
    msg = _wake_msg(ws, key, from_session=sender)
    result = _waker(vm, paseo, home).wake(key, [msg["id"]])
    assert result.outcome == dw.REFUSED
    assert "untrusted" in result.refused[msg["id"]]
    assert paseo.calls() == []
    # Still deliverable at the next turn (L1).
    assert msg["id"] in {r["id"] for r in m.claim_deliverable(key)}


def test_auto_continue_cap_refuses(ws, vm, paseo, home):
    key = _session(ws, vm, paseo_agent_id="agent-1")
    for _ in range(3):
        m.bump_auto_continue(key)
    paseo.set_agent("agent-1", "idle")
    msg = _wake_msg(ws, key)
    result = _waker(vm, paseo, home, cap=3).wake(key, [msg["id"]])
    assert result.outcome == dw.REFUSED
    assert "cap" in result.reason
    assert paseo.calls() == []


def test_auto_continue_cap_lapses_after_the_window(ws, vm, paseo, home):
    key = _session(ws, vm, paseo_agent_id="agent-1")
    for _ in range(3):
        m.bump_auto_continue(key)
    paseo.set_agent("agent-1", "idle")
    msg = _wake_msg(ws, key)
    waker = _waker(vm, paseo, home, cap=3)
    assert waker.wake(key, [msg["id"]]).outcome == dw.REFUSED

    with psycopg.connect(_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE agent_sessions SET auto_continue_at = now() - interval '2 hours' "
            "WHERE session_key = %s",
            (key,),
        )
    result = waker.wake(key, [msg["id"]])
    assert result.outcome == dw.SENT
    # The send restarted the counter rather than going 3 -> 4.
    assert m.get_session(key)["auto_continues"] == 1


def test_session_mapped_from_paseo_state_is_stored_and_woken(ws, vm, paseo, home):
    sid = str(uuid.uuid4())
    key = _session(ws, vm, session_id=sid)  # imported thread: no PASEO_AGENT_ID
    assert m.get_session(key)["wake_policy"] == "none"
    record = {
        "id": "agent-imported",
        "provider": "codex",
        "persistence": {"sessionId": sid, "nativeHandle": sid, "metadata": {"threadId": sid}},
    }
    (home / "agents" / "proj").mkdir()
    (home / "agents" / "proj" / "agent-imported.json").write_text(json.dumps(record))
    paseo.set_agent("agent-imported", "idle")
    msg = _wake_msg(ws, key)

    result = _waker(vm, paseo, home).wake(key, [msg["id"]])

    assert result.outcome == dw.SENT, result.reason
    assert result.agent_id == "agent-imported"
    row = m.get_session(key)
    assert row["paseo_agent_id"] == "agent-imported"
    assert row["wake_policy"] == "direct"


def test_unhosted_session_is_left_for_l1(ws, vm, paseo, home):
    key = _session(ws, vm)
    msg = _wake_msg(ws, key)
    result = _waker(vm, paseo, home).wake(key, [msg["id"]])
    assert result.outcome == dw.NOT_HOSTED
    assert paseo.calls() == []


def test_send_failure_leaves_the_message_deliverable(ws, vm, paseo, home):
    key = _session(ws, vm, paseo_agent_id="agent-1")
    paseo.set_agent("agent-1", "idle")
    paseo.set_send_exit(1)
    msg = _wake_msg(ws, key)
    result = _waker(vm, paseo, home).wake(key, [msg["id"]])
    assert result.outcome == dw.FAILED
    assert len(paseo.sends()) == 1
    row = m.get_message(msg["id"])
    assert row["claimed_by"] is None and row["claimed_at"] is None
    assert m.get_session(key)["auto_continues"] == 0
    assert msg["id"] in {r["id"] for r in m.claim_deliverable(key)}


def test_already_claimed_message_is_skipped(ws, vm, paseo, home):
    key = _session(ws, vm, paseo_agent_id="agent-1")
    paseo.set_agent("agent-1", "idle")
    msg = _wake_msg(ws, key)
    m.claim_deliverable(key)  # the Stop hook or an await got it first
    result = _waker(vm, paseo, home).wake(key, [msg["id"]])
    assert result.outcome == dw.SKIPPED
    assert paseo.calls() == []


def test_session_on_another_vm_is_not_woken(ws, vm, paseo, home):
    key = _session(ws, "elsewhere", paseo_agent_id="agent-1")
    paseo.set_agent("agent-1", "idle")
    msg = _wake_msg(ws, key)
    result = _waker(vm, paseo, home).wake(key, [msg["id"]])
    assert result.outcome == dw.NOT_HOSTED
    assert paseo.calls() == []


# ---------------------------------------------------------------------------
# Paseo helpers (no database)
# ---------------------------------------------------------------------------


def test_agy_plugin_session_ids_map(home):
    conv = str(uuid.uuid4())
    plugin = "plugin:" + json.dumps({"version": 1, "data": {"conversationId": conv}})
    (home / "agents" / "p").mkdir()
    (home / "agents" / "p" / "agy-agent.json").write_text(
        json.dumps({"id": "agy-agent", "persistence": {"sessionId": plugin}})
    )
    (home / "agents" / "p" / "broken.json").write_text("{not json")
    assert find_agent_for_session(conv, home=home) == "agy-agent"
    assert find_agent_for_session("nope", home=home) is None


def test_find_paseo_and_home_honour_env(tmp_path, paseo):
    assert find_paseo({"AGENT_NOTES_PASEO_BIN": str(paseo.binary)}) == str(paseo.binary)
    assert find_paseo({"AGENT_NOTES_PASEO_BIN": str(tmp_path / "missing"), "PATH": ""}) is None
    assert find_paseo({"PATH": str(paseo.binary.parent)}) is not None
    assert paseo_home({"PASEO_HOME": str(tmp_path)}) == tmp_path


def test_client_rejects_option_like_agent_ids(paseo):
    client = PaseoClient(str(paseo.binary))
    with pytest.raises(Exception, match="not a Paseo agent id"):
        client.send("--host=evil", "hi")
    assert paseo.calls() == []


# ---------------------------------------------------------------------------
# Scheduling (stub waker, fake clock)
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _StubWaker:
    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, list[int]]] = []

    def wake(self, session_key, ids):
        self.calls.append((session_key, sorted(ids)))
        return dw.WakeResult(self.outcome, "stub", session_key, tuple(ids))


def test_notification_waits_the_grace_delay_and_groups_by_session():
    clock = _Clock()
    waker = _StubWaker(dw.SENT)
    d = Dispatcher(vm="vm1", waker=waker, handlers=None, clock=clock, wake_delay=1.0)

    def note(mid: int, to: str, wake: bool = True, kind: str = "message") -> str:
        return json.dumps({"id": mid, "kind": kind, "to_session": to, "wake": wake})

    d.on_notify(note(1, "vm1/codex/a", kind="reply"))
    d.on_notify(note(2, "vm1/codex/a"))
    d.on_notify(note(3, "vm2/codex/a"))
    d.on_notify(note(4, "vm1/codex/b", wake=False))
    d.on_notify("not json")
    d.process_wakes()
    assert waker.calls == []
    clock.now += 1.0
    d.process_wakes()
    assert waker.calls == [("vm1/codex/a", [1, 2])]
    d.on_notify(json.dumps({"id": 1, "kind": "reply", "to_session": "vm1/codex/a", "wake": True}))
    clock.now += 5
    d.process_wakes()
    assert len(waker.calls) == 1  # settled messages are not retried


def test_deferred_wake_is_rechecked_then_left_for_l1():
    clock = _Clock()
    waker = _StubWaker(dw.DEFERRED)
    d = Dispatcher(
        vm="vm1", waker=waker, handlers=None, clock=clock, sweep_interval=30, defer_window=600
    )
    d.schedule_wake(7, "vm1/codex/a")
    clock.now += 1
    d.process_wakes()
    assert len(waker.calls) == 1
    clock.now += 10
    d.process_wakes()
    assert len(waker.calls) == 1  # not before the next sweep interval
    for _ in range(25):
        clock.now += 30
        d.process_wakes()
    assert 7 not in d._pending and 7 in d._settled
    assert 15 <= len(waker.calls) <= 22


def test_request_notifications_mark_the_queue_dirty():
    d = Dispatcher(vm="vm1", waker=None, handlers={}, clock=_Clock())
    d._requests_dirty = False
    d.on_notify(json.dumps({"id": 9, "kind": "request", "to_vm": "vm2"}))
    assert d._requests_dirty is False
    d.on_notify(json.dumps({"id": 9, "kind": "request", "to_vm": "vm1"}))
    assert d._requests_dirty is True


# ---------------------------------------------------------------------------
# Sweep and requests (real database)
# ---------------------------------------------------------------------------


def test_sweep_picks_up_a_missed_notification(ws, vm, paseo, home):
    key = _session(ws, vm, paseo_agent_id="agent-1")
    paseo.set_agent("agent-1", "idle")
    msg = _wake_msg(ws, key)
    d = Dispatcher(vm=vm, waker=_waker(vm, paseo, home), handlers=None)
    d.sweep()
    results = d.process_wakes(force=True)
    assert [r.outcome for r in results] == [dw.SENT]
    assert m.get_message(msg["id"])["claimed_by"] == f"{vm}/dispatcher"


def _request(ws: int, vm: str, requester: str | None, job: str = "echo", **payload) -> dict:
    return m.send_message(
        workspace_id=ws,
        from_session=requester,
        from_vm="req-vm",
        from_project="proj-x",
        to_vm=vm,
        kind="request",
        body="please",
        payload={"job": job, **payload},
    )


def _reply_to(request_id: int) -> dict:
    replies = m.replies_to(request_id)
    assert len(replies) == 1, replies
    return replies[0]


def test_requests_are_claimed_and_answered_with_a_wake_reply(ws, vm):
    requester = _session(ws, "req-vm")
    seen: list[int] = []

    def echo(request, ctx):
        seen.append(request["id"])
        assert ctx.vm == vm
        return dj.JobReply(body=f"echo {request['id']}", payload={"status": "ok"})

    ok = _request(ws, vm, requester)
    unknown = _request(ws, vm, requester, job="launch-missiles")
    other_vm = _request(ws, "not-" + vm, requester)
    Dispatcher(vm=vm, waker=None, handlers={"echo": echo}).run_once()

    assert seen == [ok["id"]]
    reply = _reply_to(ok["id"])
    assert reply["to_session"] == requester and reply["wake"] is True
    assert reply["body"] == f"echo {ok['id']}"
    assert reply["from_project"] == "proj-x"
    bad = _reply_to(unknown["id"])
    assert bad["payload"]["error_code"] == dj.JOB_TYPE_UNSUPPORTED
    assert bad["wake"] is True
    assert m.get_message(other_vm["id"])["claimed_by"] is None


def test_handler_exception_still_replies(ws, vm):
    requester = _session(ws, "req-vm")

    def boom(request, ctx):
        raise KeyError("secret-ish detail")

    req = _request(ws, vm, requester)
    Dispatcher(vm=vm, waker=None, handlers={"echo": boom}).run_once()
    reply = _reply_to(req["id"])
    assert reply["payload"]["error_code"] == dj.JOB_FAILED
    assert "secret-ish" not in reply["body"]


def test_reply_without_requester_session_goes_to_the_project_inbox(ws, vm):
    req = _request(ws, vm, None)
    Dispatcher(
        vm=vm, waker=None, handlers={"echo": lambda r, c: dj.JobReply(body="done")}
    ).run_once()
    reply = _reply_to(req["id"])
    assert reply["to_session"] is None and reply["to_project"] == "proj-x"
    assert reply["wake"] is False


def test_at_most_two_jobs_run_at_once(ws, vm):
    requester = _session(ws, "req-vm")
    lock = threading.Lock()
    running = {"now": 0, "max": 0}

    def slow(request, ctx):
        with lock:
            running["now"] += 1
            running["max"] = max(running["max"], running["now"])
        time.sleep(0.4)
        with lock:
            running["now"] -= 1
        return dj.JobReply(body="ok")

    reqs = [_request(ws, vm, requester) for _ in range(5)]
    Dispatcher(vm=vm, waker=None, handlers={"echo": slow}, max_jobs=2).run_once()
    assert running["max"] == 2
    for req in reqs:
        _reply_to(req["id"])


def test_interrupted_requests_are_answered_at_startup(ws, vm):
    requester = _session(ws, "req-vm")
    req = _request(ws, vm, requester)
    assert m.claim_requests(vm, claimer=f"{vm}/dispatcher")
    Dispatcher(vm=vm, waker=None, handlers={"echo": lambda r, c: None}).run_once()
    reply = _reply_to(req["id"])
    assert reply["payload"]["error_code"] == dj.JOB_INTERRUPTED


# ---------------------------------------------------------------------------
# The LISTEN loop end to end
# ---------------------------------------------------------------------------


def _dsn() -> str:
    from agent_notes.core.config import resolve_dsn

    return resolve_dsn()


def test_listen_loop_wakes_and_holds_the_vm_lock(ws, vm, paseo, home):
    key = _session(ws, vm, paseo_agent_id="agent-1")
    paseo.set_agent("agent-1", "idle")
    requester = _session(ws, "req-vm")
    d = Dispatcher(
        vm=vm,
        waker=_waker(vm, paseo, home),
        handlers={"echo": lambda r, c: dj.JobReply(body="echoed")},
        wake_delay=0.2,
    )
    stop = threading.Event()
    thread = threading.Thread(target=d.run, args=(stop,), daemon=True)
    thread.start()
    try:
        assert d.listening.wait(10), "the dispatcher never connected"
        with pytest.raises(AlreadyRunningError):
            Dispatcher(vm=vm, waker=None, handlers=None)._connect()

        time.sleep(0.5)  # past the startup sweep, so only NOTIFY can deliver
        msg = _wake_msg(ws, key)
        req = _request(ws, vm, requester)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if paseo.sends() and m.replies_to(req["id"]):
                break
            time.sleep(0.1)
        assert paseo.sends(), "no paseo send within 15s"
        assert m.get_message(msg["id"])["claimed_by"] == f"{vm}/dispatcher"
        assert m.replies_to(req["id"])[0]["body"] == "echoed"
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    with psycopg.connect(_dsn(), autocommit=True) as conn:
        conn.execute("SELECT 1")


# ---------------------------------------------------------------------------
# Review fixes, round 2 (Codex findings 5, 6, 7, 8)
# ---------------------------------------------------------------------------


def _fail(*_a, **_k):
    raise RuntimeError("injected failure")


def _oserror(*_a, **_k):
    raise OSError("injected disk failure")


@pytest.mark.parametrize("where", ["mkstemp", "write", "send"])
def test_any_send_failure_releases_the_claim(ws, vm, paseo, home, monkeypatch, where):
    # Finding 5: an OSError from the prompt file used to escape the release.
    from agent_notes.dispatcher import paseo as dp

    key = _session(ws, vm, paseo_agent_id="agent-1")
    paseo.set_agent("agent-1", "idle")
    msg = _wake_msg(ws, key)
    if where == "mkstemp":
        monkeypatch.setattr(dp.tempfile, "mkstemp", _oserror)
    elif where == "write":
        monkeypatch.setattr(dp.os, "fdopen", _oserror)
    else:
        monkeypatch.setattr(PaseoClient, "send", _fail)
    result = _waker(vm, paseo, home).wake(key, [msg["id"]])
    assert result.outcome == dw.FAILED
    assert m.get_message(msg["id"])["claimed_by"] is None
    assert m.get_session(key)["auto_continues"] == 0
    assert paseo.sends() == []


_SHAPE_CHECKS = ("agent_messages_payload_object", "agent_messages_subject_length")
_MIGRATION_853 = Path(__file__).resolve().parents[1] / "schema" / "853_agent_messages_shape.sql"


@pytest.fixture
def pre_853():
    """The database as it was before migration 853; 853 is re-applied after."""
    with psycopg.connect(_dsn(), autocommit=True) as conn:
        for name in _SHAPE_CHECKS:
            conn.execute(f"ALTER TABLE agent_messages DROP CONSTRAINT IF EXISTS {name}")
    try:
        yield
    finally:
        with psycopg.connect(_dsn(), autocommit=True) as conn:
            conn.execute(_MIGRATION_853.read_text(encoding="utf-8"))


def _raw_request(ws: int, vm: str, requester: str, payload: str, subject: str | None = None) -> int:
    with psycopg.connect(_dsn(), autocommit=True) as conn:
        return conn.execute(
            "INSERT INTO agent_messages (workspace_id, from_session, from_vm, to_vm, kind, "
            "subject, body, payload) VALUES (%s, %s, 'req-vm', %s, 'request', %s, 'please', "
            "%s::jsonb) RETURNING id",
            (ws, requester, vm, subject, payload),
        ).fetchone()[0]


def _review_dispatcher(vm: str) -> Dispatcher:
    return Dispatcher(vm=vm, waker=None, handlers={"review": lambda r, c: dj.JobReply(body="x")})


@pytest.mark.parametrize("payload", ['["review"]', '"just a string"', '{"job": 7}'])
def test_malformed_payload_gets_bad_request_instead_of_a_crash(ws, vm, pre_853, payload):
    # Finding 6: a non-object payload raised AttributeError before the job ran.
    requester = _session(ws, "req-vm")
    rid = _raw_request(ws, vm, requester, payload)
    _review_dispatcher(vm).run_once()
    reply = _reply_to(rid)
    assert reply["payload"]["error_code"] == dj.BAD_REQUEST
    assert reply["to_session"] == requester


def test_malformed_interrupted_request_is_answered_at_startup(ws, vm, pre_853):
    # ...and again in the interrupted-request recovery, on every restart.
    requester = _session(ws, "req-vm")
    rid = _raw_request(ws, vm, requester, '["review"]')
    assert m.claim_requests(vm, claimer=f"{vm}/dispatcher")
    _review_dispatcher(vm).run_once()
    assert _reply_to(rid)["payload"]["error_code"] == dj.JOB_INTERRUPTED


def test_migration_853_brings_old_rows_into_shape(ws, vm, pre_853):
    requester = _session(ws, "req-vm")
    bad_payload = _raw_request(ws, vm, requester, '["review"]')
    long_subject = _raw_request(ws, vm, requester, "{}", subject="s" * 12000)
    with psycopg.connect(_dsn(), autocommit=True) as conn:
        conn.execute(_MIGRATION_853.read_text(encoding="utf-8"))
        conn.execute(_MIGRATION_853.read_text(encoding="utf-8"))  # idempotent
        checks = conn.execute(
            "SELECT conname, convalidated FROM pg_constraint "
            "WHERE conrelid = 'agent_messages'::regclass AND conname = ANY(%s)",
            (list(_SHAPE_CHECKS),),
        ).fetchall()
    assert sorted(checks) == sorted((name, True) for name in _SHAPE_CHECKS)
    assert m.get_message(bad_payload)["payload"] == {"invalid_payload": ["review"]}
    assert len(m.get_message(long_subject)["subject"]) == 512
    # Both stay claimable, and the old malformed request is answered.
    _review_dispatcher(vm).run_once()
    assert _reply_to(bad_payload)["payload"]["error_code"] == dj.BAD_REQUEST


def test_unposted_reply_is_kept_and_posted_by_a_later_sweep(ws, vm, monkeypatch):
    # Finding 7: after three failed writes the verdict used to be lost.
    requester = _session(ws, "req-vm")
    req = _request(ws, vm, requester)
    monkeypatch.setattr(dj, "_REPLY_BACKOFF", 0.0)
    real = m.answer_request
    monkeypatch.setattr(m, "answer_request", _fail)
    d = Dispatcher(
        vm=vm, waker=None, handlers={"echo": lambda r, c: dj.JobReply(body="the verdict")}
    )
    assert d.poll_requests() == 1
    assert d.wait_for_jobs(30)
    assert m.replies_to(req["id"]) == []
    assert d.answer_unreplied() == 0  # still failing: kept, not replaced
    monkeypatch.setattr(m, "answer_request", real)
    assert d.answer_unreplied() == 1
    assert _reply_to(req["id"])["body"] == "the verdict"
    assert d.answer_unreplied() == 0


def test_sweep_answers_stale_claims_but_not_running_jobs(ws, vm):
    requester = _session(ws, "req-vm")
    release = threading.Event()

    def slow(request, ctx):
        release.wait(30)
        return dj.JobReply(body="slow verdict")

    d = Dispatcher(vm=vm, waker=None, handlers={"echo": slow})
    running = _request(ws, vm, requester)
    assert d.poll_requests() == 1
    stale = _request(ws, vm, requester)
    assert m.claim_requests(vm, claimer=d.claimer)  # an earlier instance's, never answered
    try:
        assert d.answer_unreplied() == 1
        assert _reply_to(stale["id"])["payload"]["error_code"] == dj.JOB_INTERRUPTED
        assert m.replies_to(running["id"]) == []
    finally:
        release.set()
    assert d.wait_for_jobs(30)
    assert _reply_to(running["id"])["body"] == "slow verdict"


def test_a_request_is_never_answered_twice(ws, vm):
    requester = _session(ws, "req-vm")
    req = _request(ws, vm, requester)
    ctx = dj.JobContext(vm=vm)
    assert dj.post_reply(req, dj.failure_reply(req, dj.JOB_INTERRUPTED, "gone"), ctx)
    assert dj.post_reply(req, dj.JobReply(body="late verdict"), ctx)
    reply = _reply_to(req["id"])
    assert reply["payload"]["error_code"] == dj.JOB_INTERRUPTED


def test_shutdown_holds_the_vm_lock_until_jobs_finish(ws, vm):
    # Finding 8: the lock connection used to close before the workers finished.
    requester = _session(ws, "req-vm")
    started, release = threading.Event(), threading.Event()

    def slow(request, ctx):
        started.set()
        release.wait(30)
        return dj.JobReply(body="late verdict")

    d = Dispatcher(vm=vm, waker=None, handlers={"echo": slow}, shutdown_grace=20)
    stop = threading.Event()
    thread = threading.Thread(target=d.run, args=(stop,), daemon=True)
    thread.start()
    req = _request(ws, vm, requester)
    try:
        assert started.wait(15), "the job never started"
        stop.set()
        time.sleep(1.5)  # past the loop's wait: it is draining now
        assert thread.is_alive()
        with pytest.raises(AlreadyRunningError):
            Dispatcher(vm=vm, waker=None, handlers=None)._connect()
    finally:
        release.set()
        thread.join(timeout=30)
    assert not thread.is_alive()
    assert _reply_to(req["id"])["body"] == "late verdict"
