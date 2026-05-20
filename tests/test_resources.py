"""Tests for core/resources.py (URI parsing, building, handler scaffolding)
and an end-to-end smoke test that a Server subclass can register a fake
handler and have `resources/list` + `resources/read` work.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from agent_notes.core.resources import (
    build_uri,
    make_resource_handler,
    parse_uri,
)
from agent_notes.core.server import Server


class TestParseUri:
    def test_full_uri(self) -> None:
        r = parse_uri("note://breadcrumb/default/my-project/BC-001")
        assert r.kind == "breadcrumb"
        assert r.workspace == "default"
        assert r.project == "my-project"
        assert r.identifier == "BC-001"

    def test_collection_prefix(self) -> None:
        r = parse_uri("note://memory/ws1/proj1/")
        assert r.kind == "memory"
        assert r.workspace == "ws1"
        assert r.project == "proj1"
        assert r.identifier is None  # trailing slash → empty segment filtered

    def test_workspace_only(self) -> None:
        r = parse_uri("note://breadcrumb/default/")
        assert r.kind == "breadcrumb"
        assert r.workspace == "default"
        assert r.project is None

    def test_invalid_scheme(self) -> None:
        with pytest.raises(ValueError, match="Unsupported URI scheme"):
            parse_uri("https://example.com/foo")

    def test_missing_workspace(self) -> None:
        with pytest.raises(ValueError):
            parse_uri("note://breadcrumb")

    def test_is_listing_true(self) -> None:
        r = parse_uri("note://bc/ws/proj/")
        assert r.is_listing() is True

    def test_is_listing_false(self) -> None:
        r = parse_uri("note://bc/ws/proj/BC-001")
        assert r.is_listing() is False


class TestBuildUri:
    def test_full_uri(self) -> None:
        uri = build_uri("breadcrumb", "default", "my-proj", "BC-001")
        assert uri == "note://breadcrumb/default/my-proj/BC-001"

    def test_without_identifier(self) -> None:
        uri = build_uri("memory", "ws1", "proj1")
        assert uri == "note://memory/ws1/proj1"

    def test_without_project(self) -> None:
        uri = build_uri("breadcrumb", "default")
        assert uri == "note://breadcrumb/default"

    def test_round_trip(self) -> None:
        original = "note://breadcrumb/ws/proj/ID-001"
        r = parse_uri(original)
        rebuilt = build_uri(r.kind, r.workspace, r.project, r.identifier)
        assert rebuilt == original


class _FakeKindServer(Server):
    """Stub kind server that exposes one fake note via a resource handler."""

    name = "fake-kind"
    version = "0.0.1"

    def __init__(self) -> None:
        super().__init__()

        fake_notes = {
            ("default", "proj1", "NOTE-001"): "# NOTE-001\n\nHello from fake kind.",
        }

        def _list_fn(workspace: str, project: str) -> list[dict]:
            return [
                {
                    "uri": build_uri("fake", workspace, project, ident),
                    "name": ident,
                    "mimeType": "text/markdown",
                }
                for (ws, p, ident) in fake_notes
                if ws == workspace and p == project
            ]

        def _read_fn(workspace: str, project: str, identifier: str) -> str:
            key = (workspace, project, identifier)
            if key not in fake_notes:
                raise KeyError(f"Not found: {key!r}")
            return fake_notes[key]

        self.register_resource_handler(
            "note://fake/",
            make_resource_handler("fake", _list_fn, _read_fn),
        )


def _rpc(method: str, params: dict | None = None, req_id: int = 1) -> str:
    msg: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params:
        msg["params"] = params
    return json.dumps(msg)


def _run_server(messages: list[str]) -> list[dict]:
    stdin_text = "\n".join(messages) + "\n"
    server = _FakeKindServer()
    orig_stdin, orig_stdout = sys.stdin, sys.stdout
    try:
        sys.stdin = io.StringIO(stdin_text)
        sys.stdout = io.StringIO()
        server.run()
        output = sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = orig_stdin, orig_stdout
    results = []
    for line in output.strip().split("\n"):
        if line.strip():
            results.append(json.loads(line))
    return results


class TestResourcesEndToEnd:
    def test_resources_list_returns_200(self) -> None:
        responses = _run_server([_rpc("resources/list")])
        assert len(responses) == 1
        assert "result" in responses[0]
        # The handler's list action returns [] for now (workspace context absent).
        assert isinstance(responses[0]["result"]["resources"], list)

    def test_resources_read_known_uri(self) -> None:
        uri = "note://fake/default/proj1/NOTE-001"
        responses = _run_server([_rpc("resources/read", {"uri": uri})])
        assert len(responses) == 1
        contents = responses[0]["result"]["contents"]
        assert len(contents) == 1
        assert "NOTE-001" in contents[0]["text"]

    def test_resources_read_unknown_uri_returns_error(self) -> None:
        uri = "note://fake/default/proj1/MISSING-999"
        responses = _run_server([_rpc("resources/read", {"uri": uri})])
        assert len(responses) == 1
        assert "error" in responses[0]

    def test_resources_read_wrong_prefix_returns_error(self) -> None:
        uri = "note://other_kind/default/proj1/X"
        responses = _run_server([_rpc("resources/read", {"uri": uri})])
        assert len(responses) == 1
        assert "error" in responses[0]


class TestServerInheritsAllCoreTools:
    """Every Server subclass must expose the full core tool set (decisions 16, 22)."""

    def test_core_tools_present(self) -> None:
        server = _FakeKindServer()
        tool_names = {t["name"] for t in server._registry.list_tools()}
        expected = {
            "list_workspaces",
            "list_projects",
            "list_vocabulary",
            "archive_vocabulary",
            "changes_since",
            "add_link",
            "remove_link",
            "history",
        }
        missing = expected - tool_names
        assert not missing, f"Missing core tools: {missing}"
