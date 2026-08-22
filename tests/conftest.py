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
from regista import canonical_workflow_yaml
from regista.testing import InMemoryRegista, make_v6_keyset, open_v6_epoch
from testcontainers.postgres import PostgresContainer

from agent_notes.core import db as coredb

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schema"

# The producer identity regista resolves from the process environment. Tests
# (and regista's own test helpers, e.g. ``set_v6_producer_env`` inside
# ``open_v6_epoch``) mutate these variables directly in ``os.environ`` —
# deliberately outside ``monkeypatch`` — so without a per-test restore a
# producer configured in one test silently signs every later test's events.
_PRODUCER_ENV_KEYS = (
    "REGISTA_PRODUCER_HARNESS",
    "REGISTA_PRODUCER_HARNESS_VERSION",
    "REGISTA_PRODUCER_MODEL",
    "REGISTA_PRODUCER_MODEL_LINEAGE",
)

# Keep the test-only v6 identity vocabulary explicit.  Production code must not
# open an epoch or register a workflow as a side effect of constructing a face;
# tests that need an ordinary v6 writer provision the throwaway project through
# this helper instead.
V6_TEST_PRINCIPALS: tuple[str, ...] = (
    "agent:worker",
    "agent:test-agent",
    "agent:author-agent",
    "agent:reviewer",
    "agent:accepter",
    "agent:env-linker",
    "agent:env-remover",
    "agent:env-lease-agent",
    "agent:p3-test-agent",
    "agent:note-ac-agent",
    "agent:ac-test-agent",
    "agent:a",
    "agent:b",
    "agent:deleter-agent",
    "human:operator",
    "human:reviewer",
    "service:hooks",
    "service:agent-notes-migration",
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


def provision_v6_regista(key_path: str | Path, *, project: str = "test_project") -> InMemoryRegista:
    """Return a throwaway, fully admitted v6 in-memory regista instance.

    The helper intentionally lives in the consumer test suite.  It keeps the
    production ``RegistaFace`` honest: workflow registration and genesis are
    external provisioning operations, never an implicit write-path side effect.
    """

    path = Path(key_path)
    keyset = make_v6_keyset(
        path.parent,
        principals=V6_TEST_PRINCIPALS,
        filename=path.name,
    )
    instance = InMemoryRegista(project=project, hmac_key_path=keyset.path)
    open_v6_epoch(instance, keyset, principals=V6_TEST_PRINCIPALS)
    instance.register_workflow(canonical_workflow_yaml())
    return instance


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
        # Clear the canonical suite vars and the tool-specific write gate so a
        # host environment cannot enable regista for every test.
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
        # AGENT_NOTES_PROJECT on bootstrapped hosts; inherited into the test
        # process it overrides every RegistaConfig().project and breaks
        # projection-sync assertions that pin their own project.
        "AGENT_NOTES_PROJECT",
    )
    saved = {k: os.environ.get(k) for k in keys}
    saved["AGENT_NOTES_ACTOR_ID"] = os.environ.get("AGENT_NOTES_ACTOR_ID")
    os.environ["AGENT_NOTES_CONFIG"] = str(cfg)
    os.environ["AGENT_SUITE_CONFIG"] = str(suite_cfg)
    os.environ["AGENT_SUITE_SYSTEM_CONFIG"] = str(suite_cfg)
    for k in keys[3:]:
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
    """Snapshot/restore the process producer identity around every test.

    ``provision_v6_regista`` → ``open_v6_epoch`` → ``set_v6_producer_env``
    writes ``REGISTA_PRODUCER_*`` straight into ``os.environ`` (only filling
    absent variables), so the first provisioning test would otherwise pin the
    producer for the whole session and make later tests depend on execution
    order. Restoring per test keeps each test's producer claims its own.
    """

    saved = {key: os.environ.get(key) for key in _PRODUCER_ENV_KEYS}
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
def v6_key_path(tmp_path: Path) -> str:
    """Path for a throwaway v6 keyset (no file written here).

    The HMAC-era placeholder manifest is gone: ``provision_v6_regista`` (or
    ``make_v6_keyset``) writes the real keyset at this path, so pre-writing a
    stand-in file would only be overwritten — and would mislabel a v6 keyset
    as an HMAC key manifest.
    """

    return str(tmp_path / "v6_keys.json")


@pytest.fixture
def producer_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Set a truthful producer (model and lineage as a consistent pair).

    Every fixture that moves the producer lineage must move the paired model
    with it: the producer block signs both, and a ``REGISTA_PRODUCER_MODEL``
    from one family next to a ``REGISTA_PRODUCER_MODEL_LINEAGE`` from another
    is a false identity claim the test would then commit to the event log.
    Tests override via ``_set_truthful_producer`` on the returned patcher.
    """

    _set_truthful_producer(monkeypatch, model=TEST_PRODUCER_MODEL, lineage=TEST_PRODUCER_LINEAGE)
    return monkeypatch


# The default fixture producer: one model, one lineage, mutually consistent
# (mirrors regista's own test identity: harness claude-code / model
# claude-fable-5 / lineage fable).
TEST_PRODUCER_MODEL = "claude-fable-5"
TEST_PRODUCER_LINEAGE = "fable"

# Truthful (model, lineage) pairs a test may switch the producer between.
TRUTHFUL_PRODUCER_PAIRS: dict[str, str] = {
    "claude-fable-5": "fable",
    "kimi-k2.5": "kimi",
    "glm-5.3": "glm",
    "claude-opus-4.6": "claude-opus",
    "longcat-flash-1": "longcat",
}


def _set_truthful_producer(
    monkeypatch: pytest.MonkeyPatch, *, model: str, lineage: str | None = None
) -> None:
    """Point the process producer at a consistent (model, lineage) pair.

    ``lineage=None`` looks the pair up from the model so callers cannot drift
    the two apart. Harness identity always stays set (the v6 writer refuses
    without it).
    """

    if lineage is None:
        lineage = TRUTHFUL_PRODUCER_PAIRS.get(model)
        if lineage is None:
            raise ValueError(f"no truthful lineage pairing declared for model {model!r}")
    if TRUTHFUL_PRODUCER_PAIRS.get(model) != lineage:
        raise ValueError(f"untruthful producer pair: model {model!r} is not a {lineage!r} model")
    monkeypatch.setenv("REGISTA_PRODUCER_HARNESS", "claude-code")
    monkeypatch.setenv("REGISTA_PRODUCER_HARNESS_VERSION", "test-harness/1")
    monkeypatch.setenv("REGISTA_PRODUCER_MODEL", model)
    monkeypatch.setenv("REGISTA_PRODUCER_MODEL_LINEAGE", lineage)


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
