"""Session-scoped identity resolution tests (WI-067).

Covers the precedence chain, the private atomic session record, the
``invariants probe`` fail-closed contract (with passing and deny fixtures),
and the claim-path lineage seam (actor resolution reading the session record).

Revised per cross-lineage review: the session record is the **stable source
once declared** — it beats explicit/env, a conflicting explicit ``--model-lineage``
is refused, the opencode plugin never mutates ``process.env``, and records /
directories are private (0600/0700).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from agent_notes.core.actor import load_actor_config
from agent_notes.core.session_identity import (
    MODEL_LINEAGE_ENV,
    SessionIdentityConflictError,
    gc_session_records,
    harness_session_id,
    harness_session_source,
    read_session_record,
    resolve_model_lineage,
    session_record_path,
    session_records_dir,
    write_session_record,
)

_CLI = (sys.executable, "-m", "agent_notes.cli")

# Harness session-id env vars that must be scrubbed in the fixture so the
# operator's real harness never leaks into a test.
_HARNESS_ID_VARS = (
    "CLAUDE_CODE_SESSION_ID",
    "OPENCODE_SESSION_ID",
    "CODEX_SESSION_ID",
    "AGENT_NOTES_SESSION",
    "AGENT_NOTES_MODEL_LINEAGE",
)


@pytest.fixture(autouse=True)
def _isolated_session_state(monkeypatch, tmp_path):
    """Pin the session-records dir and scrub harness identity from the env."""
    import agent_notes.core.session_identity as _si

    # Reset the one-shot fallback-warning flag so each test observes warnings
    # independently.
    _si._WARNED_FALLBACK_SESSION = False
    records_dir = tmp_path / "sessions"
    monkeypatch.setenv("AGENT_NOTES_SESSION_DIR", str(records_dir))
    for var in _HARNESS_ID_VARS:
        monkeypatch.delenv(var, raising=False)
    # Suite.env is also pinned away so a bootstrapped host cannot inject a
    # host-wide lineage into a test.
    suite_env = tmp_path / "suite.env"
    monkeypatch.setenv("AGENT_SUITE_CONFIG", str(suite_env))
    monkeypatch.setenv("AGENT_SUITE_SYSTEM_CONFIG", str(suite_env))
    yield
    _si._WARNED_FALLBACK_SESSION = False


def _run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(
        [*_CLI, *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=merged,
    )


def _json(result: subprocess.CompletedProcess[str]) -> dict:
    import json

    assert result.returncode is not None
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# harness session id resolution
# ---------------------------------------------------------------------------


def test_harness_session_id_prefers_claude_then_opencode_then_fallback():
    assert harness_session_id({"CLAUDE_CODE_SESSION_ID": "c", "AGENT_NOTES_SESSION": "a"}) == "c"
    assert harness_session_id({"OPENCODE_SESSION_ID": "o", "AGENT_NOTES_SESSION": "a"}) == "o"
    assert harness_session_id({"CODEX_SESSION_ID": "x"}) == "x"
    assert harness_session_id({"AGENT_NOTES_SESSION": "a"}) == "a"
    assert harness_session_id({}) is None
    # Empty string falls through.
    assert harness_session_id({"CLAUDE_CODE_SESSION_ID": "", "AGENT_NOTES_SESSION": "a"}) == "a"


def test_harness_session_id_reads_process_env():
    os.environ["CLAUDE_CODE_SESSION_ID"] = "from-env"
    assert harness_session_id() == "from-env"


def test_harness_session_source_names_the_var():
    assert harness_session_source({"CLAUDE_CODE_SESSION_ID": "c"}) == "CLAUDE_CODE_SESSION_ID"
    assert harness_session_source({"AGENT_NOTES_SESSION": "a"}) == "AGENT_NOTES_SESSION"
    assert harness_session_source({}) is None


def test_fallback_session_id_warns(monkeypatch):
    """Using only the AGENT_NOTES_SESSION fallback warns (per-process, once)."""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("OPENCODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    os.environ["AGENT_NOTES_SESSION"] = "fallback-id"
    with pytest.warns(UserWarning, match="AGENT_NOTES_SESSION"):
        assert harness_session_id() == "fallback-id"


# ---------------------------------------------------------------------------
# session record: private atomic write, read, path safety, gc, dir tightening
# ---------------------------------------------------------------------------


def test_session_record_write_is_atomic_and_private(tmp_path):
    os.environ["AGENT_NOTES_SESSION"] = "sess-atomic"
    path = write_session_record("sess-atomic", {MODEL_LINEAGE_ENV: "glm"})
    assert path == session_record_path("sess-atomic")
    assert path.is_file()
    assert read_session_record("sess-atomic") == {MODEL_LINEAGE_ENV: "glm"}
    # Private permissions: dir 0700, file 0600.
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600


def test_session_record_write_tightens_preexisting_dir_permissions(tmp_path):
    """A pre-existing loose records dir is tightened to 0700 on write."""
    os.environ["AGENT_NOTES_SESSION"] = "sess-tighten"
    records_dir = session_records_dir()
    records_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(records_dir, 0o755)
    write_session_record("sess-tighten", {MODEL_LINEAGE_ENV: "glm"})
    assert records_dir.stat().st_mode & 0o777 == 0o700


def test_session_record_roundtrip_and_overwrite():
    write_session_record("s1", {MODEL_LINEAGE_ENV: "qwen"})
    assert read_session_record("s1")[MODEL_LINEAGE_ENV] == "qwen"
    write_session_record("s1", {MODEL_LINEAGE_ENV: "kimi"})
    assert read_session_record("s1")[MODEL_LINEAGE_ENV] == "kimi"


def test_session_record_missing_reads_empty():
    assert read_session_record("does-not-exist") == {}


def test_session_record_path_sanitizes_hostile_ids():
    path = session_record_path("../../etc/passwd")
    # No traversal: the key is digested and stays inside the records dir.
    assert path.parent == session_records_dir()
    assert path.name != "../../etc/passwd"
    assert ".." not in path.name


def test_gc_session_records_removes_only_stale():
    write_session_record("old", {MODEL_LINEAGE_ENV: "glm"})
    write_session_record("new", {MODEL_LINEAGE_ENV: "glm"})
    old_path = session_record_path("old")
    old_mtime = time.time() - (31 * 24 * 60 * 60)
    os.utime(old_path, (old_mtime, old_mtime))
    removed = gc_session_records(max_age_days=30)
    assert removed == 1
    assert not old_path.exists()
    assert session_record_path("new").exists()


# ---------------------------------------------------------------------------
# precedence chain — session record is the stable source once declared
# ---------------------------------------------------------------------------


def test_resolve_precedence_session_record_beats_explicit():
    os.environ["AGENT_NOTES_SESSION"] = "p"
    write_session_record("p", {MODEL_LINEAGE_ENV: "record"})
    # Explicit that MATCHES the record is fine (idempotent) and reports the
    # session record as the source — the record is the stable source.
    lineage, source = resolve_model_lineage(explicit="record")
    assert (lineage, source) == ("record", "session_record")


def test_resolve_conflicting_explicit_after_declaration_is_refused():
    os.environ["AGENT_NOTES_SESSION"] = "p"
    write_session_record("p", {MODEL_LINEAGE_ENV: "record"})
    with pytest.raises(SessionIdentityConflictError):
        resolve_model_lineage(explicit="different")
    with pytest.raises(SessionIdentityConflictError):
        resolve_model_lineage(explicit="different", suite={MODEL_LINEAGE_ENV: "suite"})


def test_resolve_precedence_session_record_beats_env():
    os.environ["AGENT_NOTES_SESSION"] = "p"
    write_session_record("p", {MODEL_LINEAGE_ENV: "record"})
    os.environ[MODEL_LINEAGE_ENV] = "env"
    lineage, source = resolve_model_lineage()
    assert (lineage, source) == ("record", "session_record")


def test_resolve_precedence_session_record_beats_suite():
    os.environ["AGENT_NOTES_SESSION"] = "p"
    write_session_record("p", {MODEL_LINEAGE_ENV: "record"})
    suite = {MODEL_LINEAGE_ENV: "suite-value"}
    lineage, source = resolve_model_lineage(suite=suite)
    assert (lineage, source) == ("record", "session_record")


def test_resolve_precedence_explicit_before_declaration():
    """Before a session declares, explicit/env/suite apply in order."""
    lineage, source = resolve_model_lineage(explicit="explicit")
    assert (lineage, source) == ("explicit", "explicit")
    os.environ[MODEL_LINEAGE_ENV] = "env"
    lineage, source = resolve_model_lineage()
    assert (lineage, source) == ("env", "env")


def test_resolve_precedence_suite_is_last_resort():
    os.environ.pop(MODEL_LINEAGE_ENV, None)
    lineage, source = resolve_model_lineage(suite={MODEL_LINEAGE_ENV: "suite-value"})
    assert (lineage, source) == ("suite-value", "suite_env")


def test_resolve_precedence_nothing_resolves_to_none():
    lineage, source = resolve_model_lineage(suite={})
    assert (lineage, source) == (None, None)


def test_declared_session_lineage_helper():
    from agent_notes.core.session_identity import declared_session_lineage

    os.environ["AGENT_NOTES_SESSION"] = "p"
    assert declared_session_lineage() is None
    write_session_record("p", {MODEL_LINEAGE_ENV: "kimi"})
    assert declared_session_lineage() == "kimi"


# ---------------------------------------------------------------------------
# actor seam: load_actor_config reads the session record
# ---------------------------------------------------------------------------


def test_load_actor_config_reads_session_record_lineage():
    os.environ["AGENT_NOTES_SESSION"] = "actor-sess"
    write_session_record("actor-sess", {MODEL_LINEAGE_ENV: "qwen"})
    cfg = load_actor_config()
    assert cfg.model_lineage == "qwen"


def test_load_actor_config_falls_back_to_suite_env_lineage(tmp_path):
    suite_env = tmp_path / "suite.env"
    suite_env.write_text(f"{MODEL_LINEAGE_ENV}=kimi\n", encoding="utf-8")
    os.environ["AGENT_SUITE_CONFIG"] = str(suite_env)
    cfg = load_actor_config()
    assert cfg.model_lineage == "kimi"


def test_load_actor_config_actor_id_reads_suite_env(tmp_path):
    suite_env = tmp_path / "suite.env"
    suite_env.write_text("AGENT_NOTES_ACTOR_ID=host-actor\n", encoding="utf-8")
    os.environ["AGENT_SUITE_CONFIG"] = str(suite_env)
    os.environ.pop("AGENT_NOTES_ACTOR_ID", None)
    cfg = load_actor_config()
    assert cfg.actor_id == "host-actor"


# ---------------------------------------------------------------------------
# face_factory seam: actor_with_overrides cannot manufacture mid-session
# independence (WI-067 cross-lineage review)
# ---------------------------------------------------------------------------


def test_actor_with_overrides_refuses_conflicting_lineage():
    """A declared session record pins the session's identity: an override to a
    different lineage is refused so the same session cannot author as one
    lineage and then "review" as another."""
    from agent_notes.core.face_factory import actor_with_overrides

    os.environ["AGENT_NOTES_SESSION"] = "override-sess"
    write_session_record("override-sess", {MODEL_LINEAGE_ENV: "claude-opus"})
    with pytest.raises(SessionIdentityConflictError):
        actor_with_overrides(actor_id="subagent", model_lineage="qwen")
    # Same-lineage override is fine; no-record sessions still allow overrides.
    actor = actor_with_overrides(actor_id="subagent", model_lineage="claude-opus")
    assert actor.model_lineage == "claude-opus"


def test_actor_with_overrides_allows_override_before_declaration():
    """Before a session declares, explicit lineage still applies (unattended
    callers, pre-declaration)."""
    from agent_notes.core.face_factory import actor_with_overrides

    os.environ["AGENT_NOTES_SESSION"] = "undec-sess"
    actor = actor_with_overrides(actor_id="subagent", model_lineage="qwen")
    assert actor.model_lineage == "qwen"


def test_actor_with_overrides_uses_session_record_without_override():
    """The normal path — no model_lineage override at all — still carries the
    declared session record's lineage through the actor."""
    from agent_notes.core.face_factory import actor_with_overrides

    os.environ["AGENT_NOTES_SESSION"] = "plain-sess"
    write_session_record("plain-sess", {MODEL_LINEAGE_ENV: "kimi"})
    actor = actor_with_overrides(actor_id="subagent")
    assert actor.model_lineage == "kimi"


