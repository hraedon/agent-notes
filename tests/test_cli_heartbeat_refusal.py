"""Heartbeat refusals cross the real CLI/model/regista boundary (WI-081).

Both stores are disposable PostgreSQL instances: the shared pgvector fixture
for the agent-notes projection and PostgreSQL 15 for the locked regista spine.
No heartbeat call, claim decision, projection query, history query or error is
mocked in the boundary tests. Small handler tests cover the error-selection
controls that cannot be induced without corrupting a real store.
"""

from __future__ import annotations

import argparse
import json
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row
from regista import Regista, canonical_workflow_yaml
from regista._errors import ErrorCode, RegistaError
from regista.testing import make_v6_keyset, open_v6_epoch
from testcontainers.postgres import PostgresContainer

import agent_notes.cli as cli
import agent_notes.cli.work_items as work_items
from agent_notes.cli.common import (
    EXIT_CONFLICT,
    EXIT_NOT_CONFIGURED,
    EXIT_SUCCESS,
)
from agent_notes.core import db as coredb
from agent_notes.core.face_factory import reset_face, set_face_for_test
from agent_notes.core.regista_face import RegistaFace
from agent_notes.core.work_item_model import WorkItemModel
from tests import conftest
from tests.cli_harness import run_cli

pytestmark = pytest.mark.postgres
# Register this module's fixture so its DSN matches the active global pool
# after another DB-test module has installed its own session-scoped pool.
ephemeral_db = conftest.ephemeral_db


@pytest.fixture(scope="session")
def heartbeat_regista_dsn(ephemeral_db):
    # regista's migration set targets PG15. Keep it separate from the shared
    # PG17 projection, as in the existing outbox qualification.
    with PostgresContainer("postgres:15") as container:
        yield container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")


@pytest.fixture
def heartbeat_estate(ephemeral_db, heartbeat_regista_dsn, tmp_path):
    project_name = "heartbeat_" + uuid.uuid4().hex[:10]
    workspace = coredb.get_or_create_workspace("default", "Default workspace")
    repo = tmp_path / "repo"
    repo.mkdir()
    project = coredb.get_or_create_project(
        workspace.id,
        project_name,
        "Synthetic heartbeat",
        repo_root=str(repo),
    )
    principals = ("agent:worker", "agent:reviewer")
    keyset = make_v6_keyset(tmp_path, principals=principals)
    regista = Regista.create_project(heartbeat_regista_dsn, project_name, str(keyset.path))
    open_v6_epoch(regista, keyset, principals=principals)
    regista.register_workflow(canonical_workflow_yaml())
    face = RegistaFace(regista)
    reset_face()
    set_face_for_test(face)
    try:
        item = WorkItemModel.file_work_item(
            project_id=project.id,
            identifier="WI-HEARTBEAT",
            title="Synthetic heartbeat refusal",
            embedding=[0.0] * 768,
        )
        environment = {
            "AGENT_NOTES_DSN": ephemeral_db,
            "AGENT_NOTES_REGISTA_WRITES": "1",
            "REGISTA_DSN": heartbeat_regista_dsn,
            "REGISTA_KEY_PATH": str(keyset.path),
            "AGENT_NOTES_ACTOR_ID": "agent:worker",
        }
        yield project, repo, face, item, environment
    finally:
        reset_face()
        regista.close()


def _projection_state(entity_id: str, identifier: str) -> tuple[dict, dict | None, int]:
    with coredb._conn() as connection:
        item = connection.execute(
            "SELECT * FROM work_items WHERE entity_id = %s",
            (entity_id,),
        ).fetchone()
        lease = connection.execute(
            "SELECT * FROM work_item_leases WHERE entity_id = %s",
            (entity_id,),
        ).fetchone()
        changes = connection.execute(
            "SELECT count(*) AS count FROM change_log WHERE kind = 'work_item' AND identifier = %s",
            (identifier,),
        ).fetchone()["count"]
    return dict(item), dict(lease) if lease is not None else None, changes


