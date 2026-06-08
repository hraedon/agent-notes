"""Integration tests for Plan 008 P1 — verifier."""

from __future__ import annotations

import argparse
import base64
import json

import pytest

from agent_notes.core import kernel
from agent_notes.core.envelope import LocalKeySigner, NullSigner, make_envelope
from agent_notes.core.verifier import (
    apply_policy,
    verify_all,
    verify_entity,
    verify_hash_chain,
    verify_op_id,
    verify_signature,
)
from agent_notes.core.work_item_model import WorkItemModel

# Import the fixture so pytest discovers it
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


@pytest.fixture
def default_project():
    from agent_notes.core import db as coredb

    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    proj = coredb.get_or_create_project(
        ws.id,
        slug="sf2",
        name="sf2",
        repo_root="/projects/sf2",
    )
    return proj


def _vec768():
    return [0.0] * 768


# ---------------------------------------------------------------------------
# Unit tests for primitive checks
# ---------------------------------------------------------------------------


class TestVerifyOpId:
    def test_valid_op_id(self):
        payload = {"title": "Test", "project_id": 1, "identifier": "WI-001"}
        op = {
            "op_id": kernel._make_op_id("work_item", "create", payload, []),
            "entity_type": "work_item",
            "op_type": "create",
            "payload": payload,
            "parent_op_ids": [],
        }
        assert verify_op_id(op) is None

    def test_invalid_op_id(self):
        payload = {"title": "Test", "project_id": 1, "identifier": "WI-001"}
        op = {
            "op_id": "wrong-hash",
            "entity_type": "work_item",
            "op_type": "create",
            "payload": payload,
            "parent_op_ids": [],
        }
        v = verify_op_id(op)
        assert v is not None
        assert v.rule == "hash"
        assert "mismatch" in v.message


class TestVerifyHashChain:
    def test_valid_parent(self):
        op = {
            "op_id": "abc",
            "parent_op_ids": ["parent-1"],
        }
        assert verify_hash_chain(op, {"parent-1"}) is None

    def test_missing_parent(self):
        op = {
            "op_id": "abc",
            "parent_op_ids": ["missing"],
        }
        v = verify_hash_chain(op, {"parent-1"})
        assert v is not None
        assert v.rule == "parent"
        assert "not found" in v.message

    def test_no_parents(self):
        op = {
            "op_id": "abc",
            "parent_op_ids": [],
        }
        assert verify_hash_chain(op, set()) is None


class TestVerifySignature:
    def test_null_signer_no_key(self):
        envelope = make_envelope("test", {}, signer=NullSigner())
        op = {
            "op_id": "abc",
            "payload": {"envelope": envelope},
        }
        assert verify_signature(op, public_key=None) is None

    def test_null_signer_with_key(self):
        envelope = make_envelope("test", {}, signer=NullSigner())
        op = {
            "op_id": "abc",
            "payload": {"envelope": envelope},
        }
        v = verify_signature(op, public_key=b"dummy")
        assert v is not None
        assert v.rule == "signature"
        assert "Placeholder" in v.message

    def test_real_signer_no_key(self):
        signer = LocalKeySigner(key_path="/tmp/test_verify.key")
        envelope = make_envelope("test", {}, signer=signer)
        op = {
            "op_id": "abc",
            "payload": {"envelope": envelope},
        }
        v = verify_signature(op, public_key=None)
        assert v is not None
        assert v.rule == "signature"
        assert v.severity == "warning"

    def test_real_signer_with_key(self):
        signer = LocalKeySigner(key_path="/tmp/test_verify.key")
        envelope = make_envelope("test", {}, signer=signer)
        op = {
            "op_id": "abc",
            "payload": {"envelope": envelope},
        }
        v = verify_signature(op, public_key=signer._public_key)
        assert v is None

    def test_missing_envelope(self):
        op = {
            "op_id": "abc",
            "payload": {},
        }
        v = verify_signature(op, public_key=None)
        assert v is not None
        assert v.rule == "signature"
        assert "Missing envelope" in v.message