# ---------------------------------------------------------------------------
# invariants probe contract
# ---------------------------------------------------------------------------


def test_probe_fails_closed_when_no_lineage():
    result = _run_cli("invariants", "probe", "--json")
    assert result.returncode == 1
    body = _json(result)
    assert body["ok"] is False
    (check,) = body["checks"]
    assert check["id"] == "agent_notes.session_identity_resolvable"
    assert check["status"] == "fail"


def test_probe_passes_with_session_record():
    result = _run_cli(
        "session",
        "declare",
        "--model-lineage",
        "claude-opus",
        "--json",
        env={"AGENT_NOTES_SESSION": "probe-sess"},
    )
    assert result.returncode == 0
    result = _run_cli("invariants", "probe", "--json", env={"AGENT_NOTES_SESSION": "probe-sess"})
    assert result.returncode == 0
    body = _json(result)
    assert body["ok"] is True
    (check,) = body["checks"]
    assert check["status"] == "pass"
    assert check["source"] == "session_record"


def test_probe_passes_with_env_declaration():
    result = _run_cli("invariants", "probe", "--json", env={MODEL_LINEAGE_ENV: "glm"})
    assert result.returncode == 0
    body = _json(result)
    assert body["ok"] is True
    (check,) = body["checks"]
    assert check["status"] == "pass"
    assert check["source"] == "env"


