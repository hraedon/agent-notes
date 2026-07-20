"""agent-notes CLI (Plan 004 Phase 9a).

Noun/verb argparse tree: `agent-notes breadcrumb file`, `agent-notes memory add`, etc.
All commands accept `--path` (default cwd) and resolve via `core.db.resolve_project`.
`--json` produces machine-parseable output. Stable exit codes per decision 52.
"""

from __future__ import annotations

import argparse
import json
import os

from agent_notes.cli.admin import register_admin_parsers
from agent_notes.cli.breadcrumbs import register_breadcrumb_parsers
from agent_notes.cli.changes import register_changes_parsers
from agent_notes.cli.common import EXIT_GENERIC, EXIT_NOT_CONFIGURED, EXIT_SUCCESS
from agent_notes.cli.events import register_events_parsers
from agent_notes.cli.export import register_export_parsers
from agent_notes.cli.harness import register_harness_parser
from agent_notes.cli.links import register_link_parsers
from agent_notes.cli.memory import register_memory_parsers
from agent_notes.cli.memory_provider import register_memory_provider_parsers
from agent_notes.cli.orient import register_orient_parser
from agent_notes.cli.outbox import register_outbox_parsers
from agent_notes.cli.projection import register_projection_parsers
from agent_notes.cli.search import register_search_parsers
from agent_notes.cli.skills import register_skills_parser
from agent_notes.cli.verify import register_verify_parsers
from agent_notes.cli.vocabulary import register_vocabulary_parsers
from agent_notes.cli.work_items import register_work_item_parsers
from agent_notes.cli.workspace import register_workspace_parsers
from agent_notes.codex_lifecycle import register_codex_hook_parser


def _install_claude_session_hook(repo_root: str) -> tuple[str, bool]:
    """Wire a SessionStart hook into <repo>/.claude/settings.json that runs
    `agent-notes orient`, so orientation is injected every session without the
    agent having to remember. Merges into existing settings; idempotent.

    Upgrades old-format commands (with POSIX-only ``2>/dev/null || true``) to
    the cross-platform bare command. Returns (settings_path, changed).
    """
    settings_path = os.path.join(repo_root, ".claude", "settings.json")
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    settings: dict = {}
    if os.path.exists(settings_path):
        try:
            settings = json.loads(open(settings_path, encoding="utf-8").read()) or {}
        except (json.JSONDecodeError, OSError):
            settings = {}
    hooks = settings.setdefault("hooks", {})
    session_start = hooks.setdefault("SessionStart", [])
    command = f"agent-notes orient --path {repo_root}"
    changed = False
    found = False
    for entry in session_start:
        if not isinstance(entry, dict):
            continue
        for h in entry.get("hooks", []):
            if not isinstance(h, dict):
                continue
            cmd = h.get("command", "")
            if cmd.startswith("agent-notes orient"):
                found = True
                if cmd != command:
                    h["command"] = command
                    changed = True
    if not found:
        session_start.append({"hooks": [{"type": "command", "command": command}]})
        changed = True
    with open(settings_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(settings, indent=2) + "\n")
    return settings_path, changed


def _install_claude_stop_hook(repo_root: str) -> tuple[str, bool]:
    """Wire a Stop hook into <repo>/.claude/settings.json that replays the
    regista outbox (Plan 009 §12.4). Non-optional reconcile on session end; if
    regista is unreachable the reconcile is a no-op and `orient`/`outbox status`
    surface the stale count next session. Idempotent. Returns (path, changed).
    """
    settings_path = os.path.join(repo_root, ".claude", "settings.json")
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    settings: dict = {}
    if os.path.exists(settings_path):
        try:
            settings = json.loads(open(settings_path, encoding="utf-8").read()) or {}
        except (json.JSONDecodeError, OSError):
            settings = {}
    hooks = settings.setdefault("hooks", {})
    stop = hooks.setdefault("Stop", [])
    command = "agent-notes outbox reconcile"
    changed = False
    found = False
    for entry in stop:
        if not isinstance(entry, dict):
            continue
        for h in entry.get("hooks", []):
            if not isinstance(h, dict):
                continue
            cmd = h.get("command", "")
            if cmd.startswith("agent-notes outbox reconcile"):
                found = True
                if cmd != command:
                    h["command"] = command
                    changed = True
    if not found:
        stop.append({"hooks": [{"type": "command", "command": command}]})
        changed = True
    with open(settings_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(settings, indent=2) + "\n")
    return settings_path, changed


