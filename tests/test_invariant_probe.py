"""Tests for the read-only canonical v6 identity probe."""

from __future__ import annotations

import json

import pytest

from agent_notes.core import invariant_probe as probe


@pytest.fixture(autouse=True)
def _clean_identity_env(monkeypatch, tmp_path):
    for name in (
        "AGENT_NOTES_ACTOR_ID",
        "REGISTA_PRINCIPAL_ID",
        "AGENT_NOTES_ACTION_DELEGATION_PATHS",
    ):
        monkeypatch.delenv(name, raising=False)
    suite_env = tmp_path / "suite.env"
    monkeypatch.setenv("AGENT_SUITE_CONFIG", str(suite_env))
    monkeypatch.setenv("AGENT_SUITE_SYSTEM_CONFIG", str(suite_env))


def test_probe_reports_resolved_canonical_actor(monkeypatch):
    monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:worker")

    report = probe.invariant_probe_report()

    assert report["ok"] is True
    check = report["checks"][0]
    assert check["id"] == probe.SESSION_IDENTITY_CHECK
    assert check["status"] == "pass"
    assert check["evidence"]["actor_id"] == "agent:worker"


def test_probe_fails_when_actor_is_not_configured():
    report = probe.invariant_probe_report()

    assert report["ok"] is False
    check = report["checks"][0]
    assert check["reason"] == "identity_not_configured"
    assert check["evidence"]["error_type"] == "ActorConfigurationError"


def test_probe_fails_for_noncanonical_actor(monkeypatch):
    monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "worker")

    report = probe.invariant_probe_report()

    assert report["ok"] is False
    assert report["checks"][0]["reason"] == "identity_invalid"


def test_probe_is_contract_shaped_after_an_unexpected_error(monkeypatch, capsys):
    def _boom():
        raise RuntimeError("secret-bearing: postgresql://user:pass@host/db")

    monkeypatch.setattr(probe, "build_report", _boom)
    report = probe.invariant_probe_report()

    assert report["component"] == "agent-notes"
    assert report["ok"] is False
    assert report["checks"][0]["reason"] == "probe_error"
    assert "postgresql://" not in json.dumps(report)
    assert "RuntimeError" in capsys.readouterr().err


def test_probe_opens_no_database_connection_and_builds_no_face(monkeypatch):
    """Structural proof of the read-only claim.

    Every door to the store — the regista face, the projection DB pool, the
    ``psycopg_pool.ConnectionPool`` constructor the pool is built from (it
    connects eagerly, ``open=True``), and ``socket.create_connection`` — is
    booby-trapped. A probe that touched any of them fails here rather than in
    production, where "read-only" is the only reason it is safe to run this
    from a scheduled timer against a live store.

    Two properties make this a real trap rather than a shape:

    - It calls :func:`build_report`, **not** ``invariant_probe_report()``. The
      public entry point is wrapped in a deliberately broad ``except Exception``
      (a crashed probe must still emit a well-formed report), which would
      swallow a tripped trap's ``AssertionError`` and surface the violation
      only as an unnamed ``ok`` flip. Against ``build_report`` the trap's own
      message — naming the door — propagates.
    - Each door also appends to ``touched``, which is asserted separately. That
      covers doors reached from inside ``_identity``, whose own broad
      ``except Exception`` turns a trap trip into an ``identity_invalid``
      verdict rather than letting the ``AssertionError`` escape.

    This is a *structural* trap: it forbids the doors it names and nothing
    else. Any new way this package reaches the network or the store must be
    added to the list below, or the read-only claim silently stops being
    proven. The subprocess test ``test_probe_does_not_reach_a_configured_store``
    in ``tests/test_invariant_probe_cli.py`` is a smoke check over the same
    claim, not a substitute for this one.
    """
    monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:worker")

    import socket

    import psycopg_pool

    from agent_notes.core import db as coredb
    from agent_notes.core import face_factory

    touched: list[str] = []

    def _forbid(door: str):
        def _forbidden(*_a, **_kw):
            touched.append(door)
            raise AssertionError(f"the invariant probe must not touch the store: {door}")

        return _forbidden

    # `raising=False` is deliberately absent everywhere: every symbol below
    # exists today, and a rename must fail this test loudly rather than quietly
    # disarm the trap by patching an attribute nothing calls.
    monkeypatch.setattr(face_factory, "get_face", _forbid("face_factory.get_face"))
    monkeypatch.setattr(face_factory, "_build_face", _forbid("face_factory._build_face"))
    monkeypatch.setattr(coredb, "_conn", _forbid("db._conn"))
    monkeypatch.setattr(coredb, "_get_pool", _forbid("db._get_pool"))
    # Both bindings: `db.py` from-imports the class, so patching the
    # `psycopg_pool` attribute alone would leave `db.ConnectionPool` live.
    monkeypatch.setattr(coredb, "ConnectionPool", _forbid("db.ConnectionPool"))
    monkeypatch.setattr(psycopg_pool, "ConnectionPool", _forbid("psycopg_pool.ConnectionPool"))
    monkeypatch.setattr(socket, "create_connection", _forbid("socket.create_connection"))

    report = probe.build_report()

    assert touched == [], f"the probe reached the store through: {touched}"
    required = next(c for c in report["checks"] if c["id"] == probe.SESSION_IDENTITY_CHECK)
    assert required["reason"] == "resolved", required
    assert report["ok"] is True

    # The public entry point is read-only too — asserted after `build_report`
    # so a trap trip is reported by the un-guarded call above, with its own
    # message, rather than as `probe_error` here.
    guarded = probe.invariant_probe_report()
    assert touched == [], f"the probe reached the store through: {touched}"
    guarded_required = next(c for c in guarded["checks"] if c["id"] == probe.SESSION_IDENTITY_CHECK)
    assert guarded_required["reason"] == "resolved", guarded_required