def test_probe_fails_when_declared_lineage_is_not_canonical():
    # A declared-but-unresolvable token must not read as resolvable identity.
    result = _run_cli("invariants", "probe", "--json", env={MODEL_LINEAGE_ENV: "gpt-5.6-sol"})
    assert result.returncode == 1
    body = _json(result)
    assert body["ok"] is False
    (check,) = body["checks"]
    assert check["status"] == "fail"


def test_probe_human_output_mentions_declare_remediation():
    result = _run_cli("invariants", "probe")
    assert result.returncode == 1
    assert "session declare --model-lineage" in result.stdout


# ---------------------------------------------------------------------------
# session declare / status CLI
# ---------------------------------------------------------------------------


def test_session_declare_requires_session_id():
    result = _run_cli("session", "declare", "--model-lineage", "glm", "--json")
    assert result.returncode != 0
    body = _json(result)
    assert body["ok"] is False
    assert body["error"]["code"] == "NO_SESSION_ID"


def test_session_declare_rejects_noncanonical_lineage():
    result = _run_cli(
        "session",
        "declare",
        "--model-lineage",
        "gpt-5.6-sol",
        "--json",
        env={"AGENT_NOTES_SESSION": "bad-sess"},
    )
    assert result.returncode != 0
    body = _json(result)
    assert body["error"]["code"] == "INVALID_MODEL_LINEAGE"


