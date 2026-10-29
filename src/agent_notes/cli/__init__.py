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

from agent_notes.cli.breadcrumbs import register_breadcrumb_parsers
from agent_notes.cli.changes import register_changes_parsers
from agent_notes.cli.checkout import register_checkout_parsers
from agent_notes.cli.common import (
    EXIT_CONFLICT,
    EXIT_GENERIC,
    EXIT_NOT_CONFIGURED,
    EXIT_SUCCESS,
)
from agent_notes.cli.events import register_events_parsers
from agent_notes.cli.export import register_export_parsers
from agent_notes.cli.harness import register_harness_parser
from agent_notes.cli.invariants import register_invariants_parsers
from agent_notes.cli.links import register_link_parsers
from agent_notes.cli.memory import register_memory_parsers
from agent_notes.cli.msg import register_msg_parsers, register_session_parsers
from agent_notes.cli.orient import register_orient_parser
from agent_notes.cli.review import register_review_parsers
from agent_notes.cli.search import register_search_parsers
from agent_notes.cli.skills import register_skills_parser
from agent_notes.cli.verify import register_verify_parsers
from agent_notes.cli.vocabulary import register_vocabulary_parsers
from agent_notes.cli.who import register_who_parser
from agent_notes.cli.work_items import register_work_item_parsers
from agent_notes.cli.workspace import register_workspace_parsers
from agent_notes.codex_lifecycle import register_codex_hook_parser


def _repo_orient_hook(repo_root: str) -> str | None:
    """The per-repo ``.claude/settings.json`` path if it still runs ``agent-notes orient``.

    Older ``init`` runs wrote that SessionStart hook. Plan 025 Phase 4 moved
    orientation to the user-level hook ``install-harness claude`` installs,
    which skips its own orient summary while a per-repo one exists.
    """
    from pathlib import Path

    from agent_notes.hooks.claude import settings_run_orient

    settings_path = os.path.join(repo_root, ".claude", "settings.json")
    return settings_path if settings_run_orient(Path(settings_path)) else None


# The exact Stop commands earlier `init` runs wrote (77de21c, then 8530f29).
_LEGACY_RECONCILE_COMMANDS = frozenset(
    {"agent-notes outbox reconcile >/dev/null || true", "agent-notes outbox reconcile"}
)
_RECONCILE_PREFIX = "agent-notes outbox reconcile"


def _remove_claude_stop_reconcile_hook(repo_root: str) -> tuple[str, bool, list[str]]:
    """Remove the `agent-notes outbox reconcile` Stop hook earlier `init` runs
    wired into <repo>/.claude/settings.json (Plan 009 §12.4). Regista
    write-through is retired (Plan 025 D7), so there is no outbox to replay.

    Only a handler whose command is exactly one `init` wrote is removed. Any
    other command that mentions `outbox reconcile` is someone's own and is
    kept; those are returned so the caller can report them. Returns
    (path, changed, kept_commands).
    """
    settings_path = os.path.join(repo_root, ".claude", "settings.json")
    if not os.path.exists(settings_path):
        return settings_path, False, []
    try:
        settings = json.loads(open(settings_path, encoding="utf-8").read()) or {}
    except (json.JSONDecodeError, OSError):
        return settings_path, False, []
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    stop = hooks.get("Stop") if isinstance(hooks, dict) else None
    if not isinstance(stop, list):
        return settings_path, False, []
    changed = False
    left: list[str] = []
    kept_entries: list = []
    for entry in stop:
        if isinstance(entry, dict) and isinstance(entry.get("hooks"), list):
            kept = []
            for h in entry["hooks"]:
                command = h.get("command") if isinstance(h, dict) else None
                if isinstance(command, str):
                    if command in _LEGACY_RECONCILE_COMMANDS:
                        continue
                    if _RECONCILE_PREFIX in command:
                        left.append(command)
                kept.append(h)
            if len(kept) != len(entry["hooks"]):
                changed = True
                if not kept:
                    continue
                entry = {**entry, "hooks": kept}
        kept_entries.append(entry)
    if not changed:
        return settings_path, False, left
    if kept_entries:
        hooks["Stop"] = kept_entries
    else:
        hooks.pop("Stop")
    with open(settings_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(settings, indent=2) + "\n")
    return settings_path, True, left


def _cmd_retired_outbox(args: argparse.Namespace) -> int:
    """No-op for the retired ``agent-notes outbox ...`` (Plan 026 D1).

    Earlier ``init`` runs wired ``agent-notes outbox reconcile`` into
    per-repo Claude ``Stop`` hooks. An unknown subcommand exits 2, and a
    Claude Stop hook that exits 2 blocks the stop and feeds stderr back to the
    model, so removing the noun outright would trap those sessions. This
    hidden stub exits 0 instead. ``agent-notes init <repo>`` removes the hook;
    delete this stub once no installed hook calls it.
    """
    print(
        "agent-notes outbox: retired (Plan 026 D1); there is nothing to reconcile. "
        "Run `agent-notes init <repo>` to remove this Stop hook.",
        file=sys.stderr,
    )
    return EXIT_SUCCESS


