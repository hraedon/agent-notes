"""``agent-notes install-harness`` — repeatable harness wiring (Plan 017 WI-2.1).

One idempotent command that installs the skills *and* wires the harness config
(env vars the agent face needs, plus the opencode plugin transforms) for a named
harness — replacing the hand-edited ``~/.claude/settings.json`` / opencode config
that preceded it. Re-runnable; reports what it changed; ``--dry-run`` prints the
planned actions; ``uninstall-harness`` reverses them.

Implements the suite ``install-harness`` contract
(``agent-suite/docs/install-harness-contract.md``). ``install-harness`` is a
*superset* of ``install-skills``: it runs the skill install (reusing the existing,
tested helpers in :mod:`agent_notes.cli.skills`) and adds the config-merge layer.

Env-var wiring per harness (decision: respect each harness's schema):

* **claude** — merge the suite + tool env vars into ``~/.claude/settings.json``
  under the ``env`` key (the schema-supported, working mechanism Claude Code
  injects into spawned processes).
* **opencode** — ``opencode.json`` has no top-level ``env`` key
  (``additionalProperties: false``), so env vars are written into agent-notes'
  own config file (``~/.config/agent-notes/config.json``) — the existing
  harness-independent fallback that :mod:`agent_notes.core.config` already reads
  (Plan 012 WI-1). The opencode plugin path is registered in
  ``opencode.json["plugin"]`` (schema-supported); the two transform hooks
  (``experimental.chat.system.transform`` / ``experimental.session.compacting``)
  ship *inside* the plugin, so registering the path wires them.

Idempotency + uninstall: a sidecar manifest (``.agent-notes-harness.json`` next
to each harness config) records exactly what install-harness wrote (env keys,
skill names, plugin bool). ``--uninstall`` removes only those entries — user-
authored config and pre-existing secrets are never clobbered (contract §3 rules
2–4).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from agent_notes.cli.common import EXIT_GENERIC, EXIT_SUCCESS
from agent_notes.cli.skills import (
    _discover_skills,
    _install_one,
    _repo_skills_root,
    _to_opencode_body,
)

TOOL_NAME = "agent-notes"
MANIFEST_FILENAME = ".agent-notes-harness.json"

# Contract exit codes (install-harness-contract.md §7): 0 success/no-op,
# 1 failure, 2 dry-run. Defined locally so the 2 = dry-run semantics are not
# conflated with the EXIT_NOT_FOUND=2 used by project-resolution commands.
EXIT_DRY_RUN = 2

_HARNESS_TARGETS = ("claude", "opencode")

# Canonical suite env vars to propagate, each with its legacy alias (Plan 017
# WI-1.1). The canonical name is what gets written into the harness config so
# the resolver reads it without a deprecation warning.
_SUITE_ENV_VARS: list[tuple[str, str]] = [
    ("REGISTA_DSN", "AGENT_NOTES_REGISTA_DSN"),
    ("REGISTA_KEY_PATH", "AGENT_NOTES_REGISTA_HMAC_KEY_PATH"),
    ("REGISTA_REQUIRE_SSL", "AGENT_NOTES_REGISTA_REQUIRE_SSL"),
]
# Tool-specific vars (not suite-shared) — keep their AGENT_NOTES_* names.
_TOOL_ENV_VARS: list[str] = [
    "AGENT_NOTES_DSN",
    "AGENT_NOTES_REGISTA_WRITES",
    "AGENT_NOTES_REGISTA_PROJECT",
]
_PRINCIPAL_ENV = "AGENT_NOTES_PRINCIPAL_ID"

# opencode config-file field map: env-var name -> (section, key) where section
# is "regista" or None (top-level). Boolean fields are coerced from the env
# string so the config-file value is a real bool, not "1".
_OPENCODE_FIELD_MAP: dict[str, tuple[str | None, str, bool]] = {
    "REGISTA_DSN": ("regista", "dsn", False),
    "REGISTA_KEY_PATH": ("regista", "hmac_key_path", False),
    "REGISTA_REQUIRE_SSL": ("regista", "require_ssl", True),
    "AGENT_NOTES_REGISTA_WRITES": ("regista", "writes_enabled", True),
    "AGENT_NOTES_DSN": (None, "dsn", False),
    # AGENT_NOTES_REGISTA_PROJECT has no config-file field (env-only).
}

_BOOL_TRUTHY = {"1", "true", "yes"}


def _act(kind: str, keys: list[str], detail: str, status: str = "") -> dict:
    """Build a contract action dict (path is filled in by the caller)."""
    a: dict = {"kind": kind, "path": "", "keys": keys, "detail": detail}
    if status:
        a["status"] = status
    return a


# ---------------------------------------------------------------------------
# path helpers
# ---------------------------------------------------------------------------


def _plugin_path() -> Path:
    """Resolve the opencode plugin path (repo-relative, absolute)."""
    return _repo_skills_root().parent / "integrations" / "opencode" / "index.js"


def _harness_paths(harness: str, home: Path | None = None) -> dict[str, Path]:
    """Return the config/skill/manifest paths for a harness target."""
    home = home or Path.home()
    if harness == "claude":
        base = home / ".claude"
        return {
            "skills_dest": base / "skills",
            "config": base / "settings.json",
            "manifest": base / MANIFEST_FILENAME,
            "agent_config": home / ".config" / "agent-notes" / "config.json",
        }
    # opencode
    base = home / ".config" / "opencode"
    return {
        "skills_dest": base / "command",
        "config": base / "opencode.json",
        "manifest": base / MANIFEST_FILENAME,
        "agent_config": home / ".config" / "agent-notes" / "config.json",
    }


# ---------------------------------------------------------------------------
# json helpers
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# env resolution
# ---------------------------------------------------------------------------


def _resolve_harness_env(user: str | None) -> dict[str, str]:
    """Build the ordered env-var -> value map to propagate into the harness.

    Suite vars resolve canonical-first, legacy-alias fallback (the value is
    written under the canonical name so the resolver reads it warning-free).
    Tool-specific vars are taken verbatim. ``--user`` overrides the principal.
    Empty values are dropped (an unset var is not propagated).
    """
    env: dict[str, str] = {}
    for canonical, legacy in _SUITE_ENV_VARS:
        val = os.environ.get(canonical) or os.environ.get(legacy)
        if val:
            env[canonical] = val
    for name in _TOOL_ENV_VARS:
        val = os.environ.get(name)
        if val:
            env[name] = val
    if user:
        env[_PRINCIPAL_ENV] = user
    return env


def _env_coerce(name: str, value: str) -> object:
    """Coerce a string env value to its config-file type (bool for flags)."""
    spec = _OPENCODE_FIELD_MAP.get(name)
    if spec and spec[2]:
        return value.lower() in _BOOL_TRUTHY
    return value


# ---------------------------------------------------------------------------
# skill install (reuses skills.py helpers)
# ---------------------------------------------------------------------------


def _install_skills(
    target: str, src_root: Path, dest_root: Path, dry_run: bool
) -> tuple[list[dict], list[str]]:
    """Install skills for a harness target. Returns (action_dicts, skill_names).

    Reuses the tested discovery + per-file install logic from
    :mod:`agent_notes.cli.skills` so idempotency semantics stay identical to
    ``install-skills`` (Plan 004 §9 Q4).
    """
    skills = _discover_skills(src_root)
    actions: list[dict] = []
    names: list[str] = []
    for src in skills:
        name = src.parent.name
        names.append(name)
        src_content = src.read_text(encoding="utf-8")
        if target == "claude":
            dest = dest_root / name / "SKILL.md"
            payload = src_content
        else:
            dest = dest_root / f"{name}.md"
            payload = _to_opencode_body(src_content)
        status = _install_one(payload, dest, dry_run)
        a = _act("create_file", [], f"install skill ({status})", status=status)
        a["path"] = str(dest)
        actions.append(a)
    return actions, names


# ---------------------------------------------------------------------------
# env wiring — claude (settings.json env block)
# ---------------------------------------------------------------------------


def _wire_env_claude(
    settings: dict, env_values: dict[str, str], dry_run: bool
) -> tuple[list[dict], list[str], list[str], list[str]]:
    """Merge env vars into settings.json['env'].

    Returns (actions, newly_written, matching, warnings).

    * ``newly_written`` — keys actually written this run (drives ``no_op``).
    * ``matching`` — keys already present and equal to our value. Whether these
      are *managed* (tracked for uninstall) is decided by the caller using the
      previous manifest: a matching key is managed only if a prior install wrote
      it, so a user's pre-existing matching value is never clobbered on uninstall.

    No-clobber (contract §3 rule 3): a pre-existing key with a *different* value
    is kept and warned, never overwritten, and never managed.
    """
    env_block = settings.setdefault("env", {})
    if not isinstance(env_block, dict):
        env_block = {}
        settings["env"] = env_block
    actions: list[dict] = []
    newly: list[str] = []
    matching: list[str] = []
    warns: list[str] = []
    for name, value in env_values.items():
        key_path = f"env.{name}"
        if name in env_block:
            if env_block[name] == value:
                actions.append(_act("merge_json", [key_path], "already set (unchanged)"))
                matching.append(name)
            else:
                warns.append(f"{key_path}: existing value differs; kept existing (no clobber)")
                actions.append(_act("merge_json", [key_path], "kept existing (no clobber)"))
        else:
            if not dry_run:
                env_block[name] = value
            newly.append(name)
            actions.append(_act("merge_json", [key_path], "set suite env var"))
    return actions, newly, matching, warns


# ---------------------------------------------------------------------------
# env wiring — opencode (agent-notes config file)
# ---------------------------------------------------------------------------


def _wire_env_opencode(
    agent_cfg: dict, env_values: dict[str, str], dry_run: bool
) -> tuple[list[dict], list[str], list[str], list[str]]:
    """Merge env vars into the agent-notes config file (the opencode fallback).

    Returns (actions, newly_written, matching, warnings). ``matching`` are dotted
    config-file paths already present and equal to our value; the caller decides
    management using the previous manifest.
    """
    regista = agent_cfg.setdefault("regista", {})
    if not isinstance(regista, dict):
        regista = {}
        agent_cfg["regista"] = regista
    actions: list[dict] = []
    newly: list[str] = []
    matching: list[str] = []
    warns: list[str] = []
    for name, raw_value in env_values.items():
        spec = _OPENCODE_FIELD_MAP.get(name)
        if spec is None:
            continue
        section, key, _is_bool = spec
        value = _env_coerce(name, raw_value)
        target = agent_cfg if section is None else regista
        key_path = f"{section}.{key}" if section else key
        if key in target:
            if target[key] == value:
                actions.append(_act("merge_json", [key_path], "already set (unchanged)"))
                matching.append(key_path)
            else:
                warns.append(f"{key_path}: existing value differs; kept existing (no clobber)")
                actions.append(_act("merge_json", [key_path], "kept existing (no clobber)"))
        else:
            if not dry_run:
                target[key] = value
            newly.append(key_path)
            actions.append(_act("merge_json", [key_path], "set suite config value"))
    return actions, newly, matching, warns


# ---------------------------------------------------------------------------
# plugin wiring (opencode only)
# ---------------------------------------------------------------------------


def _wire_plugin(
    opencode_cfg: dict, plugin_path: Path, dry_run: bool
) -> tuple[list[dict], bool, bool]:
    """Register the opencode plugin path. Returns (actions, registered_now, already_present)."""
    plugins = opencode_cfg.setdefault("plugin", [])
    if not isinstance(plugins, list):
        plugins = []
        opencode_cfg["plugin"] = plugins
    ppath = str(plugin_path)
    already = any(p == ppath or (isinstance(p, list) and p and p[0] == ppath) for p in plugins)
    if already:
        a = _act("merge_json", ["plugin"], "plugin already registered (unchanged)")
        return ([a], False, True)
    if not dry_run:
        plugins.append(ppath)
    detail = "register opencode plugin (system.transform + session.compacting)"
    return ([_act("merge_json", ["plugin"], detail)], True, False)


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


def _read_manifest(path: Path) -> dict:
    return _load_json(path)


def _write_manifest(path: Path, data: dict) -> None:
    _save_json(path, data)


def _remove_manifest(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# install / uninstall one harness
# ---------------------------------------------------------------------------


def _install_harness_one(
    harness: str,
    dry_run: bool,
    user: str | None,
    source: Path | None,
    dest: Path | None,
    home: Path | None = None,
) -> tuple[dict, list[str]]:
    """Install wiring for one harness. Returns (result_dict, warnings)."""
    paths = _harness_paths(harness, home=home)
    src_root = source or _repo_skills_root()
    skills_dest = dest or paths["skills_dest"]
    config_path = paths["config"]
    agent_config_path = paths["agent_config"]
    plugin_path = _plugin_path()

    env_values = _resolve_harness_env(user)
    warns: list[str] = []
    if not env_values:
        warns.append("no suite env vars found in process env; skills/plugin wired without env")

    # Read the prior manifest first so we can preserve previously-managed keys
    # that are still present but no longer in env_values (user unset a var
    # between installs) — without this, re-install would drop them from the
    # manifest and uninstall would leak them (review B1).
    prev = _read_manifest(paths["manifest"]) if not dry_run else {}
    prev_env_keys = set(prev.get("env_keys", []))
    prev_plugin = bool(prev.get("plugin", False))
    prev_skills = set(prev.get("skills", []))

    # --- skills ---
    skill_actions, skill_names = _install_skills(harness, src_root, skills_dest, dry_run)
    # Preserve previously-manifested skills still on disk but no longer in the
    # repo so uninstall can remove the orphaned files (review B1).
    for name in prev_skills:
        if name in skill_names:
            continue
        sf = skills_dest / name / "SKILL.md" if harness == "claude" else skills_dest / f"{name}.md"
        if sf.is_file():
            skill_names.append(name)

    if user and harness == "opencode":
        warns.append(
            "--user has no opencode config-file field yet (principal_id resolves "
            "from git config); principal overlay skipped for opencode"
        )

    # --- config + env + plugin ---
    config = _load_json(config_path)
    config_actions: list[dict] = []
    env_managed: list[str] = []
    env_newly: list[str] = []
    plugin_newly = False
    plugin_managed = False

    if harness == "claude":
        ca, newly, matching, w = _wire_env_claude(config, env_values, dry_run)
        for a in ca:
            a["path"] = str(config_path)
        config_actions += ca
        env_newly = newly
        env_managed = newly + [k for k in matching if k in prev_env_keys]
        # Preserve previously-managed env keys still in settings.json but no
        # longer propagated (var unset between installs) — review B1.
        env_block = config.get("env", {})
        if isinstance(env_block, dict):
            for k in prev_env_keys:
                if k not in env_managed and k in env_block:
                    env_managed.append(k)
        warns += w
        if not dry_run and config:
            _save_json(config_path, config)
    else:  # opencode
        # env -> agent-notes config file (the harness-independent fallback)
        agent_cfg = _load_json(agent_config_path)
        oa, onewly, omatching, ow = _wire_env_opencode(agent_cfg, env_values, dry_run)
        for a in oa:
            a["path"] = str(agent_config_path)
        config_actions += oa
        env_newly = onewly
        env_managed = onewly + [k for k in omatching if k in prev_env_keys]
        # Preserve previously-managed config keys still present but no longer
        # propagated — review B1. Keys are dotted paths (regista.<key>) or top-level.
        for kp in prev_env_keys:
            if kp in env_managed:
                continue
            parts = kp.split(".", 1)
            if len(parts) == 2:
                tgt = agent_cfg.get("regista", {})
                if isinstance(tgt, dict) and parts[1] in tgt:
                    env_managed.append(kp)
            elif kp in agent_cfg:
                env_managed.append(kp)
        warns += ow
        if not dry_run and agent_cfg:
            _save_json(agent_config_path, agent_cfg)
        # plugin -> opencode.json (schema-supported 'plugin' array)
        pa, registered, already = _wire_plugin(config, plugin_path, dry_run)
        for a in pa:
            a["path"] = str(config_path)
        config_actions += pa
        plugin_newly = registered
        plugin_managed = registered or (already and prev_plugin)
        if not dry_run and config:
            _save_json(config_path, config)

    all_actions = skill_actions + config_actions
    no_op = (
        all(
            a.get("detail", "").endswith("(unchanged)") or a.get("status") == "unchanged"
            for a in all_actions
        )
        and not env_newly
        and not plugin_newly
    )

    # --- manifest: record what install-harness manages (survives re-installs) ---
    if not dry_run:
        try:
            from importlib.metadata import version

            ver = version("agent-notes")
        except Exception:
            ver = "unknown"

        _write_manifest(
            paths["manifest"],
            {
                "tool": TOOL_NAME,
                "version": ver,
                "harness": harness,
                "env_keys": env_managed,
                "skills": skill_names,
                "plugin": plugin_managed,
            },
        )

    result = {
        "tool": TOOL_NAME,
        "harness": harness,
        "user": user,
        "actions": all_actions,
        "no_op": no_op,
    }
    return result, warns


def _uninstall_one(harness: str, home: Path | None = None) -> tuple[dict, list[str]]:
    """Reverse a prior install-harness for one harness. Returns (result_dict, warnings)."""
    paths = _harness_paths(harness, home=home)
    manifest = _read_manifest(paths["manifest"])
    warns: list[str] = []
    actions: list[dict] = []

    if not manifest:
        # No manifest → nothing we recorded writing. Clean-profile no-op (contract §3 rule 4).
        return (
            {
                "tool": TOOL_NAME,
                "harness": harness,
                "user": None,
                "actions": [],
                "no_op": True,
                "uninstalled": True,
            },
            [],
        )

    # --- remove env keys ---
    env_keys: list[str] = manifest.get("env_keys", [])
    if harness == "claude":
        settings = _load_json(paths["config"])
        env_block = settings.get("env", {})
        if isinstance(env_block, dict):
            for key in env_keys:
                if key in env_block:
                    env_block.pop(key, None)
                    a = _act("remove_key", [f"env.{key}"], "removed env var")
                    a["path"] = str(paths["config"])
                    actions.append(a)
        if actions:
            _save_json(paths["config"], settings)
    else:  # opencode: env lives in the agent-notes config file
        agent_cfg = _load_json(paths["agent_config"])
        for key_path in env_keys:
            parts = key_path.split(".")
            if len(parts) == 2:  # regista.<key>
                target = agent_cfg.get("regista", {})
                if isinstance(target, dict) and parts[1] in target:
                    target.pop(parts[1], None)
                    a = _act("remove_key", [key_path], "removed config value")
                    a["path"] = str(paths["agent_config"])
                    actions.append(a)
            else:  # top-level key
                if key_path in agent_cfg:
                    agent_cfg.pop(key_path, None)
                    a = _act("remove_key", [key_path], "removed config value")
                    a["path"] = str(paths["agent_config"])
                    actions.append(a)
        if actions:
            _save_json(paths["agent_config"], agent_cfg)

    # --- remove plugin (opencode only) ---
    if manifest.get("plugin"):
        oc_cfg = _load_json(paths["config"])
        plugins = oc_cfg.get("plugin", [])
        if isinstance(plugins, list):
            ppath = str(_plugin_path())
            before = len(plugins)
            plugins[:] = [
                p
                for p in plugins
                if not (p == ppath or (isinstance(p, list) and p and p[0] == ppath))
            ]
            if len(plugins) != before:
                a = _act("remove_key", ["plugin"], "removed plugin entry")
                a["path"] = str(paths["config"])
                actions.append(a)
                _save_json(paths["config"], oc_cfg)

    # --- remove skills ---
    skill_names: list[str] = manifest.get("skills", [])
    skills_dest = paths["skills_dest"]
    for name in skill_names:
        if harness == "claude":
            skill_file = skills_dest / name / "SKILL.md"
        else:
            skill_file = skills_dest / f"{name}.md"
        if skill_file.is_file():
            skill_file.unlink()
            a = _act("remove_file", [], "removed skill")
            a["path"] = str(skill_file)
            actions.append(a)
            # Clean up empty parent dir (claude lays out <name>/SKILL.md).
            parent = skill_file.parent
            if harness == "claude" and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()

    _remove_manifest(paths["manifest"])

    result = {
        "tool": TOOL_NAME,
        "harness": harness,
        "user": None,
        "actions": actions,
        "no_op": not actions,
        "uninstalled": True,
    }
    return result, warns


# ---------------------------------------------------------------------------
# command
# ---------------------------------------------------------------------------


def _targets_for(harness: str) -> list[str]:
    if harness == "all":
        return list(_HARNESS_TARGETS)
    if harness in _HARNESS_TARGETS:
        return [harness]
    return []


def cmd_install_harness(args: argparse.Namespace) -> int:
    harness: str = args.harness
    dry_run = bool(getattr(args, "dry_run", False))
    uninstall = bool(getattr(args, "uninstall", False))
    user = getattr(args, "user", None)
    use_json = bool(getattr(args, "json", False))
    source = Path(args.source) if getattr(args, "source", None) else None
    dest = Path(args.dest) if getattr(args, "dest", None) else None
    home = Path(args.home) if getattr(args, "home", None) else None

    targets = _targets_for(harness)
    if not targets:
        print(f"Unknown harness: {harness!r} (expected: claude|opencode|all)", file=sys.stderr)
        return EXIT_GENERIC

    all_warns: list[str] = []
    results: list[dict] = []
    for tgt in targets:
        if uninstall:
            res, warns = _uninstall_one(tgt, home=home)
        else:
            res, warns = _install_harness_one(tgt, dry_run, user, source, dest, home=home)
        results.append(res)
        all_warns += warns

    exit_code = EXIT_DRY_RUN if dry_run else EXIT_SUCCESS

    if use_json or dry_run:
        # Contract §4: JSON to stdout.
        if harness == "all":
            payload = {"tool": TOOL_NAME, "harness": "all", "results": results}
        else:
            payload = results[0]
        print(json.dumps(payload, indent=2, default=str))
    else:
        # Human-readable summary.
        for res in results:
            verb = "Uninstalled" if uninstall else "Installed"
            if res["no_op"]:
                print(f"[{res['harness']}] already wired (no-op).")
            else:
                n = len(res["actions"])
                print(f"[{res['harness']}] {verb}: {n} action(s).")
                for a in res["actions"]:
                    print(f"  {a['kind']}: {a.get('path', '')} — {a.get('detail', '')}")
        print(f"exit {exit_code}")

    for w in all_warns:
        print(f"warning: {w}", file=sys.stderr)

    return exit_code


def register_harness_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "install-harness",
        help="Install skills + wire harness config (env/plugin) for a harness",
    )
    # harness is validated in cmd_install_harness (not via choices=) so an
    # unknown value exits 1 (failure) per the contract, not argparse's 2.
    p.add_argument("harness", help="claude | opencode | all")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned actions; change nothing (exit 2)",
    )
    p.add_argument("--uninstall", action="store_true", help="Reverse a prior install-harness")
    p.add_argument("--user", default=None, help="Per-user principal_id overlay")
    p.add_argument("--json", action="store_true", help="Emit JSON (always on for --dry-run)")
    # Hidden test flags (mirrors install-skills).
    p.add_argument("--source", default=None, help=argparse.SUPPRESS)
    p.add_argument("--dest", default=None, help=argparse.SUPPRESS)
    p.add_argument("--home", default=None, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_install_harness)
