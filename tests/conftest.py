"""Shared test fixtures for agent-notes-mcp integration tests.

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

import psycopg
import pytest
from testcontainers.postgres import PostgresContainer

from agent_notes.core import db as coredb

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schema"


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
    """
    with PostgresContainer("pgvector/pgvector:pg17") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
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
