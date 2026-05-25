"""agent-notes CLI (Plan 004 Phase 9a).

Noun/verb argparse tree: `agent-notes breadcrumb file`, `agent-notes memory add`, etc.
All commands accept `--path` (default cwd) and resolve via `core.db.resolve_project`.
`--json` produces machine-parseable output. Stable exit codes per decision 52.
"""

from __future__ import annotations

import argparse
import json
import os

from agent_notes.cli.breadcrumbs import register_breadcrumb_parsers
from agent_notes.cli.changes import register_changes_parsers
from agent_notes.cli.common import EXIT_GENERIC, EXIT_NOT_CONFIGURED, EXIT_SUCCESS
from agent_notes.cli.links import register_link_parsers
from agent_notes.cli.memory import register_memory_parsers
from agent_notes.cli.search import register_search_parsers
from agent_notes.cli.skills import register_skills_parser
from agent_notes.cli.vocabulary import register_vocabulary_parsers
from agent_notes.cli.workspace import register_workspace_parsers


def cmd_init(path: str | None) -> int:
    import os

    target = path or "."
    target = os.path.abspath(target)

    git_root = target
    while git_root != "/":
        if os.path.isdir(os.path.join(git_root, ".git")):
            break
        parent = os.path.dirname(git_root)
        if parent == git_root:
            break
        git_root = parent

    if not os.path.isdir(os.path.join(git_root, ".git")):
        print(f"Note: no git repo found above {target}; using {target} as repo root.")
        git_root = target

    repo_root = os.path.abspath(git_root)
    name = os.path.basename(repo_root)

    from agent_notes.core.db import get_or_create_project, get_or_create_workspace

    ws = get_or_create_workspace("default", "Default Workspace")
    get_or_create_project(ws.id, slug=name, name=name, repo_root=repo_root)
    print(f"Project '{name}' registered (workspace=default, repo_root={repo_root}).")
    return EXIT_SUCCESS


def cmd_resolve(path: str | None, use_json: bool) -> int:
    from agent_notes.core.db import resolve_project as db_resolve_project

    target = os.path.abspath(path or ".")
    try:
        result = db_resolve_project(target)
    except ValueError as exc:
        if use_json:
            print(json.dumps({"error": str(exc), "code": EXIT_NOT_CONFIGURED}))
        else:
            print(exc)
        return EXIT_NOT_CONFIGURED
    if use_json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(result)
    return EXIT_SUCCESS


def cmd_doctor(use_json: bool) -> int:
    from agent_notes.scripts.doctor import run as doctor_run

    code = doctor_run()
    if code != 0 and use_json:
        print(json.dumps({"status": "unhealthy", "doctor_exit": code}))
    elif use_json:
        print(json.dumps({"status": "healthy"}))
    return code


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agent-notes",
        description="agent-notes CLI — sync interface to breadcrumbs, memories, and search",
    )
    sub = parser.add_subparsers(dest="command")

    init_p = sub.add_parser("init", help="Idempotently register a project from a path")
    init_p.add_argument("path", nargs="?", default=".")

    resolve_p = sub.add_parser("resolve", help="Resolve a filesystem path to a registered project")
    resolve_p.add_argument("--path", default=".")
    resolve_p.add_argument("--json", action="store_true")

    doctor_p = sub.add_parser("doctor", help="Health check")
    doctor_p.add_argument("--json", action="store_true")

    register_breadcrumb_parsers(sub)
    register_memory_parsers(sub)
    register_link_parsers(sub)
    register_search_parsers(sub)
    register_vocabulary_parsers(sub)
    register_workspace_parsers(sub)
    register_changes_parsers(sub)
    register_skills_parser(sub)

    args = parser.parse_args()

    if args.command == "init":
        return cmd_init(args.path)
    if args.command == "resolve":
        return cmd_resolve(args.path, args.json)
    if args.command == "doctor":
        return cmd_doctor(args.json)

    func = getattr(args, "func", None)
    if func is not None:
        return func(args)

    parser.print_help()
    return EXIT_GENERIC


from agent_notes.cli.serve import (  # noqa: E402
    main_breadcrumbs,
    main_memory,
    main_omnibus,
    main_search,
    serve,
)

__all__ = [
    "main",
    "main_breadcrumbs",
    "main_memory",
    "main_omnibus",
    "main_search",
    "serve",
]
