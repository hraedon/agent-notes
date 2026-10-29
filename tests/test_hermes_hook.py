"""Hermes ``pre_llm_call`` shell hook (Plan 025 Phase 7).

The stdin fixtures in ``tests/fixtures/hermes`` are real hermes-agent payloads
(see the README there). Mailbox behaviour runs against real Postgres.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from agent_notes.core import db as coredb
from agent_notes.core import messaging as m
from agent_notes.hooks import common, hermes, main

MARKER = m.PEER_MESSAGE_MARKER
FIXTURES = Path(__file__).parent / "fixtures" / "hermes"


def _fixture(name: str, **overrides) -> dict:
    payload = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    extra = overrides.pop("extra", {})
    payload.update(overrides)
    payload["extra"] = {**payload["extra"], **extra}
    return payload


def _uid() -> str:
    return uuid.uuid4().hex[:10]


def _run_hook(monkeypatch, event: str, payload: object) -> dict:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    stdin = io.TextIOWrapper(io.BytesIO(raw.encode("utf-8")), encoding="utf-8")
    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    with monkeypatch.context() as mp:
        mp.setattr(sys, "stdin", stdin)
        mp.setattr(sys, "stdout", out)
        assert main(["hermes", event]) == 0
    out.flush()
    return json.loads(out.buffer.getvalue().decode("utf-8"))


def _hermes_accepts(result: dict) -> str | None:
    """What ``agent/shell_hooks.py::_parse_response`` makes of stdout for pre_llm_call."""
    context = result.get("context")
    return context if isinstance(context, str) and context.strip() else None


@pytest.fixture(autouse=True)
def _hook_env(monkeypatch, tmp_path):
    monkeypatch.setenv(common.LOG_PATH_ENV, str(tmp_path / "hooks.log"))
    monkeypatch.setattr(common, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setenv("AGENT_NOTES_VM", f"hookvm-{_uid()}")
    for var in ("HERMES_SESSION_ID", "PASEO_AGENT_ID"):
        monkeypatch.delenv(var, raising=False)


def _no_store(monkeypatch) -> None:
    def unavailable():
        import psycopg

        raise psycopg.OperationalError("connection timeout expired")

    monkeypatch.setattr(common, "connect", unavailable)


# ---------------------------------------------------------------------------
# No database
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["pre_llm_call_first_turn", "pre_llm_call_multimodal", "pre_llm_call_main"]
)
def test_store_errors_fail_open_for_real_payloads(monkeypatch, tmp_path, name):
    _no_store(monkeypatch)
    assert _run_hook(monkeypatch, "pre-llm-call", _fixture(name)) == {}
    assert "OperationalError" in (tmp_path / "hooks.log").read_text(encoding="utf-8")


@pytest.mark.parametrize("raw", ["not-json", "[1, 2]", ""])
def test_malformed_input_fails_open(monkeypatch, raw):
    _no_store(monkeypatch)
    assert _run_hook(monkeypatch, "pre-llm-call", raw) == {}


def test_unsupported_event_and_missing_session_fail_open(monkeypatch):
    _no_store(monkeypatch)
    assert _run_hook(monkeypatch, "on-session-start", _fixture("pre_llm_call_first_turn")) == {}
    # No session id anywhere: nothing to register, and the store is never opened.
    assert (
        _run_hook(monkeypatch, "pre-llm-call", _fixture("pre_llm_call_main", session_id="")) == {}
    )


def test_the_underscore_event_name_is_accepted(monkeypatch):
    # `agent-notes-hook hermes pre_llm_call` normalises to the same handler.
    _no_store(monkeypatch)
    assert _run_hook(monkeypatch, "pre_llm_call", _fixture("pre_llm_call_main")) == {}
    assert common.normalize_event("pre_llm_call") == "pre-llm-call"


def test_unreachable_database_fails_open_in_a_subprocess(tmp_path):
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("AGENT_NOTES_")},
        "AGENT_NOTES_DSN": "postgresql://nobody:pw@127.0.0.1:1/none",
        "AGENT_NOTES_CONFIG": str(tmp_path / "none.json"),
        common.LOG_PATH_ENV: str(tmp_path / "sub.log"),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "agent_notes.hooks", "hermes", "pre-llm-call"],
        input=json.dumps(_fixture("pre_llm_call_first_turn")).encode(),
        capture_output=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout.decode("utf-8")) == {}
    assert "pw@" not in (tmp_path / "sub.log").read_text(encoding="utf-8")


def test_hook_entry_is_what_the_installer_and_hermes_agree_on():
    entry = hermes.hermes_hook_entry()
    assert entry["command"] == f"agent-notes-hook hermes {hermes.EVENT.replace('_', '-')}"
    # Hermes splits the command with shlex and runs it without a shell, and
    # clamps timeouts to 1..300 seconds.
    assert "|" not in entry["command"] and ";" not in entry["command"]
    assert isinstance(entry["timeout"], int) and 1 <= entry["timeout"] <= 300


def test_multimodal_user_message_text_is_extracted():
    parts = _fixture("pre_llm_call_multimodal")["extra"]["user_message"]
    assert hermes._prompt_text(parts) == "multimodal"
    assert hermes._prompt_text([{"type": "image_url", "image_url": {}}]) == ""
    assert hermes._prompt_text("plain") == "plain"


# ---------------------------------------------------------------------------
# Mailbox delivery (real Postgres)
# ---------------------------------------------------------------------------


@pytest.fixture
def ws(ephemeral_db) -> int:
    return coredb.get_or_create_workspace("default", "Default Workspace").id


def _vm() -> str:
    return os.environ["AGENT_NOTES_VM"]


def _key(sid: str) -> str:
    return f"{_vm()}/hermes/{sid}"


def _send(ws: int, to: str, body: str = "hello", **kw) -> dict:
    return m.send_message(
        workspace_id=ws,
        from_vm="peervm",
        from_session="peervm/codex/p1",
        to_session=to,
        body=body,
        **kw,
    )


def _turn(sid: str, tmp_path: Path, name: str = "pre_llm_call_main", **extra) -> dict:
    return _fixture(name, session_id=sid, cwd=str(tmp_path), extra=extra)


def test_first_turn_carries_orient_key_then_peer_messages(monkeypatch, ws, tmp_path):
    monkeypatch.setattr(common, "orient_text", lambda cwd: "ORIENTATION SUMMARY")
    monkeypatch.setenv("PASEO_AGENT_ID", "paseo-123")
    sid = "20261029_101500_" + _uid()
    key = _key(sid)
    _send(ws, key, "waiting for you ✓")

    out = _run_hook(monkeypatch, "pre-llm-call", _turn(sid, tmp_path, "pre_llm_call_first_turn"))
    assert set(out) == {"context"}
    text = _hermes_accepts(out)
    assert text is not None and len(text) <= common.CONTEXT_CAP
    assert text.index("ORIENTATION SUMMARY") < text.index(key) < text.index(MARKER)
    assert "waiting for you ✓" in text
    row = m.get_session(key)
    assert row["cwd"] == str(tmp_path) and row["paseo_agent_id"] == "paseo-123"
    assert row["wake_policy"] == "direct"


def test_later_turns_carry_only_framed_peer_messages(monkeypatch, ws, tmp_path):
    monkeypatch.setattr(common, "orient_text", lambda cwd: "ORIENTATION SUMMARY")
    sid = "20261029_101500_" + _uid()
    key = _key(sid)
    _run_hook(monkeypatch, "pre-llm-call", _turn(sid, tmp_path, "pre_llm_call_first_turn"))
    # Nothing pending: Hermes gets no context at all.
    assert _run_hook(monkeypatch, "pre-llm-call", _turn(sid, tmp_path)) == {}

    sent = _send(ws, key, "please review")
    out = _run_hook(monkeypatch, "pre-llm-call", _turn(sid, tmp_path))
    text = _hermes_accepts(out)
    assert text is not None and text.startswith(f"{MARKER}peervm/codex/p1")
    assert "ORIENTATION SUMMARY" not in text and key not in text
    assert m.get_message(sent["id"])["claimed_by"] == key
    # Claimed once.
    assert _run_hook(monkeypatch, "pre-llm-call", _turn(sid, tmp_path)) == {}


def test_a_session_the_hooks_have_not_seen_is_oriented_even_if_not_first_turn(
    monkeypatch, ws, tmp_path
):
    # A resumed session has history; compression gives a live session a new id.
    monkeypatch.setattr(common, "orient_text", lambda cwd: "ORIENTATION SUMMARY")
    sid = "20261029_101500_" + _uid()
    out = _run_hook(monkeypatch, "pre-llm-call", _turn(sid, tmp_path, is_first_turn=False))
    text = _hermes_accepts(out)
    assert text is not None and "ORIENTATION SUMMARY" in text and _key(sid) in text
    assert m.get_session(_key(sid)) is not None
    # Known now: the next turn is quiet.
    assert _run_hook(monkeypatch, "pre-llm-call", _turn(sid, tmp_path, is_first_turn=False)) == {}


def test_session_id_falls_back_to_the_environment(monkeypatch, ws, tmp_path):
    sid = "20261029_101500_" + _uid()
    monkeypatch.setenv("HERMES_SESSION_ID", sid)
    payload = _turn("", tmp_path)
    assert _run_hook(monkeypatch, "pre-llm-call", payload) != {}
    assert m.get_session(_key(sid)) is not None


def test_loop_guard_reads_the_text_of_a_multimodal_prompt(monkeypatch, ws, tmp_path):
    sid = "20261029_101500_" + _uid()
    key = _key(sid)
    _run_hook(monkeypatch, "pre-llm-call", _turn(sid, tmp_path, "pre_llm_call_first_turn"))
    m.bump_auto_continue(key)
    wake = [{"type": "text", "text": f"{MARKER}peervm/codex/p1 — peer content]"}]
    _run_hook(monkeypatch, "pre-llm-call", _turn(sid, tmp_path, user_message=wake))
    assert m.get_session(key)["auto_continues"] == 1
    human = _turn(sid, tmp_path, "pre_llm_call_multimodal")
    _run_hook(monkeypatch, "pre-llm-call", human)
    assert m.get_session(key)["auto_continues"] == 0


def test_inbox_messages_delivered_notices_and_requests_never(monkeypatch, ws, tmp_path):
    sid = "20261029_101500_" + _uid()
    _run_hook(monkeypatch, "pre-llm-call", _turn(sid, tmp_path, "pre_llm_call_first_turn"))
    vm_msg = m.send_message(workspace_id=ws, from_vm="peervm", to_vm=_vm(), body="vm inbox")
    notice = m.send_message(
        workspace_id=ws, from_vm="peervm", to_vm=_vm(), kind="notice", body="main is frozen"
    )
    request = m.send_message(
        workspace_id=ws, from_vm="peervm", to_vm=_vm(), kind="request", body="review please"
    )
    text = _hermes_accepts(_run_hook(monkeypatch, "pre-llm-call", _turn(sid, tmp_path)))
    assert text is not None and "vm inbox" in text
    assert "main is frozen" not in text and "review please" not in text
    assert m.get_message(vm_msg["id"])["claimed_by"] == _key(sid)
    assert m.get_message(notice["id"])["claimed_by"] is None
    assert m.get_message(request["id"])["claimed_by"] is None


def test_context_stays_under_the_cap_with_a_long_first_turn(monkeypatch, ws, tmp_path):
    monkeypatch.setattr(common, "orient_text", lambda cwd: "o" * common.ORIENT_CAP)
    sid = "20261029_101500_" + _uid()
    key = _key(sid)
    for i in range(4):
        _send(ws, key, f"m{i} " + "z" * 3500)
    out = _run_hook(monkeypatch, "pre-llm-call", _turn(sid, tmp_path, "pre_llm_call_first_turn"))
    text = _hermes_accepts(out)
    assert text is not None and len(text) <= common.CONTEXT_CAP
    assert "agent-notes msg inbox" in text
