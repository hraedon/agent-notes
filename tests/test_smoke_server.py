"""Smoke tests for core/server.py and core/mcp.py."""

from __future__ import annotations

import io
import json
import sys

from agent_notes.core.server import Server


class _EchoServer(Server):
    """Minimal subclass that registers one extra tool."""

    name = "echo-test"
    version = "0.0.1"

    def __init__(self) -> None:
        super().__init__()
        self.register_tool(
            "echo",
            {
                "description": "Echo the 'text' argument back.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
            lambda args: args.get("text", ""),
        )


def _rpc(method: str, params: dict | None = None, req_id: int = 1) -> str:
    msg: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params:
        msg["params"] = params
    return json.dumps(msg)


def _run_server(messages: list[str]) -> list[dict]:
    """Feed messages to a fresh server via fake stdin/stdout and return parsed responses."""
    stdin_text = "\n".join(messages) + "\n"
    server = _EchoServer()

    orig_stdin = sys.stdin
    orig_stdout = sys.stdout
    try:
        sys.stdin = io.StringIO(stdin_text)
        sys.stdout = io.StringIO()
        server.run()
        output = sys.stdout.getvalue()
    finally:
        sys.stdin = orig_stdin
        sys.stdout = orig_stdout

    results = []
    for line in output.strip().split("\n"):
        if line.strip():
            results.append(json.loads(line))
    return results


class TestServerProtocol:
    def test_initialize(self) -> None:
        responses = _run_server([_rpc("initialize")])
        assert len(responses) == 1
        r = responses[0]
        assert r["id"] == 1
        assert r["result"]["protocolVersion"] == "2024-11-05"
        assert r["result"]["serverInfo"]["name"] == "echo-test"

    def test_tools_list_includes_echo_and_core_tools(self) -> None:
        responses = _run_server([_rpc("tools/list")])
        assert len(responses) == 1
        tools = {t["name"] for t in responses[0]["result"]["tools"]}
        assert "echo" in tools
        assert "list_workspaces" in tools
        assert "list_projects" in tools
        assert "changes_since" in tools
        assert "add_link" in tools
        assert "remove_link" in tools

    def test_tools_call_echo(self) -> None:
        responses = _run_server(
            [
                _rpc("tools/call", {"name": "echo", "arguments": {"text": "hello"}}),
            ]
        )
        assert len(responses) == 1
        content = responses[0]["result"]["content"]
        assert content[0]["text"] == "hello"

    def test_tools_call_unknown_returns_error(self) -> None:
        responses = _run_server(
            [
                _rpc("tools/call", {"name": "nonexistent", "arguments": {}}),
            ]
        )
        assert len(responses) == 1
        assert "error" in responses[0]
        assert responses[0]["error"]["code"] == -32601

    def test_ping(self) -> None:
        responses = _run_server([_rpc("ping")])
        assert len(responses) == 1
        assert responses[0]["result"] == {}

    def test_unknown_method_returns_error(self) -> None:
        responses = _run_server([_rpc("unknown/method")])
        assert len(responses) == 1
        assert "error" in responses[0]

    def test_notifications_initialized_is_silent(self) -> None:
        responses = _run_server(
            [
                _rpc("notifications/initialized"),
            ]
        )
        assert len(responses) == 0

    def test_multiple_messages_in_sequence(self) -> None:
        responses = _run_server(
            [
                _rpc("initialize", req_id=1),
                _rpc("tools/list", req_id=2),
                _rpc("tools/call", {"name": "echo", "arguments": {"text": "world"}}, req_id=3),
            ]
        )
        assert len(responses) == 3
        ids = [r["id"] for r in responses]
        assert ids == [1, 2, 3]


class TestToolRegistry:
    def test_merge_registries(self) -> None:
        s1 = _EchoServer()
        s2 = _EchoServer()
        s2.register_tool(
            "another",
            {
                "description": "x",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
            lambda args: "another",
        )
        s1.merge_registry(s2)
        tool_names = {t["name"] for t in s1._registry.list_tools()}
        assert "another" in tool_names
        assert "echo" in tool_names