def _kernel_claim(environment: dict[str, str], project_name: str, work_item_id: str):
    with psycopg.connect(environment["REGISTA_DSN"], row_factory=dict_row) as connection:
        row = connection.execute(
            sql.SQL("SELECT * FROM {}.claims WHERE work_item_id = %s").format(
                sql.Identifier(project_name)
            ),
            (work_item_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def _kernel_claim_expiry(environment: dict[str, str], project_name: str, work_item_id: str):
    with psycopg.connect(environment["REGISTA_DSN"], row_factory=dict_row) as connection:
        row = connection.execute(
            sql.SQL(
                "SELECT claim_expires_at FROM {}.work_items_current WHERE work_item_id = %s"
            ).format(sql.Identifier(project_name)),
            (work_item_id,),
        ).fetchone()
    return row["claim_expires_at"]


@pytest.mark.parametrize("use_json", [True, False])
@pytest.mark.parametrize("claimed", [False, True])
def test_heartbeat_refusal_is_an_envelope_and_preserves_all_state(
    heartbeat_estate, use_json: bool, claimed: bool
):
    project, repo, face, item, environment = heartbeat_estate
    expected = "CLAIM_NOT_FOUND"
    message_fragment = "No claim found"
    if claimed:
        WorkItemModel.claim_work_item(project.id, item["identifier"], ttl_seconds=900)
        environment["AGENT_NOTES_ACTOR_ID"] = "agent:reviewer"
        expected = "CLAIM_LOST"
        message_fragment = "is now held by agent:worker"

    before_projection = _projection_state(item["entity_id"], item["identifier"])
    before_claim = _kernel_claim(environment, project.slug, item["regista_work_item_id"])
    before_expiry = _kernel_claim_expiry(
        environment,
        project.slug,
        item["regista_work_item_id"],
    )
    before_history = face.history(item["regista_work_item_id"])

    arguments = [
        "work-item",
        "heartbeat",
        item["identifier"],
        "--ttl",
        "1200",
        "--path",
        str(repo),
    ]
    if use_json:
        arguments.append("--json")
    result = run_cli(*arguments, env=environment, check=False)

    assert result.returncode == EXIT_CONFLICT, result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    if use_json:
        envelope = json.loads(result.stdout)
        assert envelope == {
            "ok": False,
            "error": {
                "code": expected,
                "message": envelope["error"]["message"],
                "detail": None,
                "retryable": False,
                "partial": None,
            },
        }
        assert message_fragment in envelope["error"]["message"]
    else:
        assert result.stdout == ""
        assert "Error: " in result.stderr
        assert message_fragment in result.stderr

    assert _projection_state(item["entity_id"], item["identifier"]) == before_projection
    assert _kernel_claim(environment, project.slug, item["regista_work_item_id"]) == before_claim
    assert (
        _kernel_claim_expiry(environment, project.slug, item["regista_work_item_id"])
        == before_expiry
    )
    assert face.history(item["regista_work_item_id"]) == before_history


def test_successful_heartbeat_really_renews_both_stores(heartbeat_estate):
    project, repo, face, item, environment = heartbeat_estate
    WorkItemModel.claim_work_item(project.id, item["identifier"], ttl_seconds=120)
    before_item, before_lease, before_changes = _projection_state(
        item["entity_id"], item["identifier"]
    )
    before_claim = _kernel_claim(environment, project.slug, item["regista_work_item_id"])
    before_history = face.history(item["regista_work_item_id"])
    assert before_lease is not None
    assert before_claim is not None

    result = run_cli(
        "work-item",
        "heartbeat",
        item["identifier"],
        "--ttl",
        "1200",
        "--path",
        str(repo),
        "--json",
        env=environment,
        check=False,
    )

    assert result.returncode == EXIT_SUCCESS, result.stderr
    assert "Traceback" not in result.stderr
    response = json.loads(result.stdout)
    after_item, after_lease, after_changes = _projection_state(
        item["entity_id"], item["identifier"]
    )
    after_claim = _kernel_claim(environment, project.slug, item["regista_work_item_id"])
    after_history = face.history(item["regista_work_item_id"])
    assert after_lease is not None
    assert after_claim is not None
    assert response["lease"]["entity_id"] == item["entity_id"]
    assert response["lease"]["heartbeat_count"] == before_lease["heartbeat_count"] + 1
    assert after_lease["heartbeat_count"] == before_lease["heartbeat_count"] + 1
    assert after_lease["expires_at"] > before_lease["expires_at"]
    assert after_claim["expires_at"] > before_claim["expires_at"]
    assert after_lease["expires_at"] == after_claim["expires_at"]
    assert (
        _kernel_claim_expiry(environment, project.slug, item["regista_work_item_id"])
        == after_claim["expires_at"]
    )
    assert after_item == before_item
    assert after_changes == before_changes
    assert len(after_history) == len(before_history) + 1
    assert after_history[-1].transition == "claim_heartbeat"


def _handler_args(*, use_json: bool = True, path: str | None = "/synthetic"):
    return argparse.Namespace(
        workspace=None,
        project=None,
        path=path,
        identifier="WI-SYNTHETIC",
        ttl=300,
        json=use_json,
    )


@pytest.mark.parametrize("use_json", [True, False])
@pytest.mark.parametrize("code", [ErrorCode.CLAIM_NOT_FOUND, ErrorCode.CLAIM_LOST])
def test_expected_regista_refusal_preserves_structured_detail(
    monkeypatch, capsys, use_json: bool, code: ErrorCode
):
    detail = {"identifier": "WI-SYNTHETIC", "attempt": 7}
    error = RegistaError(code, "synthetic expected refusal", detail=detail)
    monkeypatch.setattr(work_items, "_resolve", lambda *_: (1, 2, "workspace", "project"))

    def refuse(**_kwargs):
        raise error

    monkeypatch.setattr(WorkItemModel, "heartbeat_work_item", staticmethod(refuse))
    assert work_items.cmd_wi_heartbeat(_handler_args(use_json=use_json)) == EXIT_CONFLICT
    captured = capsys.readouterr()
    if use_json:
        assert captured.err == ""
        envelope = json.loads(captured.out)
        assert envelope["error"]["code"] == code.value
        assert envelope["error"]["detail"] == json.dumps(detail, sort_keys=True)
    else:
        assert captured.out == ""
        assert captured.err == (
            "Error: synthetic expected refusal\n  attempt: 7\n  identifier: WI-SYNTHETIC\n"
        )


@pytest.mark.parametrize(
    "error",
    [
        RegistaError(
            ErrorCode.VALIDATOR_FAILED,
            "unexpected regista sentinel",
            detail={"sentinel": True},
        ),
        RuntimeError("unexpected runtime sentinel"),
    ],
)
def test_unrelated_errors_still_propagate(monkeypatch, capsys, error: Exception):
    monkeypatch.setattr(work_items, "_resolve", lambda *_: (1, 2, "workspace", "project"))

    def fail(**_kwargs):
        raise error

    monkeypatch.setattr(WorkItemModel, "heartbeat_work_item", staticmethod(fail))
    with pytest.raises(type(error)) as raised:
        work_items.cmd_wi_heartbeat(_handler_args())
    assert raised.value is error
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_value_error_remains_validation_envelope(monkeypatch, capsys):
    monkeypatch.setattr(work_items, "_resolve", lambda *_: (1, 2, "workspace", "project"))

    def reject(**_kwargs):
        raise ValueError("ttl must be positive")

    monkeypatch.setattr(WorkItemModel, "heartbeat_work_item", staticmethod(reject))
    assert work_items.cmd_wi_heartbeat(_handler_args()) == EXIT_CONFLICT
    captured = capsys.readouterr()
    assert captured.err == ""
    envelope = json.loads(captured.out)
    assert envelope["error"] == {
        "code": "VALIDATION_FAILED",
        "message": "ttl must be positive",
        "detail": None,
        "retryable": False,
        "partial": None,
    }


def test_resolution_failure_still_short_circuits_before_model(monkeypatch, capsys):
    def unresolved(*_args):
        raise SystemExit(EXIT_NOT_CONFIGURED)

    def must_not_run(**_kwargs):  # pragma: no cover - assertion sentinel
        raise AssertionError("heartbeat model must not run after resolution failure")

    monkeypatch.setattr(work_items, "_resolve", unresolved)
    monkeypatch.setattr(WorkItemModel, "heartbeat_work_item", staticmethod(must_not_run))
    assert work_items.cmd_wi_heartbeat(_handler_args(path=None)) == EXIT_NOT_CONFIGURED
    captured = capsys.readouterr()
    assert captured.err == ""
    envelope = json.loads(captured.out)
    assert envelope["error"]["code"] == "PROJECT_NOT_RESOLVED"


def test_global_db_not_found_adapter_is_unchanged(monkeypatch, capsys):
    error = RegistaError(ErrorCode.DB_NOT_FOUND, "synthetic missing schema")

    def missing(_args):
        raise error

    from agent_notes.core import face_factory

    monkeypatch.setattr(face_factory, "current_project", lambda: "synthetic_project")
    assert cli._dispatch(missing, _handler_args()) == EXIT_NOT_CONFIGURED
    captured = capsys.readouterr()
    assert captured.err == ""
    envelope = json.loads(captured.out)
    assert envelope["error"]["code"] == "DB_NOT_FOUND"
    assert "synthetic_project" in envelope["error"]["message"]
