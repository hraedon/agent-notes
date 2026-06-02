"""Tests for agent-notes-doctor health-check script (Phase 6.3).

Drives the checks against a clean ephemeral DB. Seeding workspace/project/vocab
is required so the schema checks pass.
"""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest

from agent_notes.core import db as coredb
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


def _fake_embed(text, task="document"):
    return np.zeros(768, dtype=np.float32)


class TestDoctorClean:
    @pytest.fixture(autouse=True)
    def _seed(self):
        ws = coredb.get_or_create_workspace("doc-ws", "Doc WS")
        coredb.get_or_create_project(
            ws.id,
            slug="doc-proj",
            name="Doc Proj",
            repo_root="/tmp",
        )
        coredb.add_vocabulary(ws.id, "bc_kind", "bug")
        coredb.add_vocabulary(ws.id, "bc_status", "new")
        coredb.add_vocabulary(ws.id, "bc_severity", "medium")
        coredb.add_vocabulary(ws.id, "memory_type", "note")

    def test_doctor_clean_exit_code(self, capsys):
        from agent_notes.scripts.doctor import run

        with patch("agent_notes.scripts.doctor._check_embedding", return_value=(True, "mocked")):
            code = run(check_embed=True)
        captured = capsys.readouterr()
        assert code == 0, f"Doctor failed: {captured.out}"
        assert "DSN" in captured.out
        assert "embedding" in captured.out.lower()
        assert "Schema" in captured.out
        assert "Links Audit" in captured.out
        assert "Vocabulary Integrity" in captured.out
        assert "All checks passed" in captured.out


class TestDoctorDanglingLink:
    @pytest.fixture(autouse=True)
    def _seed(self):
        ws = coredb.get_or_create_workspace("doc-link-ws", "Doc Link WS")
        proj = coredb.get_or_create_project(
            ws.id,
            slug="doc-link-proj",
            name="Doc Link Proj",
            repo_root="/tmp",
        )
        coredb.add_vocabulary(ws.id, "bc_kind", "bug")
        coredb.add_vocabulary(ws.id, "bc_status", "new")
        coredb.add_vocabulary(ws.id, "bc_severity", "medium")
        coredb.add_vocabulary(ws.id, "memory_type", "note")

        from agent_notes.core.db import _conn

        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO links
                    (from_kind, from_workspace, from_project, from_identifier,
                     to_kind, to_workspace, to_project, to_identifier, relationship)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    "breadcrumb",
                    ws.id,
                    proj.id,
                    "NONEXISTENT",
                    "memory",
                    ws.id,
                    proj.id,
                    "ghost",
                    "relates_to",
                ),
            )
            conn.commit()

    def test_doctor_catches_dangling_link(self, capsys):
        from agent_notes.scripts.doctor import run

        with patch("agent_notes.scripts.doctor._check_embedding", return_value=(True, "mocked")):
            code = run(check_embed=True)
        captured = capsys.readouterr()
        assert code == 1, f"Expected failure, got: {captured.out}"
        assert "Dangling" in captured.out


class TestDoctorSkipsOnDsnFailure:
    def test_skips_embedding_when_dsn_fails(self, capsys):
        from agent_notes.scripts.doctor import run

        with patch(
            "agent_notes.scripts.doctor._check_dsn",
            return_value=(False, "DSN not set"),
        ):
            code = run()
        captured = capsys.readouterr()
        assert code == 1
        assert "SKIPPED" in captured.out
        assert "prerequisite" in captured.out.lower()
        assert "Embedding Model" in captured.out
        assert "Links Audit" in captured.out
        assert "Vocabulary Integrity" in captured.out


class TestDoctorSkipsEmbeddingByDefault:
    @pytest.fixture(autouse=True)
    def _seed(self):
        ws = coredb.get_or_create_workspace("doc-embed-ws", "Doc Embed WS")
        coredb.get_or_create_project(
            ws.id,
            slug="doc-embed-proj",
            name="Doc Embed Proj",
            repo_root="/tmp",
        )
        coredb.add_vocabulary(ws.id, "bc_kind", "bug")
        coredb.add_vocabulary(ws.id, "bc_status", "new")
        coredb.add_vocabulary(ws.id, "bc_severity", "medium")
        coredb.add_vocabulary(ws.id, "memory_type", "note")

    def test_embedding_skipped_without_check_embed(self, capsys):
        from agent_notes.scripts.doctor import run

        run(check_embed=False)
        captured = capsys.readouterr()
        assert "SKIPPED: use --check-embed" in captured.out
