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


@pytest.fixture(autouse=True, scope="session")
def _hermetic_config(tmp_path_factory):
    """Hermetic config isolation for the test session.

    Plan 012 WI-1 added a config-file fallback to ``RegistaConfig``. Without
    isolation, the operator's host ``~/.config/agent-notes/config.json`` (which
    enables regista against the prod DB) would be read by every test that hits
    the write path, routing test writes to production. This pins
    ``AGENT_NOTES_CONFIG`` to an empty ``{}`` session file and clears any
    host-provided regista env, so regista stays disabled unless a test opts in
    (via ``monkeypatch`` env, or ``set_face_for_test`` which bypasses config).
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
        # Legacy aliases (one-release back-compat, Plan 017 WI-1.1) ...
        "AGENT_NOTES_REGISTA_DSN",
        "AGENT_NOTES_REGISTA_HMAC_KEY_PATH",
        "AGENT_NOTES_REGISTA_PROJECT",
        "AGENT_NOTES_REGISTA_REQUIRE_SSL",
        "AGENT_NOTES_REGISTA_WRITES",
        # ... and the canonical suite env vars the resolver prefers. Without
        # clearing these, a host with REGISTA_DSN set enables regista for every
        # test (the alias clearing above is a no-op when the canonical var is
        # present), routing test writes to the production spine.
        "REGISTA_DSN",
        "REGISTA_KEY_PATH",
        "REGISTA_REQUIRE_SSL",
        # Principal_id (Plan 017 WI-4.2) — clear so tests control their own.
        "AGENT_NOTES_PRINCIPAL_ID",
        "REGISTA_PRINCIPAL_ID",
    )
    saved = {k: os.environ.get(k) for k in keys}
    os.environ["AGENT_NOTES_CONFIG"] = str(cfg)
    os.environ["AGENT_SUITE_CONFIG"] = str(suite_cfg)
    os.environ["AGENT_SUITE_SYSTEM_CONFIG"] = str(suite_cfg)
    for k in keys[3:]:
        os.environ.pop(k, None)
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


@pytest.fixture(scope="session")
def ephemeral_db():
    """Session-scoped ephemeral Postgres with core schema applied.

    Yields the DSN string. Also sets `AGENT_NOTES_DSN` and resets the
    module-level pool singleton so all `db.*` helpers use the test DB.

    Skips cleanly when Docker is unavailable (e.g. windows-latest CI) so
    Postgres-dependent tests skip rather than error (Plan 003 WI-5.1).
    """
    container = PostgresContainer("pgvector/pgvector:pg17")
    try:
        docker.from_env().ping()
    except (docker.errors.DockerException, OSError) as exc:
        pytest.skip(f"Docker unavailable: {exc}")

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
        dsn = container.get_connection_url().replace(
            "postgresql+psycopg2://", "postgresql://"
        )
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
