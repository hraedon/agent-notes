"""Codex lifecycle adapter contract tests (Plan 019 Phase 2)."""

from __future__ import annotations

import argparse
import io
import json
import sys

from agent_notes import codex_lifecycle as lifecycle
from agent_notes.hooks import common


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


def _no_store(monkeypatch) -> None:
    def unavailable():
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(common, "connect", unavailable)


def test_session_start_injects_only_bounded_orientation(monkeypatch, tmp_path) -> None:
    _no_store(monkeypatch)
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
    _no_store(monkeypatch)
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
    }

    context = lifecycle._format_orientation(payload)

    assert len(context) <= lifecycle.MAX_ORIENTATION_CHARS
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in context
    assert "do-not-print" not in context
    assert "also-do-not-print" not in context
    assert "WI-5" not in context  # renderer is deliberately capped at five items
    assert "more available" in context


def test_orientation_freshness_is_visible_without_exposing_handoff_text() -> None:
    payload = {
        "generated_at": "2026-10-15T00:00:00+00:00",
        "latest_reflection": {
            "name": "password=must-not-enter-hook",
            "age_days": 80,
            "updated_at": "2026-07-27T00:00:00+00:00",
            "freshness": "aged",
        },
        "open_work_items": [
            {
                "identifier": "WI-001",
                "severity": "high",
                "status": "open",
                "age_days": 40,
                "freshness": "aged",
            }
        ],
        "has_more": {"open_work_items": True},
    }
    context = lifecycle._format_orientation(payload)
    assert "2026-10-15" in context and "2026-07-27" in context
    assert "40d; aged" in context
    assert "must-not-enter-hook" not in context


def test_stop_without_a_store_never_requests_continuation(monkeypatch, tmp_path) -> None:
    def unavailable():
        raise RuntimeError("dsn=postgresql://user:password@example.invalid/db")

    monkeypatch.setattr(common, "connect", unavailable)
    assert lifecycle.run_stop(_stop_payload(tmp_path)) == {"continue": True}
    assert lifecycle.run_stop(_stop_payload(tmp_path, stop_hook_active=True)) == {"continue": True}


def test_stop_failure_and_malformed_input_always_emit_valid_json(
    monkeypatch, capsys, tmp_path
) -> None:
    def fail():
        raise RuntimeError("password=must-not-escape")

    monkeypatch.setattr(common, "connect", fail)
    assert lifecycle.run_stop(_stop_payload(tmp_path)) == {"continue": True}

    monkeypatch.setattr(sys, "stdin", io.StringIO("not-json"))
    code = lifecycle.cmd_codex_hook(argparse.Namespace(codex_hook_event="stop"))
    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"continue": True}


def test_wrong_event_and_relative_cwd_are_non_blocking(monkeypatch, tmp_path) -> None:
    _no_store(monkeypatch)
    assert lifecycle.run_session_start(_session_payload(tmp_path, hook_event_name="Stop")) == {
        "continue": True
    }
    payload = _stop_payload(tmp_path)
    payload["cwd"] = "relative/path"
    assert lifecycle.run_stop(payload) == {"continue": True}