class TestApplyPolicy:
    def test_valid_create(self):
        op = {
            "op_id": "abc",
            "op_type": "create",
            "entity_type": "work_item",
            "actor_id": "actor-1",
            "payload": {},
        }
        assert apply_policy(op) is None

    def test_missing_actor(self):
        op = {
            "op_id": "abc",
            "op_type": "create",
            "entity_type": "work_item",
            "actor_id": None,
            "payload": {},
        }
        v = apply_policy(op)
        assert v is not None
        assert v.rule == "policy"
        assert "Missing actor_id" in v.message

    def test_invalid_op_type(self):
        op = {
            "op_id": "abc",
            "op_type": "hack",
            "entity_type": "work_item",
            "actor_id": "actor-1",
            "payload": {},
        }
        v = apply_policy(op)
        assert v is not None
        assert v.rule == "policy"
        assert "Unknown op_type" in v.message

    def test_invalid_status(self):
        op = {
            "op_id": "abc",
            "op_type": "set_status",
            "entity_type": "work_item",
            "actor_id": "actor-1",
            "payload": {"status": "hacked"},
        }
        v = apply_policy(op)
        assert v is not None
        assert v.rule == "policy"
        assert "Invalid status" in v.message


# ---------------------------------------------------------------------------
# Entity-level and global verification
# ---------------------------------------------------------------------------


class TestVerifyEntity:
    def test_verify_entity_all_pass(self, default_project):
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-V-01",
            title="Verified",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]
        result = verify_entity(entity_id=entity_id)
        assert result.ok()
        assert result.checked >= 1
        assert result.passed == result.checked

    def test_verify_entity_with_wrong_key(self, default_project):
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-V-02",
            title="Verified",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]
        # Provide a wrong key
        result = verify_entity(entity_id=entity_id, public_key=b"\x00" * 32)
        # NullSigner is used by default, so this should be a warning
        # about placeholder signature with key supplied
        assert not result.ok()
        assert any(v.rule == "signature" for v in result.violations)


class TestVerifyAll:
    def test_verify_all(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-V-03",
            title="All verify",
            embedding=_vec768(),
        )
        result = verify_all()
        assert result.checked >= 1
        assert result.passed >= 1

    def test_verify_all_by_entity_type(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-V-04",
            title="Filter verify",
            embedding=_vec768(),
        )
        result = verify_all(entity_type="work_item")
        assert result.checked >= 1

        result2 = verify_all(entity_type="memory")
        assert result2.checked == 0


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


class TestVerifyCli:
    def test_cli_verify_run(self, default_project, capsys):
        from agent_notes.cli.verify import cmd_verify

        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-V-CLI",
            title="CLI verify",
            embedding=_vec768(),
        )

        ns = argparse.Namespace(
            json=True,
            entity_id=None,
            entity_type=None,
            public_key=None,
            no_policy=False,
        )
        assert cmd_verify(ns) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["ok"] is True
        assert data["checked"] >= 1

    def test_cli_verify_entity(self, default_project, capsys):
        from agent_notes.cli.verify import cmd_verify

        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-V-CLI-02",
            title="Entity verify",
            embedding=_vec768(),
        )

        ns = argparse.Namespace(
            json=True,
            entity_id=wi["entity_id"],
            entity_type=None,
            public_key=None,
            no_policy=False,
        )
        assert cmd_verify(ns) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["ok"] is True

    def test_cli_verify_with_wrong_key(self, default_project, capsys):
        from agent_notes.cli.verify import cmd_verify

        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-V-CLI-03",
            title="Key verify",
            embedding=_vec768(),
        )

        ns = argparse.Namespace(
            json=True,
            entity_id=None,
            entity_type=None,
            public_key=base64.b64encode(b"\x00" * 32).decode(),
            no_policy=False,
        )
        assert cmd_verify(ns) == 1
        data = json.loads(capsys.readouterr().out)
        assert data["ok"] is False
        assert any(v["rule"] == "signature" for v in data["violations"])
