"""agent-notes CLI (Plan 004 Phase 9a).

Noun/verb argparse tree: `agent-notes breadcrumb file`, `agent-notes memory add`, etc.
All commands accept `--path` and resolve via `core.db.resolve_project`. With no
`--path` / `--workspace` / `--project`, the project is discovered from the cwd
(`core.project_discovery`); an unregistered directory stays unresolved.
`--json` produces machine-parseable output. Stable exit codes per decision 52.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Callable

from regista._errors import ErrorCode, RegistaError

from agent_notes.cli.admin import register_admin_parsers
from agent_notes.cli.breadcrumbs import register_breadcrumb_parsers
from agent_notes.cli.changes import register_changes_parsers
from agent_notes.cli.common import (
    EXIT_CONFLICT,
    EXIT_GENERIC,
    EXIT_NOT_CONFIGURED,
    EXIT_SUCCESS,
)
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


def _regista_schema_missing(slug: str) -> str | None:
    """Return the regista project (schema) name for *slug* if it is missing (WI-049).

    ``init`` used to register a project unconditionally and report success —
    leaving the workspace permanently broken when the regista schema for the
    (basename-derived) slug did not exist: every subsequent command died with
    a raw ``DB_NOT_FOUND`` traceback and there was no CLI way back. Verify the
    schema *before* persisting anything, so a broken registration is refused
    rather than created.

    Only a definitive ``DB_NOT_FOUND`` (or a slug that cannot even name a
    regista schema) counts as missing. When regista writes are disabled this
    is a no-op, and any other failure (regista unreachable, ...) warns and
    lets ``init`` proceed — the degrade path never required a live regista.
    """
    from agent_notes.core.config import regista_config

    cfg = regista_config()
    if not cfg.enabled:
        return None

    from agent_notes.core import face_factory

    try:
        regista_name = face_factory.regista_project_name(slug)
    except ValueError as exc:
        print(f"Error: slug {slug!r} cannot name a regista schema: {exc}", file=sys.stderr)
        return slug

    previous = face_factory.current_project()
    face_factory.set_current_project(regista_name)
    try:
        # Building the face connects and runs regista's ensure_schema; a
        # missing schema surfaces here instead of on the first write.
        face_factory.get_face()
    except RegistaError as exc:
        if exc.code == ErrorCode.DB_NOT_FOUND:
            return regista_name
        print(
            f"Warning: could not verify regista schema {regista_name!r} "
            f"({exc}); proceeding without verification.",
            file=sys.stderr,
        )
    except Exception as exc:
        print(
            f"Warning: could not verify regista schema {regista_name!r} "
            f"({exc}); proceeding without verification.",
            file=sys.stderr,
        )
    finally:
        face_factory.set_current_project(previous)
    return None


def cmd_init(
    path: str | None,
    workspace_slug: str | None = None,
    install_hooks: bool = True,
    slug: str | None = None,
    relocate: bool = False,
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
    name = slug or os.path.basename(repo_root)

    missing = _regista_schema_missing(name)
    if missing is not None:
        print(
            f"Error: refusing to register project '{name}': its regista project "
            f"schema '{missing}' does not exist, so the registration could never "
            "be used. Nothing was registered.",
            file=sys.stderr,
        )
        print(
            f"  Provision the schema first: regista provision --project {missing}",
            file=sys.stderr,
        )
        if slug is None:
            print(
                "  (The slug was derived from the directory basename; pass "
                "--slug to register under a different, provisioned slug.)",
                file=sys.stderr,
            )
        return EXIT_NOT_CONFIGURED

    from agent_notes.core.db import get_or_create_project, get_or_create_workspace

    ws_slug = workspace_slug or "default"
    ws_name = ws_slug.replace("-", " ").title()
    ws = get_or_create_workspace(ws_slug, ws_name)
    proj = get_or_create_project(
        ws.id, slug=name, name=name, repo_root=repo_root, relocate=relocate
    )
    if proj.repo_root != repo_root:
        # Slug collision: another checkout of the same-named repo already owns
        # this slug. Repointing would drag every work item and memory to the
        # new path, so refuse and say so — silently succeeding here is what
        # stranded usage-dashboard behind a deleted /tmp clone.
        print(
            f"Error: project '{name}' is already registered at {proj.repo_root!r}, "
            f"not {repo_root!r}. Nothing was changed.",
            file=sys.stderr,
        )
        print(
            "  Two checkouts of the same repo share a slug (it defaults to the "
            "directory basename). If this checkout is scratch (a /tmp clone, a "
            "worktree, a CI checkout), register it under a different --slug or "
            "leave it unregistered.",
            file=sys.stderr,
        )
        print(
            f"  If the project genuinely moved, re-run with: "
            f"agent-notes init {repo_root} --relocate",
            file=sys.stderr,
        )
        return EXIT_CONFLICT
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


def _resolved_version() -> str:
    """Return the installed distribution version for this package (WI-048).

    The import package is ``agent_notes`` but the published distribution is
    ``agent-notes-hraedon``, so ``version("agent-notes")`` raised an unhandled
    ``PackageNotFoundError`` on every wheel/PyPI install — the first command the
    deployment guide tells the operator to run ended in a traceback. Resolve the
    distribution *from the import package* via ``packages_distributions()`` so a
    future distribution rename cannot break this again, with the current
    distribution name as an explicit fallback, and degrade to a legible string
    (never a traceback) when no metadata is installed at all.
    """
    from importlib.metadata import (
        PackageNotFoundError,
        packages_distributions,
        version,
    )

    top_level = __package__.split(".", 1)[0]  # "agent_notes"
    candidates = list(packages_distributions().get(top_level) or [])
    if "agent-notes-hraedon" not in candidates:
        candidates.append("agent-notes-hraedon")
    for dist_name in candidates:
        try:
            return version(dist_name)
        except PackageNotFoundError:
            continue
    return f"unknown (no installed distribution metadata for {top_level})"


def _cmd_contract() -> int:
    """Emit the committed CLI contract manifest (contract §6 discovery).

    Loads the manifest from package data (``agent_notes/cli-manifest.json``,
    included in the wheel via hatch ``force-include``). In a source checkout
    (editable install) the package resource is absent, so a fallback reads
    from the repo-relative ``data/cli-manifest.json``. Prints the manifest as
    a pure JSON document to stdout. Exit 0 on success; if the manifest is
    missing from both locations (a packaging error), emits the common error
    envelope and exits 1.
    """
    from importlib.resources import files
    from pathlib import Path

    from agent_notes.cli.common import EXIT_GENERIC, emit_error

    manifest_text: str | None = None

    # Primary: package data (wheel install).
    try:
        resource = files("agent_notes").joinpath("cli-manifest.json")
        manifest_text = resource.read_text(encoding="utf-8")
    except (OSError, TypeError):
        pass

    # Fallback: source checkout (editable install) — the manifest lives at
    # <repo>/data/cli-manifest.json, three levels up from this file.
    if manifest_text is None:
        try:
            repo_root = Path(__file__).resolve().parent.parents[2]
            fallback_path = repo_root / "data" / "cli-manifest.json"
            manifest_text = fallback_path.read_text(encoding="utf-8")
        except OSError:
            pass

    if manifest_text is None:
        return emit_error(
            "MANIFEST_UNAVAILABLE",
            "cannot load cli-manifest.json from package data or source checkout",
            use_json=True,
            exit_code=EXIT_GENERIC,
        )

    try:
        manifest = json.loads(manifest_text)
    except json.JSONDecodeError as exc:
        return emit_error(
            "MANIFEST_UNAVAILABLE",
            f"cli-manifest.json is not valid JSON: {exc}",
            use_json=True,
            exit_code=EXIT_GENERIC,
        )

    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return EXIT_SUCCESS


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
        "--relocate",
        action="store_true",
        help=(
            "Repoint an already-registered project at this path. Required when "
            "the slug is taken, because moving the root moves every work item "
            "and memory with it"
        ),
    )
    init_p.add_argument(
        "--slug",
        default=None,
        help=(
            "Project slug to register (default: the repo directory's basename). "
            "With regista enabled the slug must map to a provisioned schema"
        ),
    )
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

    contract_p = sub.add_parser(
        "contract",
        help="Emit the CLI contract manifest (contract §6 discovery)",
    )
    contract_p.add_argument(
        "--json",
        action="store_true",
        default=True,
        help="Emit as JSON (default; the manifest is always JSON)",
    )

    args = parser.parse_args()

    if args.version:
        print(_resolved_version())
        return EXIT_SUCCESS

    if args.command == "init":
        return cmd_init(
            args.path,
            workspace_slug=args.workspace,
            install_hooks=not args.no_hooks,
            slug=args.slug,
            relocate=args.relocate,
        )
    if args.command == "resolve":
        return cmd_resolve(args.path, args.json)
    if args.command == "doctor":
        return cmd_doctor(
            args.json,
            skip_embed=getattr(args, "skip_embed", False),
            check_embed=getattr(args, "check_embed", False),
        )
    if args.command == "contract":
        return _cmd_contract()

    func = getattr(args, "func", None)
    if func is not None:
        return _dispatch(func, args)

    parser.print_help()
    return EXIT_GENERIC


def _dispatch(func: Callable[[argparse.Namespace], int], args: argparse.Namespace) -> int:
    """Run a subcommand, keeping the most likely first-run failure legible (WI-049).

    A ``DB_NOT_FOUND`` raised while constructing the regista face means the
    resolved project's schema was never provisioned — a registration/bootstrap
    gap, not a bug — and it used to escape as a raw traceback naming an
    internal API (``Regista.create_project()``). Convert it to the common
    error envelope with the actual remediation. Everything else re-raises
    unchanged.

    ``UndeclaredLineageError`` (WI-062) gets the same treatment and for the same
    reason: it is a configuration gap with a one-line remedy, and it must be the
    *last* thing on the operator's terminal. It is caught here rather than in
    each subcommand because the subcommands' ``except ValueError`` handlers
    would relabel it ``NOT_FOUND`` / ``VALIDATION_FAILED`` — which is how the
    original failure stayed invisible.
    """
    from agent_notes.core.actor import UndeclaredLineageError

    try:
        return func(args)
    except UndeclaredLineageError as exc:
        from agent_notes.cli.common import emit_error

        return emit_error(
            exc.code,
            str(exc),
            use_json=bool(getattr(args, "json", False)),
            detail={"actor_id": exc.actor_id, "operation": exc.operation or ""},
            exit_code=EXIT_NOT_CONFIGURED,
        )
    except RegistaError as exc:
        if exc.code != ErrorCode.DB_NOT_FOUND:
            raise
        from agent_notes.cli.common import emit_error
        from agent_notes.core import face_factory
        from agent_notes.core.config import regista_config

        target = face_factory.current_project() or regista_config().project
        return emit_error(
            "DB_NOT_FOUND",
            f"The regista project schema '{target}' does not exist: the project "
            "is registered in agent-notes but was never provisioned in regista.",
            use_json=bool(getattr(args, "json", False)),
            detail=(
                f"Provision it with `regista provision --project {target}`, then "
                f"re-run this command. (regista said: {exc.message})"
            ),
            exit_code=EXIT_NOT_CONFIGURED,
        )


__all__ = ["main"]
