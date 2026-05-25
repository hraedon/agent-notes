from __future__ import annotations

import sys

_KIND_ALIASES = {
    "bc": "breadcrumbs",
    "breadcrumbs": "breadcrumbs",
    "memory": "memory",
    "memories": "memory",
    "search": "search",
}


def serve(kinds: list[str]) -> None:
    from agent_notes.core.server import Server

    server = Server()

    for kind in kinds:
        canonical = _KIND_ALIASES.get(kind, kind)
        kind_server = None
        if canonical == "breadcrumbs":
            from agent_notes.servers.breadcrumbs import BreadcrumbServer

            kind_server = BreadcrumbServer()
        elif canonical == "memory":
            from agent_notes.servers.memory import MemoryServer

            kind_server = MemoryServer()
        elif canonical == "search":
            from agent_notes.servers.search import SearchServer

            kind_server = SearchServer()
        else:
            raise NotImplementedError(f"unknown kind: {kind!r}")

        collisions = server.merge_registry(kind_server)
        if collisions:
            print(
                f"Warning: omnibus merge skipped colliding tool(s): {', '.join(collisions)}. "
                f"Use trace_graph_all for cross-kind traversal.",
                file=sys.stderr,
            )

    server.run()


def main_breadcrumbs() -> None:
    serve(["breadcrumbs"])


def main_memory() -> None:
    serve(["memory"])


def main_search() -> None:
    serve(["search"])


def main_omnibus() -> None:
    serve(["breadcrumbs", "memory", "search"])
