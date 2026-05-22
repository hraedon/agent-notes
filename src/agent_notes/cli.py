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

    In omnibus mode, core tools (list_workspaces, etc.) are deduplicated
    silently because they are functionally identical. Kind-specific tool
    name collisions (e.g. trace_graph in breadcrumbs + memory) are
    first-registration-wins with a warning printed to stderr. Resource
    handlers are deduplicated by URI prefix.
    """
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
                f"Warning: omnibus merge skipped colliding tool(s): "
                f"{', '.join(collisions)}. First registration wins. "
                f"Use trace_graph_all for cross-kind traversal.",
                file=sys.stderr,
            )

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
    init_cmd = sub.add_parser("init", help="Idempotently register a project from a path")
    init_cmd.add_argument(
        "path", nargs="?", default=".", help="Directory or file under the project"
    )

    args = parser.parse_args()

    if args.command == "serve":
        kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
        if not kinds:
            sys.exit("Error: --kinds must be non-empty")
        serve(kinds)
    elif args.command == "init":
        from agent_notes.scripts.init_project import main as init_main
        init_main(args)
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
