"""Integration tests for the web frontend (Plan 003, Phase 8a).

Uses FastAPI's TestClient against a real ephemeral DB.
"""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

from agent_notes.core import db as coredb
from agent_notes.core.work_item_model import WorkItemModel
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


def _fake_embed(text, task="document"):
    return np.zeros(768, dtype=np.float32)


@pytest.fixture
def client():
    from agent_notes.web.app import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def _seed():
    ws = coredb.get_or_create_workspace("web-ws", "Web WS")
    coredb.get_or_create_project(
        ws.id,
        slug="web-proj",
        name="Web Proj",
        repo_root="/tmp",
    )
    coredb.add_vocabulary(ws.id, "wi_kind", "bug")
    coredb.add_vocabulary(ws.id, "wi_status", "open")
    coredb.add_vocabulary(ws.id, "wi_severity", "medium")
    coredb.add_vocabulary(ws.id, "memory_type", "note")


class TestIndexRoute:
    def test_index_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "agent-notes" in resp.text

    def test_index_shows_workspaces(self, client):
        resp = client.get("/")
        assert "web-ws" in resp.text


class TestWorkspaceRoute:
    def test_workspace_detail_200(self, client):
        resp = client.get("/workspaces/web-ws")
        assert resp.status_code == 200
        assert "web-proj" in resp.text

    def test_workspace_404(self, client):
        resp = client.get("/workspaces/nonexistent")
        assert resp.status_code == 404


class TestProjectRoute:
    def test_project_detail_200(self, client):
        resp = client.get("/workspaces/web-ws/web-proj")
        assert resp.status_code == 200
        assert "web-proj" in resp.text

    def test_project_404(self, client):
        resp = client.get("/workspaces/web-ws/nope")
        assert resp.status_code == 404

    def test_project_empty_state(self, client):
        resp = client.get("/workspaces/web-ws/web-proj")
        assert "No work items" in resp.text
        assert "No memories" in resp.text


class TestBreadcrumbRoutes:
    def test_breadcrumb_detail_200(self, client):
        ws = coredb.get_or_create_workspace("web-ws", "Web WS")
        projects = coredb.list_projects(workspace_id=ws.id)
        proj = next(p for p in projects if p.slug == "web-proj")

        WorkItemModel.file_work_item(
            project_id=proj.id,
            identifier="WI-WEB-001",
            title="Test WI for web",
            body="Some body text",
            kind="bug",
            status="open",
            severity="medium",
            embedding=[0.0] * 768,
        )

        resp = client.get("/workspaces/web-ws/web-proj/breadcrumbs/WI-WEB-001")
        assert resp.status_code == 200
        assert "WI-WEB-001" in resp.text
        assert "Test WI for web" in resp.text

    def test_breadcrumb_404(self, client):
        resp = client.get("/workspaces/web-ws/web-proj/breadcrumbs/NOSUCH")
        assert resp.status_code == 404

    def test_breadcrumb_appears_in_project_list(self, client):
        ws = coredb.get_or_create_workspace("web-ws", "Web WS")
        projects = coredb.list_projects(workspace_id=ws.id)
        proj = next(p for p in projects if p.slug == "web-proj")

        WorkItemModel.file_work_item(
            project_id=proj.id,
            identifier="WI-WEB-002",
            title="List test",
            kind="bug",
            status="open",
            embedding=[0.0] * 768,
        )

        resp = client.get("/workspaces/web-ws/web-proj")
        assert "WI-WEB-002" in resp.text


class TestMemoryRoutes:
    def test_memory_detail_200(self, client):
        ws = coredb.get_or_create_workspace("web-ws", "Web WS")
        projects = coredb.list_projects(workspace_id=ws.id)
        proj = next(p for p in projects if p.slug == "web-proj")

        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            from agent_notes.core.memory_model import add_memory

            add_memory(
                workspace_id=ws.id,
                project_id=proj.id,
                name="test-memory",
                memory_type="note",
                body="A test memory for the web view",
                embedding=[0.0] * 768,
            )

        resp = client.get("/workspaces/web-ws/web-proj/memories/test-memory")
        assert resp.status_code == 200
        assert "test-memory" in resp.text
        assert "A test memory" in resp.text

    def test_memory_404(self, client):
        resp = client.get("/workspaces/web-ws/web-proj/memories/nonexistent")
        assert resp.status_code == 404


class TestSearchRoute:
    def test_search_empty_query(self, client):
        resp = client.get("/search")
        assert resp.status_code == 200
        assert "Search" in resp.text

    def test_search_with_query(self, client):
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            resp = client.get("/search?q=test")
        assert resp.status_code == 200
        assert "test" in resp.text
