"""Regression: MCP-exposed core link tools must write change_log rows.

The Phase 1b dispatch initially wired `_tool_add_link` / `_tool_remove_link`
in `core/server.py` to `db.add_link` / `db.remove_link`, which do NOT write
to change_log. This violates decision 20 (every state-mutating tool writes a
change_log row in the same transaction). The tools were rewired to call
`links.add_link` / `links.remove_link`, which do. This test pins that
behavior so a future refactor can't silently reintroduce the asymmetry.
"""

from __future__ import annotations

import io
import json
import sys
from datetime import datetime, timezone

from agent_notes.core import change_log as cl
from agent_notes.core import db as coredb
from agent_notes.core.server import Server


def _rpc(method: str, params: dict | None = None, req_id: int = 1) -> str:
    obj: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        obj["params"] = params
    return json.dumps(obj)


def _drive(server: Server, messages: list[str]) -> list[dict]:
    stdin_text = "\n".join(messages) + "\n"
    orig_stdin, orig_stdout = sys.stdin, sys.stdout
    try:
        sys.stdin = io.StringIO(stdin_text)
        sys.stdout = io.StringIO()
        server.run()
        output = sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = orig_stdin, orig_stdout
    return [json.loads(line) for line in output.strip().split("\n") if line.strip()]


def test_mcp_add_link_writes_change_log(ephemeral_db):
    ws = coredb.get_or_create_workspace("audit-ws", "Audit Tools WS")
    proj = coredb.get_or_create_project(ws.id, "audit-proj", "Audit Proj")

    server = Server()
    _drive(
        server,
        [
            _rpc("initialize", {"protocolVersion": "2024-11-05"}),
            _rpc(
                "tools/call",
                {
                    "name": "add_link",
                    "arguments": {
                        "from_kind": "breadcrumb",
                        "from_workspace": ws.id,
                        "from_project": proj.id,
                        "from_identifier": "BC-AUDIT-1",
                        "to_kind": "memory",
                        "to_workspace": ws.id,
                        "to_project": proj.id,
                        "to_identifier": "mem-audit-1",
                        "relationship": "relates_to",
                    },
                },
                req_id=2,
            ),
        ],
    )

    since = datetime(2000, 1, 1, tzinfo=timezone.utc)
    rows = cl.changes_since(since, workspace_id=ws.id, kind="breadcrumb")
    matching = [r for r in rows if r.event == "link_added" and r.identifier == "BC-AUDIT-1"]
    assert matching, "MCP add_link tool must write a change_log row (decision 20)"


def test_mcp_remove_link_writes_change_log(ephemeral_db):
    ws = coredb.get_or_create_workspace("audit-rm-ws", "Audit Remove WS")
    proj = coredb.get_or_create_project(ws.id, "audit-rm-proj", "Audit Remove Proj")

    # Seed a link via the same MCP path so the test exercises end-to-end.
    server = Server()
    _drive(
        server,
        [
            _rpc("initialize", {"protocolVersion": "2024-11-05"}),
            _rpc(
                "tools/call",
                {
                    "name": "add_link",
                    "arguments": {
                        "from_kind": "breadcrumb",
                        "from_workspace": ws.id,
                        "from_project": proj.id,
                        "from_identifier": "BC-AUDIT-RM-1",
                        "to_kind": "memory",
                        "to_workspace": ws.id,
                        "to_project": proj.id,
                        "to_identifier": "mem-audit-rm-1",
                        "relationship": "relates_to",
                    },
                },
                req_id=2,
            ),
            _rpc(
                "tools/call",
                {
                    "name": "remove_link",
                    "arguments": {
                        "from_kind": "breadcrumb",
                        "from_workspace": ws.id,
                        "from_project": proj.id,
                        "from_identifier": "BC-AUDIT-RM-1",
                        "to_kind": "memory",
                        "to_workspace": ws.id,
                        "to_project": proj.id,
                        "to_identifier": "mem-audit-rm-1",
                        "relationship": "relates_to",
                    },
                },
                req_id=3,
            ),
        ],
    )

    since = datetime(2000, 1, 1, tzinfo=timezone.utc)
    rows = cl.changes_since(since, workspace_id=ws.id, kind="breadcrumb")
    removed = [r for r in rows if r.event == "link_removed" and r.identifier == "BC-AUDIT-RM-1"]
    assert removed, "MCP remove_link tool must write a change_log row (decision 20)"