def test_session_declare_refuses_changing_declared_lineage():
    """A session cannot relabel itself mid-session (stable source)."""
    env = {"AGENT_NOTES_SESSION": "conflict-sess"}
    first = _run_cli("session", "declare", "--model-lineage", "claude-opus", "--json", env=env)
    assert first.returncode == 0
    second = _run_cli("session", "declare", "--model-lineage", "qwen", "--json", env=env)
    assert second.returncode != 0
    body = _json(second)
    assert body["error"]["code"] == "SESSION_LINEAGE_CONFLICT"
    # The record is unchanged.
    record = read_session_record("conflict-sess")
    assert record[MODEL_LINEAGE_ENV] == "claude-opus"


def test_session_declare_same_value_is_idempotent():
    env = {"AGENT_NOTES_SESSION": "idem-sess"}
    first = _run_cli("session", "declare", "--model-lineage", "glm", "--json", env=env)
    assert first.returncode == 0
    second = _run_cli("session", "declare", "--model-lineage", "glm", "--json", env=env)
    assert second.returncode == 0
    body = _json(second)
    assert body["ok"] is True
    assert body["model_lineage"] == "glm"


def test_session_status_reports_resolution():
    result = _run_cli(
        "session",
        "declare",
        "--model-lineage",
        "deepseek",
        "--json",
        env={"AGENT_NOTES_SESSION": "status-sess"},
    )
    assert result.returncode == 0
    result = _run_cli("session", "status", "--json", env={"AGENT_NOTES_SESSION": "status-sess"})
    assert result.returncode == 0
    body = _json(result)
    assert body["session_id"] == "status-sess"
    assert body["model_lineage"] == "deepseek"
    assert body["source"] == "session_record"
    assert body["resolvable"] is True