def cmd_init(
    path: str | None,
    workspace_slug: str | None = None,
    install_hooks: bool = True,
) -> int:
    target = os.path.abspath(path or ".")

    git_root = target
    while git_root != os.path.dirname(git_root):
        if os.path.isdir(os.path.join(git_root, ".git")):
            break
        git_root = os.path.dirname(git_root)

    if not os.path.isdir(os.path.join(git_root, ".git")):
        print(f"Note: no git repo found above {target}; using {target} as repo root.")
        git_root = target

    repo_root = os.path.abspath(git_root)
    name = os.path.basename(repo_root)

    from agent_notes.core.db import get_or_create_project, get_or_create_workspace

    ws_slug = workspace_slug or "default"
    ws_name = ws_slug.replace("-", " ").title()
    ws = get_or_create_workspace(ws_slug, ws_name)
    get_or_create_project(ws.id, slug=name, name=name, repo_root=repo_root)
    print(f"Project '{name}' registered (workspace={ws_slug}, repo_root={repo_root}).")

    if install_hooks:
        settings_path, changed = _install_claude_session_hook(repo_root)
        verb = "wired" if changed else "already present"
        print(f"Claude Code SessionStart -> `agent-notes orient` {verb} in {settings_path}.")
        stop_path, stop_changed = _install_claude_stop_hook(repo_root)
        stop_verb = "wired" if stop_changed else "already present"
        print(f"Claude Code Stop -> `agent-notes outbox reconcile` {stop_verb} in {stop_path}.")

    print(
        "Next: `agent-notes install-skills --target claude` and `--target opencode` "
        "(global, once); opencode SessionStart wiring is pending (see plans/007)."
    )
    return EXIT_SUCCESS


def cmd_resolve(path: str | None, use_json: bool) -> int:
    from agent_notes.core.db import resolve_project as db_resolve_project

    target = os.path.abspath(path or ".")
    try:
        result = db_resolve_project(target)
    except ValueError as exc:
        from agent_notes.cli.common import emit_error

        return emit_error(
            "PROJECT_NOT_RESOLVED",
            str(exc),
            use_json=use_json,
            exit_code=EXIT_NOT_CONFIGURED,
        )
    if use_json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(result)
    return EXIT_SUCCESS


def cmd_doctor(use_json: bool, skip_embed: bool = False, check_embed: bool = False) -> int:
    if use_json:
        from agent_notes.scripts.doctor import run_json

        payload, code = run_json(check_embed=check_embed)
        print(json.dumps(payload, indent=2, default=str))
        return code

    from agent_notes.scripts.doctor import run as doctor_run

    return doctor_run(skip_embed=skip_embed, check_embed=check_embed)


def _configure_logging_stderr() -> None:
    """Route all structlog output to stderr (suite CLI contract v1 §1).

    regista (imported as a library) logs through structlog; unconfigured
    structlog prints to stdout, which contaminates every ``--json``
    consumer (WI-019's root cause). Configure explicitly at CLI entry so
    the fix doesn't depend on which regista version is installed.
    """
    import sys

    import structlog

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(20),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


def main() -> int:
    _configure_logging_stderr()
    parser = argparse.ArgumentParser(
        prog="agent-notes",
        description="agent-notes CLI — sync interface to breadcrumbs, memories, and search",
    )
    parser.add_argument("--version", action="store_true", help="Show version and exit")
    sub = parser.add_subparsers(dest="command")

    init_p = sub.add_parser(
        "init", help="Idempotently register a project from a path and wire lifecycle hooks"
    )
    init_p.add_argument("path", nargs="?", default=".")
    init_p.add_argument("--workspace", default=None, help="Workspace slug (default: default)")
    init_p.add_argument(
        "--no-hooks", action="store_true", help="Register only; do not wire the SessionStart hook"
    )

    resolve_p = sub.add_parser("resolve", help="Resolve a filesystem path to a registered project")
    resolve_p.add_argument("--path", default=".")
    resolve_p.add_argument("--json", action="store_true")

    doctor_p = sub.add_parser("doctor", help="Health check")
    doctor_p.add_argument("--json", action="store_true")
    doctor_p.add_argument(
        "--skip-embed",
        action="store_true",
        help="Deprecated; embedding check is now opt-in via --check-embed",
    )
    doctor_p.add_argument(
        "--check-embed",
        action="store_true",
        help="Run embedding model check (~270MB model load, ~30s on first run)",
    )

    register_breadcrumb_parsers(sub)
    register_work_item_parsers(sub)
    register_memory_parsers(sub)
    register_memory_provider_parsers(sub)
    register_link_parsers(sub)
    register_search_parsers(sub)
    register_vocabulary_parsers(sub)
    register_workspace_parsers(sub)
    register_changes_parsers(sub)
    register_codex_hook_parser(sub)
    register_events_parsers(sub)
    register_skills_parser(sub)
    register_harness_parser(sub)
    register_export_parsers(sub)
    register_orient_parser(sub)
    register_verify_parsers(sub)
    register_outbox_parsers(sub)
    register_projection_parsers(sub)
    register_admin_parsers(sub)

    args = parser.parse_args()

    if args.version:
        from importlib.metadata import version

        print(version("agent-notes"))
        return EXIT_SUCCESS

    if args.command == "init":
        return cmd_init(args.path, workspace_slug=args.workspace, install_hooks=not args.no_hooks)
    if args.command == "resolve":
        return cmd_resolve(args.path, args.json)
    if args.command == "doctor":
        return cmd_doctor(
            args.json,
            skip_embed=getattr(args, "skip_embed", False),
            check_embed=getattr(args, "check_embed", False),
        )

    func = getattr(args, "func", None)
    if func is not None:
        return func(args)

    parser.print_help()
    return EXIT_GENERIC


__all__ = ["main"]
