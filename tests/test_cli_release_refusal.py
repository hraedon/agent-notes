"""Release refusals cross the real CLI/model/regista boundary (WI-078).

Both stores are disposable PostgreSQL instances: the shared pgvector fixture
for the agent-notes projection and PostgreSQL 15 for the locked regista spine.
No release call, claim decision, projection query or error is mocked.
"""

from __future__ import annotations

import json
import uuid

import psycopg
import pytest
from psycopg import sql
from regista import Regista, canonical_workflow_yaml
from regista.testing import make_v6_keyset, open_v6_epoch
from testcontainers.postgres import PostgresContainer

from agent_notes.cli.common import EXIT_CONFLICT, EXIT_SUCCESS
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
def release_regista_dsn(ephemeral_db):
    # regista's migration set targets PG15. Keep it separate from the shared
    # PG17 projection, as in the existing outbox qualification.
    with PostgresContainer("postgres:15") as container:
        yield container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")


@pytest.fixture
def release_estate(ephemeral_db, release_regista_dsn, tmp_path):
    project_name = "release_" + uuid.uuid4().hex[:10]
    workspace = coredb.get_or_create_workspace("default", "Default workspace")
    repo = tmp_path / "repo"
    repo.mkdir()
    project = coredb.get_or_create_project(
        workspace.id, project_name, "Synthetic release", repo_root=str(repo)
    )
    principals = ("agent:worker", "agent:reviewer")
    keyset = make_v6_keyset(tmp_path, principals=principals)
    regista = Regista.create_project(release_regista_dsn, project_name, str(keyset.path))
    open_v6_epoch(regista, keyset, principals=principals)
    regista.register_workflow(canonical_workflow_yaml())
    face = RegistaFace(regista)
    reset_face()
    set_face_for_test(face)
    try:
        item = WorkItemModel.file_work_item(
            project_id=project.id,
            identifier="WI-RELEASE",
            title="Synthetic release refusal",
            embedding=[0.0] * 768,
        )
        environment = {
            "AGENT_NOTES_DSN": ephemeral_db,
            "AGENT_NOTES_REGISTA_WRITES": "1",
            "REGISTA_DSN": release_regista_dsn,
            "REGISTA_KEY_PATH": str(keyset.path),
            "AGENT_NOTES_ACTOR_ID": "agent:worker",
        }
        yield project, repo, face, item, environment
    finally:
        reset_face()
        regista.close()


def _projection_state(entity_id):
    with coredb._conn() as connection:
        item = connection.execute(
            "SELECT * FROM work_items WHERE entity_id = %s", (entity_id,)
        ).fetchone()
        lease = connection.execute(
            "SELECT * FROM work_item_leases WHERE entity_id = %s", (entity_id,)
        ).fetchone()
        changes = connection.execute("SELECT count(*) FROM change_log").fetchone()
    return item, lease, changes


def _kernel_claim(environment, project_name, work_item_id):
    with psycopg.connect(environment["REGISTA_DSN"]) as connection:
        return connection.execute(
            sql.SQL("SELECT * FROM {}.claims WHERE work_item_id = %s").format(
                sql.Identifier(project_name)
            ),
            (work_item_id,),
        ).fetchone()


@pytest.mark.parametrize("use_json", [True, False])
@pytest.mark.parametrize("claimed", [False, True])
def test_release_refusal_is_an_envelope_and_preserves_claim_state(
    release_estate, use_json, claimed
):
    project, repo, face, item, environment = release_estate
    expected = "CLAIM_NOT_FOUND"
    if claimed:
        WorkItemModel.claim_work_item(project.id, item["identifier"])
        environment["AGENT_NOTES_ACTOR_ID"] = "agent:reviewer"
        expected = "CLAIM_LOST"
    before = _projection_state(item["entity_id"])
    claim = _kernel_claim(environment, project.slug, item["regista_work_item_id"])
    history = face.history(item["regista_work_item_id"])

    arguments = ["work-item", "release", item["identifier"], "--path", str(repo)]
    if use_json:
        arguments.append("--json")
    result = run_cli(*arguments, env=environment, check=False)

    assert result.returncode == EXIT_CONFLICT, result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    if use_json:
        envelope = json.loads(result.stdout)
        assert envelope["ok"] is False
        assert envelope["error"]["code"] == expected
        assert envelope["error"]["retryable"] is False
    else:
        assert result.stdout == ""
        assert "Error:" in result.stderr
    assert _projection_state(item["entity_id"]) == before
    assert _kernel_claim(environment, project.slug, item["regista_work_item_id"]) == claim
    assert face.history(item["regista_work_item_id"]) == history


def test_successful_release_is_unchanged_and_repeat_is_a_clean_refusal(release_estate):
    project, repo, face, item, environment = release_estate
    WorkItemModel.claim_work_item(project.id, item["identifier"])
    arguments = ["work-item", "release", item["identifier"], "--path", str(repo), "--json"]

    success = run_cli(*arguments, env=environment, check=False)
    assert success.returncode == EXIT_SUCCESS, success.stderr
    assert json.loads(success.stdout)["work_item"]["identifier"] == item["identifier"]
    assert _projection_state(item["entity_id"])[1] is None
    assert _kernel_claim(environment, project.slug, item["regista_work_item_id"]) is None
    before = _projection_state(item["entity_id"])
    history = face.history(item["regista_work_item_id"])

    repeated = run_cli(*arguments, env=environment, check=False)
    assert repeated.returncode == EXIT_CONFLICT, repeated.stderr
    assert json.loads(repeated.stdout)["error"]["code"] == "CLAIM_NOT_FOUND"
    assert _projection_state(item["entity_id"]) == before
    assert face.history(item["regista_work_item_id"]) == history
