"""Entry-point shims (decision 7 / GLM #7).

`main()` is the generic `agent-notes serve --kinds X,Y,...` parser.
`main_breadcrumbs()`, `main_memory()`, `main_search()` are thin wrappers that
call `serve(kinds=[...])` so harness configs (Claude/OpenCode/Gemini) stay
ergonomic — one named binary per server kind.

Invoking `agent-notes serve --kinds bc,memory` mounts multiple registries in
one process (decision 12 — omnibus mode is the same code path).
"""

from __future__ import annotations

import argparse
import sys

_KIND_ALIASES = {
    "bc": "breadcrumbs",
    "breadcrumbs": "breadcrumbs",
    "memory": "memory",
    "memories": "memory",
    "search": "search",
}


def serve(kinds: list[str]) -> None:
    """Instantiate and run a server mounting the given kind registries.

    In omnibus mode, resource handlers with different URI prefixes do not
    collide (e.g. note://breadcrumb/ vs note://memory/). If two registries
    register the same prefix, the last one wins via ToolRegistry.merge.
    """
    from agent_notes.core.server import Server

    server = Server()

    for kind in kinds:
        canonical = _KIND_ALIASES.get(kind, kind)
        if canonical == "breadcrumbs":
            from agent_notes.servers.breadcrumbs import BreadcrumbServer

            server.merge_registry(BreadcrumbServer())
        elif canonical == "memory":
            from agent_notes.servers.memory import MemoryServer

            server.merge_registry(MemoryServer())
        elif canonical == "search":
            from agent_notes.servers.search import SearchServer

            server.merge_registry(SearchServer())
        else:
            raise NotImplementedError(f"unknown kind: {kind!r}")

    server.run()  # reached only when all kinds are implemented


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agent-notes",
        description="agent-notes MCP server — generic entry point",
    )
    sub = parser.add_subparsers(dest="command")
    serve_cmd = sub.add_parser("serve", help="Start the MCP server for given kinds")
    serve_cmd.add_argument(
        "--kinds",
        required=True,
        help="Comma-separated list of kinds to mount: bc,memory,search",
    )
    args = parser.parse_args()

    if args.command == "serve":
        kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
        if not kinds:
            sys.exit("Error: --kinds must be non-empty")
        serve(kinds)
    else:
        parser.print_help()
        sys.exit(1)


def main_breadcrumbs() -> None:
    serve(["breadcrumbs"])


def main_memory() -> None:
    serve(["memory"])


def main_search() -> None:
    serve(["search"])


def main_omnibus() -> None:
    """Convenience entry point: mount breadcrumbs, memory, and search in one process."""
    serve(["breadcrumbs", "memory", "search"])
