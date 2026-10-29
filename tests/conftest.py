"""Shared test fixtures for agent-notes integration tests.

Convention (from AGENTS.md / Phase 1b.6):
- Triggers, recursive CTEs, and change_log semantics MUST run against real
  Postgres. No DB mocks for those.
- Use the `ephemeral_db` session-scoped fixture from this module to get a
  fresh DB with the core schema applied.
- Import the fixture explicitly in each test module:
      from tests.conftest import ephemeral_db  # noqa: F401
  or rely on pytest's auto-discovery (fixtures are available project-wide).
"""

from __future__ import annotations

import os
from pathlib import Path

import docker.errors
import psycopg
import pytest
from testcontainers.postgres import PostgresContainer

from agent_notes.core import db as coredb

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schema"

# The transitional producer variables the native review gate still reads
# (``REGISTA_PRODUCER_MODEL``, Plan 026 D2). A test that sets one directly in
# ``os.environ`` must not leak it into every later test, so they are restored
# per test.
_PRODUCER_ENV_KEYS = (
    "REGISTA_PRODUCER_HARNESS",
    "REGISTA_PRODUCER_HARNESS_VERSION",
    "REGISTA_PRODUCER_MODEL",
    "REGISTA_PRODUCER_MODEL_LINEAGE",
)


def shape_valid_delegation(*, subject_principal_id: str) -> dict:
    """A structurally parseable action-delegation document.

    The signature is inert zeros — ``parse_action_delegation`` checks shape,
    not cryptography; the chain is verified by regista at write time. This is
    for refusal-path tests only (checks that run before any verification,
    like the terminal-subject match or the reconciliation credential-hash
    comparison), never for a write that must be accepted as delegated.
    """

    import base64
    import uuid as _uuid
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
    return {
        "type": "regista.action-delegation",
        "version": 1,
        "credential_id": str(_uuid.uuid4()),
        "trust_domain_id": str(_uuid.uuid4()),
        "issuer_principal_id": "human:operator",
        "subject_principal_id": subject_principal_id,
        "issuer_key_id": "pk_test",
        "issuer_key_binding_event_hash": "sha256:" + "ab" * 32,
        "parent_credential_hash": None,
        "scope": {
            "project_instance_ids": [str(_uuid.uuid4())],
            "entity_kinds": ["work_item"],
            "workflow_names": ["canonical"],
            "transitions": ["amend"],
        },
        "not_before": (now - timedelta(hours=1)).strftime(fmt),
        "not_after": (now + timedelta(hours=1)).strftime(fmt),
        "max_uses": None,
        "delegation_allowed": False,
        "signature": {
            "scheme_id": "ed25519",
            "value": base64.b64encode(b"\x00" * 64).decode("ascii"),
        },
    }


