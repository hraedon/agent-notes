"""Hand-rolled JSON-RPC / MCP stdio loop.

Decision 21: the official `mcp` Python SDK (v1.27 at evaluation time) was
examined and rejected for this project. The SDK's `stdio_server` and `Server`
class are fully async — built on anyio with asynccontextmanager, async
generators, and `await`-based I/O throughout. Adopting it would require either
(a) running an asyncio event loop for what is inherently a synchronous,
stdin-blocking server, or (b) wrapping every psycopg and embedding call with
`asyncio.run()` or a thread executor. Decision 22 mandates sync throughout:
stdin loop is blocking sync, `psycopg_pool.ConnectionPool` is the sync
variant, and embedding is in-process sync. Fighting the SDK's async model
would add more friction than a 60-line hand-rolled loop. The existing two
legacy servers (`breadcrumb-mcp`, `memory-mcp`) already prove the pattern is
sufficient; we extract it here as a shared module so kind servers never copy it
again.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _ok(req_id: Any, text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {"content": [{"type": "text", "text": text}]},
    }


def _err(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


class ToolRegistry:
    """Per-class tool registry so multiple kind registries compose cleanly (decision 12)."""

    def __init__(self) -> None:
        self._tools: list[dict] = []
        self._fns: dict[str, Any] = {}
        self._resource_handlers: list[tuple[str, Any]] = []

    def register(self, name: str, schema: dict, fn: Any) -> None:
        self._tools.append({"name": name, **schema})
        self._fns[name] = fn

    def register_resource_handler(self, uri_prefix: str, fn: Any) -> None:
        self._resource_handlers.append((uri_prefix, fn))

    def call(self, name: str, args: dict) -> str:
        fn = self._fns.get(name)
        if fn is None:
            raise KeyError(name)
        return fn(args)

    def list_tools(self) -> list[dict]:
        return list(self._tools)

    def list_resource_handlers(self) -> list[tuple[str, Any]]:
        return list(self._resource_handlers)

    def merge(self, other: "ToolRegistry") -> None:
        """Merge another registry into this one (omnibus composition, decision 12)."""
        for tool in other._tools:
            name = tool["name"]
            if name not in self._fns:
                self._tools.append(tool)
                self._fns[name] = other._fns[name]
        for prefix, fn in other._resource_handlers:
            self._resource_handlers.append((prefix, fn))
