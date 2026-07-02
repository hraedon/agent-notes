"""Unit tests for the outbox file layer (Plan 009 §6.1-6.3).

Tests the low-level outbox operations: enqueue/read_all round-trip,
client_seq monotonicity, signature presence, and remove_ops rewriting.
Uses a temp outbox dir and temp signer key — no Postgres needed.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from agent_notes.core import outbox
from agent_notes.core.envelope import LocalKeySigner, parse_envelope, verify_envelope


@pytest.fixture
def outbox_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "outbox"
    monkeypatch.setenv("AGENT_NOTES_OUTBOX_DIR", str(d))
    monkeypatch.setenv("AGENT_NOTES_SESSION", uuid.uuid4().hex)
    return d


@pytest.fixture
def signer(tmp_path: Path) -> LocalKeySigner:
    return LocalKeySigner(str(tmp_path / "signing.key"))


def _make_op(op_type: str = "create", **kwargs) -> dict:
    args = {"title": "test", "description": "", "severity": "medium", "kind": "todo"}
    args.update(kwargs)
    return {
        "op": op_type,
        "work_item_id": None,
        "args": args,
        "expected_state": None,
    }


class TestEnqueueReadAll:
    def test_round_trip(self, outbox_dir: Path, signer: LocalKeySigner) -> None:
        project = "test_proj"
        op = _make_op("create", title="hello")
        env = outbox.enqueue(project, op, signer)

        assert env["payloadType"] == "agent-notes-v1/outbox-op"
        assert "payload" in env
        assert len(env["signatures"]) == 1

        entries = outbox.read_all(project)
        assert len(entries) == 1
        entry = entries[0]
        assert entry.client_seq == 1
        assert entry.envelope == env

        payload = parse_envelope(env)
        assert payload["op"] == "create"
        assert payload["args"]["title"] == "hello"
        assert payload["client_seq"] == 1

    def test_multiple_ops_ordered_by_client_seq(
        self, outbox_dir: Path, signer: LocalKeySigner
    ) -> None:
        project = "test_proj"
        for i in range(3):
            outbox.enqueue(project, _make_op("create", title=f"op-{i}"), signer)

        entries = outbox.read_all(project)
        assert len(entries) == 3
        seqs = [e.client_seq for e in entries]
        assert seqs == [1, 2, 3]

    def test_multiple_sessions_sorted(self, outbox_dir: Path, signer: LocalKeySigner) -> None:
        project = "test_proj"
        os.environ["AGENT_NOTES_SESSION"] = "session-b"
        outbox.enqueue(project, _make_op("create", title="b1"), signer)

        os.environ["AGENT_NOTES_SESSION"] = "session-a"
        outbox.enqueue(project, _make_op("create", title="a1"), signer)

        entries = outbox.read_all(project)
        assert len(entries) == 2
        assert entries[0].session == "session-a"
        assert entries[1].session == "session-b"

    def test_empty_project_returns_empty(self, outbox_dir: Path) -> None:
        assert outbox.read_all("nonexistent") == []

    def test_read_all_preserves_raw_line(self, outbox_dir: Path, signer: LocalKeySigner) -> None:
        project = "test_proj"
        env = outbox.enqueue(project, _make_op("create"), signer)
        entries = outbox.read_all(project)
        raw = entries[0].raw_line
        parsed = json.loads(raw)
        assert parsed == env


class TestClientSeq:
    def test_monotonic_increment(self, outbox_dir: Path, signer: LocalKeySigner) -> None:
        project = "test_proj"
        for _ in range(5):
            outbox.enqueue(project, _make_op("create"), signer)
        entries = outbox.read_all(project)
        assert [e.client_seq for e in entries] == [1, 2, 3, 4, 5]

    def test_client_seq_after_remove(self, outbox_dir: Path, signer: LocalKeySigner) -> None:
        project = "test_proj"
        session = os.environ["AGENT_NOTES_SESSION"]
        for i in range(3):
            outbox.enqueue(project, _make_op("create", title=f"op-{i}"), signer)

        outbox.remove_ops(project, session, {2})

        entries = outbox.read_all(project)
        assert len(entries) == 2
        assert entries[0].client_seq == 1
        assert entries[1].client_seq == 3

        env = outbox.enqueue(project, _make_op("create", title="op-new"), signer)
        payload = parse_envelope(env)
        assert payload["client_seq"] == 3

        entries = outbox.read_all(project)
        seqs = [e.client_seq for e in entries]
        assert seqs == [1, 3, 3]


class TestSignature:
    def test_signature_present_and_valid(self, outbox_dir: Path, signer: LocalKeySigner) -> None:
        project = "test_proj"
        env = outbox.enqueue(project, _make_op("create", title="signed"), signer)

        assert len(env["signatures"]) == 1
        sig = env["signatures"][0]
        assert sig["keyid"] == signer.key_id()

        payload = verify_envelope(env, signer.public_key())
        assert payload["args"]["title"] == "signed"

    def test_signature_verify_fails_with_wrong_key(
        self, outbox_dir: Path, signer: LocalKeySigner, tmp_path: Path
    ) -> None:
        project = "test_proj"
        env = outbox.enqueue(project, _make_op("create"), signer)

        other_signer = LocalKeySigner(str(tmp_path / "other.key"))
        with pytest.raises(ValueError, match="No valid signature"):
            verify_envelope(env, other_signer.public_key())


class TestRemoveOps:
    def test_remove_single_op(self, outbox_dir: Path, signer: LocalKeySigner) -> None:
        project = "test_proj"
        session = os.environ["AGENT_NOTES_SESSION"]
        for i in range(3):
            outbox.enqueue(project, _make_op("create", title=f"op-{i}"), signer)

        outbox.remove_ops(project, session, {2})

        entries = outbox.read_all(project)
        assert len(entries) == 2
        payloads = [parse_envelope(e.envelope) for e in entries]
        titles = [p["args"]["title"] for p in payloads]
        assert titles == ["op-0", "op-2"]

    def test_remove_multiple_ops(self, outbox_dir: Path, signer: LocalKeySigner) -> None:
        project = "test_proj"
        session = os.environ["AGENT_NOTES_SESSION"]
        for i in range(5):
            outbox.enqueue(project, _make_op("create", title=f"op-{i}"), signer)

        outbox.remove_ops(project, session, {1, 3, 5})

        entries = outbox.read_all(project)
        assert len(entries) == 2
        payloads = [parse_envelope(e.envelope) for e in entries]
        titles = [p["args"]["title"] for p in payloads]
        assert titles == ["op-1", "op-3"]

    def test_remove_all_ops(self, outbox_dir: Path, signer: LocalKeySigner) -> None:
        project = "test_proj"
        session = os.environ["AGENT_NOTES_SESSION"]
        outbox.enqueue(project, _make_op("create"), signer)
        outbox.enqueue(project, _make_op("create"), signer)

        outbox.remove_ops(project, session, {1, 2})

        entries = outbox.read_all(project)
        assert entries == []

    def test_remove_nonexistent_seq_noop(self, outbox_dir: Path, signer: LocalKeySigner) -> None:
        project = "test_proj"
        session = os.environ["AGENT_NOTES_SESSION"]
        outbox.enqueue(project, _make_op("create"), signer)

        outbox.remove_ops(project, session, {999})

        entries = outbox.read_all(project)
        assert len(entries) == 1

    def test_remove_nonexistent_file_noop(self, outbox_dir: Path) -> None:
        outbox.remove_ops("no_project", "no_session", {1})


class TestCountOps:
    def test_count_empty(self, outbox_dir: Path) -> None:
        assert outbox.count_ops("no_project") == 0

    def test_count_with_ops(self, outbox_dir: Path, signer: LocalKeySigner) -> None:
        project = "test_proj"
        outbox.enqueue(project, _make_op("create"), signer)
        outbox.enqueue(project, _make_op("create"), signer)
        assert outbox.count_ops(project) == 2

    def test_count_after_remove(self, outbox_dir: Path, signer: LocalKeySigner) -> None:
        project = "test_proj"
        session = os.environ["AGENT_NOTES_SESSION"]
        for _ in range(3):
            outbox.enqueue(project, _make_op("create"), signer)
        outbox.remove_ops(project, session, {1})
        assert outbox.count_ops(project) == 2


class TestListProjects:
    def test_empty(self, outbox_dir: Path) -> None:
        assert outbox.list_projects() == []

    def test_lists_projects_with_ops(self, outbox_dir: Path, signer: LocalKeySigner) -> None:
        outbox.enqueue("proj-a", _make_op("create"), signer)
        outbox.enqueue("proj-b", _make_op("create"), signer)
        assert outbox.list_projects() == ["proj-a", "proj-b"]

    def test_excludes_empty_dirs(self, outbox_dir: Path, signer: LocalKeySigner) -> None:
        outbox.enqueue("proj-a", _make_op("create"), signer)
        (outbox_dir / "proj-b").mkdir(parents=True)
        assert outbox.list_projects() == ["proj-a"]


class TestActorSerialization:
    def test_round_trip(self) -> None:
        from agent_notes.core.actor import Actor

        actor = Actor(
            actor_id="test-agent",
            actor_kind="agent",
            display_name="Test Agent",
            on_behalf_of={"principal_id": "user@example.com"},
            role="agent",
        )
        d = outbox._actor_to_dict(actor)
        restored = outbox.dict_to_actor(d)
        assert restored.actor_id == actor.actor_id
        assert restored.actor_kind == actor.actor_kind
        assert restored.display_name == actor.display_name
        assert restored.on_behalf_of == actor.on_behalf_of
        assert restored.role == actor.role
