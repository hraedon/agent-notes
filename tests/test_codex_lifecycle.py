"""Codex lifecycle adapter contract tests (Plan 019 Phase 2)."""

from __future__ import annotations

import argparse
import io
import json
import os
import sys

from agent_notes import codex_lifecycle as lifecycle


def _session_payload(tmp_path, **extra):
    return {
        "session_id": "session-1",
        "transcript_path": "/tmp/transcript-with-secret",
        "cwd": str(tmp_path),
        "hook_event_name": "SessionStart",
        "model": "gpt-test",
        "permission_mode": "default",
        "source": "startup",
        "prompt": "password=never-read",
        **extra,
    }


def _stop_payload(tmp_path, **extra):
    return {
        "session_id": "session-1",
        "turn_id": "turn-1",
        "transcript_path": "/tmp/transcript-with-secret",
        "cwd": str(tmp_path),
        "hook_event_name": "Stop",
        "model": "gpt-test",
        "permission_mode": "default",
        "stop_hook_active": False,
        "last_assistant_message": "api_key=never-read",
        **extra,
    }


def test_session_start_injects_only_bounded_orientation(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        lifecycle,
        "_orientation_for_cwd",
        lambda _cwd: "safe project orientation",
    )

    result = lifecycle.run_session_start(_session_payload(tmp_path))

    assert result == {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "safe project orientation",
        },
    }
    serialized = json.dumps(result)
    assert "never-read" not in serialized
    assert "transcript" not in serialized


def test_session_start_unregistered_or_unavailable_never_blocks(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(lifecycle, "_orientation_for_cwd", lambda _cwd: None)
    assert lifecycle.run_session_start(_session_payload(tmp_path)) == {"continue": True}

    def unavailable(_cwd):
        raise RuntimeError("dsn=postgresql://user:password@example.invalid/db")

    monkeypatch.setattr(lifecycle, "_orientation_for_cwd", unavailable)
    assert lifecycle.run_session_start(_session_payload(tmp_path)) == {"continue": True}


def test_orientation_renderer_redacts_and_bounds_metadata() -> None:
    payload = {
        "project": "demo",
        "workspace": "default",
        "open_work_items": [
            {
                "identifier": f"WI-{index}",
                "status": "open",
                "severity": "medium",
                "title": "api_key=sk-abcdefghijklmnopqrstuvwxyz " + ("x" * 900),
            }
            for index in range(30)
        ],
        "recent_changes": [],
        "memories": [{"name": "password=do-not-print", "type": "decision"}],
        "learned_context": {"results": [{"text": "token=also-do-not-print"}]},
        "resolved_in_git": [],
        "regista_sync": {"enabled": False},
    }

    context = lifecycle._format_orientation(payload)

    assert len(context) <= lifecycle.MAX_ORIENTATION_CHARS
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in context
    assert "do-not-print" not in context
    assert "also-do-not-print" not in context
    assert "WI-10" not in context  # renderer is deliberately capped at ten items


def test_stop_reconciles_once_and_never_requests_continuation(monkeypatch, tmp_path) -> None:
    calls = []
    monkeypatch.setattr(lifecycle, "_reconcile_cwd", lambda cwd: calls.append(cwd))

    assert lifecycle.run_stop(_stop_payload(tmp_path)) == {"continue": True}
    assert calls == [tmp_path]

    assert lifecycle.run_stop(_stop_payload(tmp_path, stop_hook_active=True)) == {"continue": True}
    assert calls == [tmp_path]


def test_stop_failure_and_malformed_input_always_emit_valid_json(
    monkeypatch, capsys, tmp_path
) -> None:
    def fail(_cwd):
        raise RuntimeError("password=must-not-escape")

    monkeypatch.setattr(lifecycle, "_reconcile_cwd", fail)
    assert lifecycle.run_stop(_stop_payload(tmp_path)) == {"continue": True}

    monkeypatch.setattr(sys, "stdin", io.StringIO("not-json"))
    code = lifecycle.cmd_codex_hook(argparse.Namespace(codex_hook_event="stop"))
    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"continue": True}


def test_wrong_event_and_relative_cwd_are_non_blocking(tmp_path) -> None:
    assert lifecycle.run_session_start(_session_payload(tmp_path, hook_event_name="Stop")) == {
        "continue": True
    }
    payload = _stop_payload(tmp_path)
    payload["cwd"] = "relative/path"
    assert lifecycle.run_stop(payload) == {"continue": True}


def test_session_id_is_forwarded_as_codex_session_env(monkeypatch, tmp_path) -> None:
    """WI-067: the Codex session_id keys the session-scoped identity record."""
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    lifecycle.run_session_start(_session_payload(tmp_path))
    assert os.environ.get("CODEX_SESSION_ID") == "session-1"

    # The payload's session_id is authoritative within the hook process: a
    # stale env value is overwritten, not preserved (a re-used process must
    # not attribute a session record to the previous session).
    monkeypatch.setenv("CODEX_SESSION_ID", "stale-session")
    lifecycle.run_session_start(_session_payload(tmp_path, session_id="fresh-session"))
    assert os.environ["CODEX_SESSION_ID"] == "fresh-session"
    lifecycle.run_stop(_stop_payload(tmp_path, session_id="stop-session"))
    assert os.environ["CODEX_SESSION_ID"] == "stop-session"

    # A payload with no session id clears a stale env value: a hook
    # invocation without a session must not keep attributing identity to a
    # dead session.
    monkeypatch.setenv("CODEX_SESSION_ID", "stale-session")
    lifecycle.run_session_start({"cwd": str(tmp_path), "hook_event_name": "SessionStart"})
    assert "CODEX_SESSION_ID" not in os.environ