def _register_retired_outbox(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("outbox", help=argparse.SUPPRESS, add_help=False)
    parser.add_argument("rest", nargs=argparse.REMAINDER)
    parser.set_defaults(func=_cmd_retired_outbox)


def _checkout_vm_id() -> str | None:
    from agent_notes.core.config import VmIdError, resolve_vm_id

    try:
        return resolve_vm_id()
    except VmIdError:
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

    if name == "global" or name.startswith("vm:"):
        print(
            f"Error: '{name}' is reserved for memory tiers (global / vm:<id>) and cannot "
            "name a project. Pass --slug to register this repo under another slug.",
            file=sys.stderr,
        )
        return EXIT_CONFLICT

    from agent_notes.core import checkouts
    from agent_notes.core.db import get_or_create_project, get_or_create_workspace

    ws_slug = workspace_slug or "default"
    vm_id = _checkout_vm_id()
    if vm_id is not None:
        owner = checkouts.find_project_by_checkout(repo_root, vm_id)
        if (
            owner
            and owner["resolved_via"] == "exact"
            and (owner["workspace"], owner["project"]) != (ws_slug, name)
        ):
            print(
                f"Error: {repo_root!r} is already registered on {vm_id} as a checkout of "
                f"project {owner['project']!r} (workspace {owner['workspace']}). Nothing "
                f"was changed. Pass --slug {owner['project']} to use that project, or "
                "`agent-notes checkout add` to move it deliberately.",
                file=sys.stderr,
            )
            return EXIT_CONFLICT

    ws_name = ws_slug.replace("-", " ").title()
    ws = get_or_create_workspace(ws_slug, ws_name)
    proj = get_or_create_project(
        ws.id, slug=name, name=name, repo_root=repo_root, relocate=relocate
    )
    attached = proj.repo_root != repo_root
    if vm_id is None and attached:
        print(
            f"Error: project '{name}' is already registered at {proj.repo_root!r}, not "
            f"{repo_root!r}, and this machine's VM ID cannot be derived to record "
            "a checkout. Nothing was changed. Set AGENT_NOTES_VM and re-run.",
            file=sys.stderr,
        )
        return EXIT_CONFLICT
    if vm_id is not None:
        # A slug collision is not an error: a second path for the same slug is
        # this VM's checkout (a Windows clone, a scratch clone). It never moves
        # repo_root, which keeps first-registration-wins (and --relocate) intact.
        checkouts.register_checkout(proj.id, vm_id, repo_root)
    else:
        print(
            "Warning: no stable VM ID (set AGENT_NOTES_VM); no per-VM checkout recorded.",
            file=sys.stderr,
        )
    if attached:
        print(
            f"Project '{name}' already exists (repo_root={proj.repo_root}); recorded "
            f"{repo_root} as the checkout for VM '{vm_id}' (workspace={ws_slug}). "
            "repo_root is unchanged; use --relocate to move it."
        )
        # The per-repo hook file is committed and shared by every checkout;
        # rewriting it with this machine's path would flip it back and forth.
        print("Hooks not wired for an attached checkout (the repo's hook file is shared).")
        return EXIT_SUCCESS
    print(f"Project '{name}' registered (workspace={ws_slug}, repo_root={repo_root}).")
    if vm_id is not None:
        print(f"Checkout recorded for VM '{vm_id}'.")

    if install_hooks:
        legacy = _repo_orient_hook(repo_root)
        if legacy:
            print(
                f"Note: {legacy} still runs `agent-notes orient` at SessionStart. The "
                "user-level hook from `agent-notes install-harness claude` now does this "
                "(and skips orient while the per-repo hook exists); you can remove it."
            )
        stop_path, stop_changed, stop_left = _remove_claude_stop_reconcile_hook(repo_root)
        if stop_changed:
            print(f"Removed the retired `agent-notes outbox reconcile` Stop hook from {stop_path}.")
        for command in stop_left:
            print(
                f"Note: {stop_path} has a Stop hook `{command}` that runs the retired "
                "`agent-notes outbox reconcile`. It isn't one `init` wrote, so it was left "
                "as is; remove it yourself if you no longer need it."
            )

    print(
        "Next (once per machine): `agent-notes install-harness all` wires skills, "
        "orientation and agent messages into Claude Code, OpenCode, Codex and agy."
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
            "Repoint an already-registered project's repo_root at this path. Without "
            "it, an existing slug keeps its root and this path is recorded as this "
            "VM's checkout; moving the root moves every work item and memory with it"
        ),
    )
    init_p.add_argument(
        "--slug",
        default=None,
        help="Project slug to register (default: the repo directory's basename)",
    )
    init_p.add_argument(
        "--no-hooks",
        action="store_true",
        help="Register only; do not touch the repo's .claude/settings.json",
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
    register_invariants_parsers(sub)
    _register_retired_outbox(sub)
    register_msg_parsers(sub)
    register_session_parsers(sub)
    register_who_parser(sub)
    register_review_parsers(sub)
    register_checkout_parsers(sub)

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
    """Run a subcommand, keeping configuration failures legible (WI-049).

    Actor, delegation and retired-setting failures are configuration gaps, not
    not-found or validation results from the business operation, so they are
    rendered as a named error envelope instead of a traceback. Everything else
    re-raises unchanged.
    """
    from agent_notes.core.actor import ActorConfigurationError, DelegationConfigurationError
    from agent_notes.core.config import RetiredSettingError

    try:
        return func(args)
    except (
        ActorConfigurationError,
        DelegationConfigurationError,
        RetiredSettingError,
    ) as exc:
        from agent_notes.cli.common import emit_error

        return emit_error(
            exc.code,
            str(exc),
            use_json=bool(getattr(args, "json", False)),
            detail={"error_type": type(exc).__name__},
            exit_code=EXIT_NOT_CONFIGURED,
        )


__all__ = ["main"]