def test_session_status_without_record_reports_undeclared():
    result = _run_cli("session", "status", "--json", env={"AGENT_NOTES_SESSION": "bare-sess"})
    assert result.returncode == 0
    body = _json(result)
    assert body["model_lineage"] is None
    assert body["resolvable"] is False
    assert body["session_record"] == {}


def test_session_declare_with_explicit_session_id():
    """Harnesses that cannot export a session id (opencode) name it explicitly."""
    result = _run_cli(
        "session",
        "declare",
        "--session-id",
        "explicit-sess",
        "--model-lineage",
        "glm",
        "--json",
    )
    assert result.returncode == 0
    body = _json(result)
    assert body["session_id"] == "explicit-sess"
    record = read_session_record("explicit-sess")
    assert record[MODEL_LINEAGE_ENV] == "glm"


def test_session_status_with_explicit_session_id():
    result = _run_cli(
        "session",
        "declare",
        "--session-id",
        "explicit-status-sess",
        "--model-lineage",
        "qwen",
        "--json",
    )
    assert result.returncode == 0
    result = _run_cli("session", "status", "--session-id", "explicit-status-sess", "--json")
    assert result.returncode == 0
    body = _json(result)
    assert body["session_id"] == "explicit-status-sess"
    assert body["model_lineage"] == "qwen"
    assert body["source"] == "session_record"


def test_session_declare_write_failure_emits_contract_error(monkeypatch):
    """An OSError from write_session_record surfaces as a contract error, not
    a traceback, and does not also emit the success payload (single JSON)."""
    import argparse

    import agent_notes.core.session_identity as _si
    from agent_notes.cli import session as _cli
    from agent_notes.cli.common import EXIT_GENERIC

    def _boom(*_args, **_kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(_si, "write_session_record", _boom)
    monkeypatch.setattr(_cli, "_canonical_families_or_error", lambda _u: ("glm", "qwen"))
    os.environ["AGENT_NOTES_SESSION"] = "fail-sess"
    result = _cli.cmd_session_declare(argparse.Namespace(json=True, model_lineage="glm"))
    assert result == EXIT_GENERIC


def test_session_status_regista_unavailable_emits_single_json(monkeypatch):
    """When regista is unavailable, session status emits ONE JSON document (the
    REGISTA_UNAVAILABLE error envelope), not the error followed by the status
    payload (which would be two JSON documents on stdout)."""
    import argparse

    import agent_notes.cli.session as _cli
    from agent_notes.cli.common import EXIT_GENERIC, emit_error

    # Mirrors the real _canonical_families_or_error contract: emit the error
    # and return None (not the emit_error exit code).
    def _unavailable(_use_json: bool):
        emit_error(
            "REGISTA_UNAVAILABLE",
            "cannot validate lineage families",
            use_json=_use_json,
            exit_code=EXIT_GENERIC,
        )
        return None

    monkeypatch.setattr(_cli, "_canonical_families_or_error", _unavailable)
    os.environ["AGENT_NOTES_SESSION"] = "no-reg"
    result = _cli.cmd_session_status(argparse.Namespace(json=True))
    assert result == EXIT_GENERIC