@pytest.fixture(autouse=True, scope="session")
def _hermetic_config(tmp_path_factory):
    """Hermetic config isolation for the test session.

    Without isolation, the operator's host ``~/.config/agent-notes/config.json``
    (its native DSN, or a leftover ``regista.writes_enabled``) would be read by
    every test that hits the write path. This pins ``AGENT_NOTES_CONFIG`` to an
    empty ``{}`` session file and clears any host-provided regista env, so a
    test only sees the retired regista settings when it sets them itself.
    """
    cfg = tmp_path_factory.mktemp("cfg") / "empty.json"
    cfg.write_text("{}")
    # Isolate from host suite.env files (Plan 017 WI-4.2): point both the
    # per-user and system suite.env paths at non-existent files so a host
    # /etc/agent-suite/suite.env or ~/.config/agent-suite/suite.env does not
    # leak into tests (routing test writes to production or attributing them
    # to the operator's principal_id).
    suite_cfg = tmp_path_factory.mktemp("suite") / "suite.env"
    keys = (
        "AGENT_NOTES_CONFIG",
        "AGENT_SUITE_CONFIG",
        "AGENT_SUITE_SYSTEM_CONFIG",
        "AGENT_NOTES_OUTBOX_DIR",
        # Clear the retired regista vars so a host environment cannot make
        # every write refuse.
        "AGENT_NOTES_REGISTA_WRITES",
        "REGISTA_DSN",
        "REGISTA_KEY_PATH",
        "REGISTA_REQUIRE_SSL",
        "REGISTA_PRODUCER_HARNESS",
        "REGISTA_PRODUCER_HARNESS_VERSION",
        "REGISTA_PRODUCER_MODEL",
        "REGISTA_PRODUCER_MODEL_LINEAGE",
        # Canonical actor identity (v6) — clear so tests control their own.
        "AGENT_NOTES_ACTOR_ID",
        "REGISTA_PRINCIPAL_ID",
        # Project slug (WI-029 sweep) — the per-user suite.env overlay exports
        # AGENT_NOTES_PROJECT on bootstrapped hosts; keep it out of tests.
        "AGENT_NOTES_PROJECT",
    )
    saved = {k: os.environ.get(k) for k in keys}
    saved["AGENT_NOTES_ACTOR_ID"] = os.environ.get("AGENT_NOTES_ACTOR_ID")
    os.environ["AGENT_NOTES_CONFIG"] = str(cfg)
    os.environ["AGENT_SUITE_CONFIG"] = str(suite_cfg)
    os.environ["AGENT_SUITE_SYSTEM_CONFIG"] = str(suite_cfg)
    # The retired regista outbox dir doctor inspects (Plan 026 D5): point it at
    # a path that does not exist so a host's real queue never reaches a test.
    os.environ["AGENT_NOTES_OUTBOX_DIR"] = str(suite_cfg.parent / "no-legacy-outbox")
    for k in keys[4:]:
        os.environ.pop(k, None)
    # All ordinary write-path tests run as one canonical fixture actor. Tests
    # that exercise the refusal path delete this value explicitly.
    os.environ["AGENT_NOTES_ACTOR_ID"] = "agent:worker"
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _apply_schema(dsn: str) -> None:
    sql_files = sorted(SCHEMA_DIR.glob("*.sql"))
    for f in sql_files:
        sql = f.read_text(encoding="utf-8")
        with psycopg.connect(dsn) as conn:
            with conn.transaction():
                conn.execute(sql)


@pytest.fixture(autouse=True)
def _hermetic_producer_env():
    """Snapshot/restore the process producer identity around every test."""

    saved = {key: os.environ.get(key) for key in _PRODUCER_ENV_KEYS}
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(scope="session")
def ephemeral_db():
    """Session-scoped ephemeral Postgres with core schema applied.

    Yields the DSN string. Also sets `AGENT_NOTES_DSN` and resets the
    module-level pool singleton so all `db.*` helpers use the test DB.

    Skips cleanly when Docker is unavailable (e.g. windows-latest CI) so
    Postgres-dependent tests skip rather than error (Plan 003 WI-5.1).
    """
    try:
        docker.from_env().ping()
    except (docker.errors.DockerException, OSError) as exc:
        pytest.skip(f"Docker unavailable: {exc}")

    container = PostgresContainer("pgvector/pgvector:pg17")

    try:
        container.start()
    except Exception as exc:
        # testcontainers (the ryuk sidecar + the docker.sock volume mount)
        # cannot start on hosts whose Docker is present but Linux-container-
        # hostile — notably GitHub windows-latest runners reject the
        # ``/var/run/docker.sock`` volume spec. Skip cleanly rather than
        # erroring the whole suite (matches the documented Plan 003 intent).
        pytest.skip(f"Postgres test container could not start: {exc}")
    try:
        dsn = container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        _apply_schema(dsn)
        old = os.environ.get("AGENT_NOTES_DSN")
        os.environ["AGENT_NOTES_DSN"] = dsn
        coredb._pool = None
        yield dsn
        if old is None:
            del os.environ["AGENT_NOTES_DSN"]
        else:
            os.environ["AGENT_NOTES_DSN"] = old
        if coredb._pool is not None:
            coredb._pool.close()
            coredb._pool = None
    finally:
        container.stop()
