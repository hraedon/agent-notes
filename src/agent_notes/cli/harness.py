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
* **hermes** — ``~/.hermes/.env`` is a ``KEY=VALUE`` file (not JSON). Env vars
  are written between sentinel comments (``# BEGIN agent-notes-harness-managed``
  / ``# END agent-notes-harness-managed``). Idempotent: if sentinels exist the
  managed block is replaced. No-clobber: keys outside the block with a different
  value are warned and skipped. Skills use the same ``SKILL.md`` directory layout
  as claude. No plugin or agents for hermes.

Idempotency + uninstall: a sidecar manifest (``.agent-notes-harness.json`` next
to each harness config) records exactly what install-harness wrote (env keys,
skill names, plugin bool, and Codex hook-group hashes). ``--uninstall`` removes
only those entries — user-authored config, Cairn hooks, and pre-existing secrets
are never clobbered (contract §3 rules 2–4).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from agent_notes.cli.common import EXIT_GENERIC, EXIT_SUCCESS
from agent_notes.cli.skills import (
    _discover_skills,
    _repo_skills_root,
    _to_opencode_body,
)
from agent_notes.core.config import config_path as _config_path

TOOL_NAME = "agent-notes"
MANIFEST_FILENAME = ".agent-notes-harness.json"

# Opencode subagent definitions to install from the repo tree. These live in
# `.opencode/agents/` and are copied into `~/.config/opencode/agents/` so a
# primary agent in an agent-notes context can invoke adversarial reviewers via
# the Task tool. Kept separate from skills because opencode agents need a
# distinct permission model (read-only review + limited `agent-notes` bash).
_OPENCODE_AGENT_FILES: tuple[str, ...] = (
    "adversarial-reviewer-glm.md",
    "adversarial-reviewer-kimi.md",
    "adversarial-reviewer-minimax-m3.md",
    "adversarial-reviewer-nemotron-3-ultra.md",
    "glm.md",
    "kimi.md",
)

# Contract exit codes (install-harness-contract.md): 0 success/no-op,
# 1 failure, 2 dry-run. Defined locally so the 2 = dry-run semantics are not
# conflated with the EXIT_NOT_FOUND=2 used by project-resolution commands.
EXIT_DRY_RUN = 2

_STABLE_HARNESS_TARGETS = ("claude", "opencode")
_CANDIDATE_HARNESS_TARGETS = ("codex",)
_PRIVATE_HARNESS_TARGETS = ("hermes",)
_HARNESS_TARGETS = _STABLE_HARNESS_TARGETS + _CANDIDATE_HARNESS_TARGETS + _PRIVATE_HARNESS_TARGETS

# Canonical suite env vars to propagate, each with its legacy alias (Plan 017
# WI-1.1). The canonical name is preferred (checked first) and is what gets
# written into the harness config so the resolver reads it without a
# deprecation warning. The legacy ``AGENT_NOTES_REGISTA_*`` alias is retained
# as a deprecated fallback for one release; it is read only when the canonical
# var is unset.
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


# ---------------------------------------------------------------------------
# hash helpers (Plan 019 Decision 5, WI-1.2)
# ---------------------------------------------------------------------------


def _sha256(content: str) -> str:
    """SHA-256 hex digest of a string."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str | None:
    """SHA-256 hex digest of a file's content.

    Returns ``None`` if the file cannot be read as UTF-8 (e.g., the user
    replaced it with a binary file).  A ``None`` result causes the caller to
    treat the file as modified — our installed files are always UTF-8, so a
    non-UTF-8 file is definitely user-modified.
    """
    try:
        return _sha256(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        return None


def _normalize_hash_map(raw: object) -> dict[str, str | None]:
    """Normalize a manifest ``skills``/``agents`` entry to a name→hash dict.

    Handles both the current dict format (``{"name": "<hash>"}``) and the
    legacy list format (``["name1", "name2"]``) from before hash tracking was
    added.  Entries from the legacy format get ``None`` — meaning "hash
    unknown, cannot detect user modification" — so the old behavior (always
    update / always remove) is preserved for those entries.  Non-string hash
    values from a crafted or corrupted manifest are coerced to ``None``.
    """
    if isinstance(raw, dict):
        return {str(k): v if isinstance(v, str) else None for k, v in raw.items()}
    if isinstance(raw, list):
        return {str(name): None for name in raw}
    return {}


def _install_tracked(
    payload: str,
    dest: Path,
    dry_run: bool,
    recorded_hash: str | None,
    *,
    managed_before: bool,
) -> tuple[str, str | None, bool]:
    """Install a single file with hash-based modification detection.

    Returns ``(status, installed_hash, is_conflict)``.

    * ``status`` — ``'created'``, ``'updated'``, ``'unchanged'``, or
      ``'conflict'`` (user-modified, preserved).
    * ``installed_hash`` — the hash to retain in the ownership manifest. For a
      conflict on a previously managed file this is the prior recorded hash.
      For a pre-existing unowned conflict it is ``None``: observing a foreign
      file is not ownership evidence and uninstall must never claim it.
    * ``is_conflict`` — ``True`` when the file was locally modified and
      preserved.

    Conflict detection (Plan 019 Decision 5): if the file exists, differs
    from ``payload``, and we have a prior ``recorded_hash`` that does not
    match the on-disk content, the user modified it after our last install.
    The file is left untouched and a conflict is reported. Any file with no
    prior manifest entry is also a conflict and remains unowned, even if its
    bytes happen to match: equality is not proof that agent-notes may delete
    it later. When a legacy manifest names the file but has no hash, the update
    proceeds as before for backward compatibility.
    """
    src_hash = _sha256(payload)

    if dest.exists():
        # Try to read existing content as UTF-8. If the user replaced the
        # file with binary content, UnicodeDecodeError is caught — the file
        # is definitely not our payload (which is always UTF-8 text).
        try:
            existing = dest.read_text(encoding="utf-8")
            content_matches = existing == payload
        except UnicodeDecodeError:
            content_matches = False
        # Content equality proves compatibility, not ownership. A file that
        # predates our manifest may belong to another installer; adopting it
        # would let our uninstall delete someone else's managed asset.
        if not managed_before:
            return "conflict", None, True
        if content_matches:
            return "unchanged", src_hash, False
        if recorded_hash is not None:
            on_disk_hash = _sha256_file(dest)
            if on_disk_hash != recorded_hash:
                return "conflict", recorded_hash, True
        if not dry_run:
            dest.write_text(payload, encoding="utf-8")
        return "updated", src_hash, False

    if not dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(payload, encoding="utf-8")
    return "created", src_hash, False


def _act(kind: str, keys: list[str], detail: str, status: str = "") -> dict:
    """Build a contract action dict (path is filled in by the caller)."""
    a: dict = {"kind": kind, "path": "", "keys": keys, "detail": detail}
    if status:
        a["status"] = status
    return a


def _action_is_unchanged(a: dict) -> bool:
    """True if an action represents an already-present managed object."""
    detail = a.get("detail", "")
    if detail.endswith("(unchanged)"):
        return True
    if a.get("status") == "unchanged":
        return True
    # merge_json details from env/plugin wiring use prose ending in
    # "already set (unchanged)" / "plugin already registered (unchanged)".
    if "(unchanged)" in detail:
        return True
    return False


# ---------------------------------------------------------------------------
# path helpers
# ---------------------------------------------------------------------------


def _plugin_path() -> Path:
    """Resolve the opencode plugin path (repo-relative, absolute)."""
    return _repo_skills_root().parent / "integrations" / "opencode" / "index.js"


def _opencode_agents_src_root(src_override: Path | None = None) -> Path:
    """Resolve the repo-local ``.opencode/agents/`` directory.

    When *src_override* is given (the skills source root, typically from
    ``--source``), the agents directory is derived relative to it — so test
    fixtures that place ``.opencode/agents/`` alongside ``skills/`` are found.
    """
    base = src_override if src_override is not None else _repo_skills_root()
    return base.parent / ".opencode" / "agents"


def _opencode_agents_dest(home: Path | None = None) -> Path:
    """Resolve the user-global opencode agents directory."""
    return (home or Path.home()) / ".config" / "opencode" / "agents"


def _codex_home(home: Path | None = None) -> Path:
    """Resolve Codex's config root: ``$CODEX_HOME`` else ``~/.codex``.

    Under a test ``--home`` override, use ``<home>/.codex`` so fixtures stay
    isolated (mirrors how the other harnesses derive their base under ``home``).
    A real run honors the operator's ``CODEX_HOME`` if set.
    """
    if home is not None:
        return home / ".codex"
    env = os.environ.get("CODEX_HOME")
    return Path(env) if env else Path.home() / ".codex"


def _harness_paths(harness: str, home: Path | None = None) -> dict[str, Path]:
    """Return the config/skill/manifest/agent paths for a harness target."""
    resolved = home or Path.home()
    agent_config = _config_path(home=home)
    if harness == "claude":
        base = resolved / ".claude"
        return {
            "skills_dest": base / "skills",
            "config": base / "settings.json",
            "manifest": base / MANIFEST_FILENAME,
            "agent_config": agent_config,
            "agents_dest": base / "agents",
        }
    if harness == "hermes":
        base = resolved / ".hermes"
        return {
            "skills_dest": base / "skills",
            "config": base / ".env",
            "manifest": base / MANIFEST_FILENAME,
            "agent_config": resolved / ".config" / "agent-notes" / "config.json",
            "agents_dest": base / "agents",
        }
    if harness == "codex":
        # Codex user-scoped shared skills live at $HOME/.agents/skills per the
        # suite install-harness contract (§2 Codex) and Plan 019 Decision 2
        # (restored 2026-07-18); repo-scoped skills live at .agents/skills (a
        # per-project concern, not handled by this user-global command). Skills
        # are home-relative, NOT under $CODEX_HOME: $CODEX_HOME holds only Codex
        # user config and installed-plugin state. No env, plugin, agent, or TOML
        # config is written for Codex. Phase 2 merges lifecycle groups into the
        # separate hooks.json file; the ownership manifest stays under
        # $CODEX_HOME as a stable sidecar.
        base = _codex_home(home)
        return {
            "skills_dest": resolved / ".agents" / "skills",
            "config": base / "config.toml",  # never written; present for shape
            "manifest": base / MANIFEST_FILENAME,
            "agent_config": agent_config,
            "agents_dest": base / "agents",
            "hooks": base / "hooks.json",
        }
    # opencode
    base = resolved / ".config" / "opencode"
    return {
        "skills_dest": base / "command",
        "config": base / "opencode.json",
        "manifest": base / MANIFEST_FILENAME,
        "agent_config": agent_config,
        "agents_dest": base / "agents",
    }


# ---------------------------------------------------------------------------
# json helpers
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"Cannot parse {path}: {exc}. Refusing to overwrite a corrupted config. "
            f"Fix or remove the file and re-run."
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} contains a {type(data).__name__}, not a JSON object.")
    return data


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    import tempfile

    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# Codex lifecycle hook merge (Plan 019 Phase 2)
# ---------------------------------------------------------------------------


def _json_hash(value: object) -> str:
    """Hash a JSON value in a formatting-independent canonical form."""
    return _sha256(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _normalize_hook_hashes(raw: object) -> dict[str, str | None]:
    """Normalize the manifest's event-to-owned-group-hash mapping."""
    return _normalize_hash_map(raw)


def _hook_group_has_command(group: object, command: str) -> bool:
    if not isinstance(group, dict):
        return False
    handlers = group.get("hooks")
    if not isinstance(handlers, list):
        return False
    return any(
        isinstance(handler, dict) and handler.get("command") == command for handler in handlers
    )


def _wire_codex_hooks(
    hooks_path: Path,
    dry_run: bool,
    prev_hashes: dict[str, str | None],
) -> tuple[list[dict], dict[str, str], list[str], bool]:
    """Merge only agent-notes' two matcher groups into a shared hooks file.

    A same-command group without prior hash ownership is a conflict even when
    byte-equivalent.  This mirrors skill ownership: compatibility is not proof
    that a later uninstall may delete another installer's entry.
    """
    from agent_notes.codex_lifecycle import canonical_hook_groups

    existed_before = hooks_path.is_file()
    document = _load_json(hooks_path)
    raw_hooks = document.get("hooks", {})
    if not isinstance(raw_hooks, dict):
        raise RuntimeError(f"{hooks_path} has a non-object 'hooks' value; refusing to overwrite")
    hooks = raw_hooks
    document["hooks"] = hooks

    actions: list[dict] = []
    owned_hashes: dict[str, str] = {}
    conflicts: list[str] = []
    changed = False
    for event, canonical in canonical_hook_groups().items():
        command = canonical["hooks"][0]["command"]
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise RuntimeError(
                f"{hooks_path} has a non-array hooks.{event} value; refusing to overwrite"
            )
        candidates = [
            index
            for index, group in enumerate(groups)
            if _hook_group_has_command(group, str(command))
        ]
        canonical_hash = _json_hash(canonical)
        status: str
        if not candidates:
            groups.append(canonical)
            hooks[event] = groups
            owned_hashes[event] = canonical_hash
            status = "created"
            changed = True
        elif len(candidates) > 1:
            status = "conflict"
            conflicts.append(f"Codex {event} hook: duplicate same-command groups, preserved")
            if event in prev_hashes and prev_hashes[event] is not None:
                owned_hashes[event] = str(prev_hashes[event])
        else:
            index = candidates[0]
            current = groups[index]
            current_hash = _json_hash(current)
            if event not in prev_hashes:
                status = "conflict"
                conflicts.append(
                    f"Codex {event} hook: pre-existing and not owned by agent-notes, preserved"
                )
            elif current_hash == canonical_hash:
                status = "unchanged"
                owned_hashes[event] = canonical_hash
            elif prev_hashes[event] is not None and current_hash != prev_hashes[event]:
                status = "conflict"
                owned_hashes[event] = str(prev_hashes[event])
                conflicts.append(f"Codex {event} hook: locally modified, preserved")
            else:
                groups[index] = canonical
                hooks[event] = groups
                owned_hashes[event] = canonical_hash
                status = "updated"
                changed = True

        action = _act(
            "merge_json",
            [f"hooks.{event}"],
            f"install Codex {event} lifecycle hook ({status})",
            status=status,
        )
        action["path"] = str(hooks_path)
        actions.append(action)

    if changed and not dry_run:
        _save_json(hooks_path, document)
    return actions, owned_hashes, conflicts, not existed_before


def _unwire_codex_hooks(
    hooks_path: Path,
    recorded_hashes: dict[str, str | None],
    *,
    remove_created_file: bool,
) -> tuple[list[dict], dict[str, str], list[str]]:
    """Remove only unchanged, hash-owned agent-notes matcher groups."""
    from agent_notes.codex_lifecycle import canonical_hook_groups

    if not recorded_hashes:
        return [], {}, []
    document = _load_json(hooks_path)
    raw_hooks = document.get("hooks", {})
    if not isinstance(raw_hooks, dict):
        return (
            [],
            {event: value for event, value in recorded_hashes.items() if value is not None},
            ["Codex hooks: malformed shared hooks object, preserved"],
        )

    actions: list[dict] = []
    preserved: dict[str, str] = {}
    warnings: list[str] = []
    changed = False
    for event, canonical in canonical_hook_groups().items():
        if event not in recorded_hashes:
            continue
        command = canonical["hooks"][0]["command"]
        groups = raw_hooks.get(event, [])
        if not isinstance(groups, list):
            if recorded_hashes[event] is not None:
                preserved[event] = str(recorded_hashes[event])
            warnings.append(f"Codex {event} hook: malformed event group, preserved")
            continue
        candidates = [
            index
            for index, group in enumerate(groups)
            if _hook_group_has_command(group, str(command))
        ]
        matching = [
            index
            for index in candidates
            if recorded_hashes[event] is None or _json_hash(groups[index]) == recorded_hashes[event]
        ]
        if len(matching) == 1:
            groups.pop(matching[0])
            if groups:
                raw_hooks[event] = groups
            else:
                raw_hooks.pop(event, None)
            action = _act(
                "remove_key",
                [f"hooks.{event}"],
                f"removed owned Codex {event} lifecycle hook",
            )
            action["path"] = str(hooks_path)
            actions.append(action)
            changed = True
        elif not candidates:
            # Already absent is safe and needs no retained ownership record.
            continue
        else:
            if recorded_hashes[event] is not None:
                preserved[event] = str(recorded_hashes[event])
            warnings.append(f"Codex {event} hook: modified or duplicated, preserved")
            action = _act(
                "skip_key",
                [f"hooks.{event}"],
                f"Codex {event} lifecycle hook modified or duplicated, preserved",
                status="conflict",
            )
            action["path"] = str(hooks_path)
            actions.append(action)

    if changed:
        document["hooks"] = raw_hooks
        if remove_created_file and not raw_hooks and set(document) == {"hooks"}:
            hooks_path.unlink(missing_ok=True)
        else:
            _save_json(hooks_path, document)
    return actions, preserved, warnings


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
    target: str,
    src_root: Path,
    dest_root: Path,
    dry_run: bool,
    prev_hashes: dict[str, str | None] | None = None,
) -> tuple[list[dict], dict[str, str], list[str]]:
    """Install skills for a harness target.

    Returns ``(action_dicts, skill_hashes, conflicts)``.

    * ``skill_hashes`` — maps skill name to the SHA-256 of the content on disk
      after install (or the prior hash if a conflict was detected).
    * ``conflicts`` — human-readable messages for user-modified skills that
      were preserved (Plan 019 Decision 5, WI-1.2).

    Reuses the tested discovery logic from :mod:`agent_notes.cli.skills` so
    skill discovery stays identical to ``install-skills`` (Plan 004 §9 Q4).
    """
    prev_hashes = prev_hashes or {}
    skills = _discover_skills(src_root)
    actions: list[dict] = []
    skill_hashes: dict[str, str] = {}
    conflicts: list[str] = []
    for src in skills:
        name = src.parent.name
        src_content = src.read_text(encoding="utf-8")
        if target in ("claude", "hermes", "codex"):
            dest = dest_root / name / "SKILL.md"
            payload = src_content
        else:
            dest = dest_root / f"{name}.md"
            payload = _to_opencode_body(src_content)
        status, installed_hash, is_conflict = _install_tracked(
            payload,
            dest,
            dry_run,
            prev_hashes.get(name),
            managed_before=name in prev_hashes,
        )
        if installed_hash is not None:
            skill_hashes[name] = installed_hash
        if is_conflict:
            reason = (
                "locally modified"
                if name in prev_hashes
                else "pre-existing and not owned by agent-notes"
            )
            conflicts.append(f"skill '{name}': {reason}, not overwritten")
        a = _act("create_file", [], f"install skill ({status})", status=status)
        a["path"] = str(dest)
        actions.append(a)
    return actions, skill_hashes, conflicts


def _install_opencode_agents(
    src_root: Path,
    dest_root: Path,
    dry_run: bool,
    prev_agents: dict[str, str | None],
) -> tuple[list[dict], dict[str, str], list[str]]:
    """Install repo-local opencode subagent definitions.

    Copies the agent Markdown files from ``.opencode/agents/`` into the
    user-global opencode agents directory. The adversarial reviewer agents
    are read-only (edit denied) with limited ``agent-notes`` and ``git``
    bash access so they can drive the review gate without mutating code.

    Returns ``(action_dicts, agent_hashes, conflicts)``.  Hash tracking is
    identical to :func:`_install_skills` — a user-modified agent file is
    preserved, not overwritten (Plan 019 Decision 5).

    Preserves previously-managed agents still on disk but no longer in the
    repo so uninstall can remove orphaned files (mirror of the skill
    preservation logic).
    """
    actions: list[dict] = []
    agent_hashes: dict[str, str] = {}
    conflicts: list[str] = []
    for filename in _OPENCODE_AGENT_FILES:
        src = src_root / filename
        if not src.is_file():
            continue
        dest = dest_root / filename
        name = Path(filename).stem
        src_content = src.read_text(encoding="utf-8")
        status, installed_hash, is_conflict = _install_tracked(
            src_content,
            dest,
            dry_run,
            prev_agents.get(name),
            managed_before=name in prev_agents,
        )
        if installed_hash is not None:
            agent_hashes[name] = installed_hash
        if is_conflict:
            reason = (
                "locally modified"
                if name in prev_agents
                else "pre-existing and not owned by agent-notes"
            )
            conflicts.append(f"agent '{name}': {reason}, not overwritten")
        a = _act("create_file", [], f"install agent ({status})", status=status)
        a["path"] = str(dest)
        actions.append(a)

    for name, recorded_hash in prev_agents.items():
        if name in agent_hashes:
            continue
        af = dest_root / f"{name}.md"
        if af.is_file():
            if recorded_hash is not None:
                agent_hashes[name] = recorded_hash
            else:
                agent_hashes[name] = _sha256_file(af)

    return actions, agent_hashes, conflicts


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
# env wiring — hermes (.env KEY=VALUE with sentinel block)
# ---------------------------------------------------------------------------

_BEGIN_SENTINEL = f"# BEGIN {TOOL_NAME}-harness-managed"
_END_SENTINEL = f"# END {TOOL_NAME}-harness-managed"


def _parse_env_file(content: str) -> list[tuple[str, str]]:
    """Parse a .env file into an ordered list of (key, value) pairs.

    Lines outside the managed block are returned as-is. Sentinel comments are
    *not* included — they are structural markers re-inserted by
    :func:`_serialize_env_file`.
    """
    pairs: list[tuple[str, str]] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if not stripped:
            continue
        if "=" in stripped:
            key, _, val = stripped.partition("=")
            pairs.append((key.strip(), val.strip()))
    return pairs


def _serialize_env_file(
    outside: list[tuple[str, str]],
    managed: list[tuple[str, str]],
) -> str:
    """Serialize a .env file: outside lines first, then a sentinel-wrapped managed block."""
    lines: list[str] = []
    for key, val in outside:
        lines.append(f"{key}={val}")
    if managed:
        lines.append("")
        lines.append(_BEGIN_SENTINEL)
        for key, val in managed:
            lines.append(f"{key}={val}")
        lines.append(_END_SENTINEL)
    if not lines or lines[-1] != "":
        lines.append("")
    return "\n".join(lines) + "\n"


def _wire_env_hermes(
    env_path: Path, env_values: dict[str, str], dry_run: bool
) -> tuple[list[dict], list[str], list[str], list[str]]:
    """Merge env vars into ~/.hermes/.env using a sentinel-wrapped managed block.

    Returns (actions, newly_written, matching, warnings).

    * If the .env file doesn't exist, it is created with the managed block.
    * If sentinels already exist, the managed block is replaced (idempotent).
    * If a key exists *outside* the managed block with a *different* value, warn
      and skip (no clobber, contract §3 rule 3).
    * If a key exists outside with the *same* value, it is treated as matching
      (not re-written).
    """
    existing_content = ""
    if env_path.is_file():
        existing_content = env_path.read_text(encoding="utf-8")

    # Split existing content into managed and outside sections.
    all_pairs = _parse_env_file(existing_content)

    # Identify managed pairs by locating the sentinel block in the raw content.
    lines = existing_content.splitlines()
    in_block = False
    managed_keys_existing: set[str] = set()
    managed_map_existing: dict[str, str] = {}
    for line in lines:
        if line.strip() == _BEGIN_SENTINEL:
            in_block = True
            continue
        if line.strip() == _END_SENTINEL:
            in_block = False
            continue
        if in_block and "=" in line:
            k, _, v = line.strip().partition("=")
            k = k.strip()
            managed_keys_existing.add(k)
            managed_map_existing[k] = v.strip()

    outside_pairs = [(k, v) for k, v in all_pairs if k not in managed_keys_existing]
    outside_map = dict(outside_pairs)

    actions: list[dict] = []
    newly: list[str] = []
    matching: list[str] = []
    warns: list[str] = []
    managed_pairs: list[tuple[str, str]] = []

    for name, value in env_values.items():
        if name in outside_map:
            if outside_map[name] == value:
                actions.append(_act("merge_env", [name], "already set (unchanged)"))
                matching.append(name)
            else:
                warns.append(f"{name}: existing value differs; kept existing (no clobber)")
                actions.append(_act("merge_env", [name], "kept existing (no clobber)"))
        elif name in managed_map_existing:
            # Key already in the managed block — check if value matches.
            if managed_map_existing[name] == value:
                actions.append(_act("merge_env", [name], "already set (unchanged)"))
                matching.append(name)
            else:
                # Value changed — update it (we own the managed block).
                managed_pairs.append((name, value))
                newly.append(name)
                actions.append(_act("merge_env", [name], "updated suite env var"))
        else:
            managed_pairs.append((name, value))
            newly.append(name)
            actions.append(_act("merge_env", [name], "set suite env var"))

    if not dry_run and newly:
        # Only write when there are new or updated entries.
        # Preserve previously-managed keys that are no longer in env_values but
        # still in the old managed block (review B1: manifest drift).
        prev_managed_map: dict[str, str] = {}
        in_block = False
        for line in lines:
            if line.strip() == _BEGIN_SENTINEL:
                in_block = True
                continue
            if line.strip() == _END_SENTINEL:
                in_block = False
                continue
            if in_block and "=" in line:
                k, _, v = line.strip().partition("=")
                prev_managed_map[k.strip()] = v.strip()

        # Merge: new values override, preserve old ones not in env_values.
        final_managed: dict[str, str] = {}
        final_managed.update(prev_managed_map)
        for k, v in managed_pairs:
            final_managed[k] = v

        # Only keep managed entries that are still relevant (either in
        # env_values or in prev_managed_map and not clobbered by outside).
        managed_list = [(k, v) for k, v in final_managed.items() if k not in outside_map]

        content = _serialize_env_file(outside_pairs, managed_list)
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(content, encoding="utf-8")

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

    # Read the prior manifest first so we can (a) preserve previously-managed
    # keys that are still present but no longer in env_values (user unset a var
    # between installs) — without this, re-install would drop them from the
    # manifest and uninstall would leak them (review B1); and (b) detect
    # user-modified skills/agents via hash comparison (Plan 019 Decision 5).
    # Always read, even in dry-run, so conflicts are surfaced in --dry-run too.
    # In dry-run mode, a corrupted manifest should not block the preview —
    # fall back to an empty manifest with a warning (review Finding 3).
    try:
        prev = _read_manifest(paths["manifest"])
    except RuntimeError as exc:
        if not dry_run:
            raise
        warns.append(f"could not read manifest: {exc}")
        prev = {}
    prev_env_keys = set(prev.get("env_keys", []))
    prev_plugin = bool(prev.get("plugin", False))
    prev_skill_hashes = _normalize_hash_map(prev.get("skills", []))
    prev_agent_hashes = _normalize_hash_map(prev.get("agents", []))
    prev_hook_hashes = _normalize_hook_hashes(prev.get("hooks", {}))

    # --- skills ---
    skill_actions, skill_hashes, skill_conflicts = _install_skills(
        harness, src_root, skills_dest, dry_run, prev_skill_hashes
    )
    warns += skill_conflicts
    # Refuse the false green (WI-022): when the source resolves to zero skills
    # the install must not report `installed / no_op: true`. Contract §4 bounds
    # `no_op: true` to an already-installed idempotent state; an empty source is
    # a failure, surfaced as an explicit action so --dry-run previews it too.
    # This fires whether the default source path is wrong (e.g. an editable
    # checkout whose package-internal skills/ is absent) or an operator-supplied
    # --source points at an empty tree. Previously-manifested skills on disk do
    # not change this: install-harness installs from the source, not from disk.
    if not skill_actions:
        msg = f"no skills found at source {src_root}"
        warns.append(msg)
        # `kind: "skip_file"` (not "create_file"): nothing is created — this
        # action records a failed discovery. `status: "missing"` lets dry-run
        # previews and consumers distinguish it from a real file write.
        miss = _act("skip_file", [], msg, status="missing")
        miss["path"] = str(src_root)
        return (
            {
                "tool": TOOL_NAME,
                "harness": harness,
                "user": user,
                "status": "failed",
                "actions": [miss],
                "no_op": False,
                "warnings": warns,
            },
            warns,
        )
    # Preserve previously-manifested skills still on disk but no longer in the
    # repo so uninstall can remove the orphaned files (review B1).
    for name, recorded_hash in prev_skill_hashes.items():
        if name in skill_hashes:
            continue
        sf = (
            skills_dest / name / "SKILL.md"
            if harness in ("claude", "hermes", "codex")
            else skills_dest / f"{name}.md"
        )
        if sf.is_file():
            if recorded_hash is not None:
                skill_hashes[name] = recorded_hash
            else:
                skill_hashes[name] = _sha256_file(sf)

    # --- opencode agents (opencode only) ---
    agent_actions: list[dict] = []
    installed_agent_hashes: dict[str, str] = {}
    if harness == "opencode":
        agent_actions, installed_agent_hashes, agent_conflicts = _install_opencode_agents(
            _opencode_agents_src_root(source),
            paths["agents_dest"],
            dry_run,
            prev_agent_hashes,
        )
        warns += agent_conflicts

    if user and harness == "opencode":
        warns.append(
            "--user has no opencode config-file field yet (principal_id resolves "
            "from git config); principal overlay skipped for opencode"
        )

    # --- config + env + plugin ---
    # Hermes config is a .env file and Codex's is TOML — neither is JSON, and
    # Codex config is never touched at all (Decision 4), so skip the JSON load.
    config = _load_json(config_path) if harness not in ("hermes", "codex") else {}
    config_actions: list[dict] = []
    installed_hook_hashes: dict[str, str] = {}
    hooks_file_created = bool(prev.get("hooks_file_created", False))
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
    elif harness == "hermes":
        # Hermes uses a .env KEY=VALUE file with sentinel-wrapped managed block
        # (not JSON). The file is written atomically by _wire_env_hermes.
        ha, hnewly, hmatching, hw = _wire_env_hermes(config_path, env_values, dry_run)
        for a in ha:
            a["path"] = str(config_path)
        config_actions += ha
        env_newly = hnewly
        env_managed = hnewly + [k for k in hmatching if k in prev_env_keys]
        # Preserve previously-managed env keys still in the .env managed block
        # but no longer propagated — review B1.
        if config_path.is_file():
            content = config_path.read_text(encoding="utf-8")
            in_block = False
            for line in content.splitlines():
                if line.strip() == _BEGIN_SENTINEL:
                    in_block = True
                    continue
                if line.strip() == _END_SENTINEL:
                    in_block = False
                    continue
                if in_block and "=" in line:
                    k = line.strip().partition("=")[0].strip()
                    if k not in env_managed:
                        env_managed.append(k)
        warns += hw
    elif harness == "codex":
        # Codex: no env/plugin/TOML wiring (Decision 4).  Merge only the two
        # canonical lifecycle matcher groups into the shared hooks.json. Cairn
        # and user groups remain untouched and are never adopted by ownership.
        hook_actions, installed_hook_hashes, hook_conflicts, created_now = _wire_codex_hooks(
            paths["hooks"], dry_run, prev_hook_hashes
        )
        config_actions += hook_actions
        warns += hook_conflicts
        hooks_file_created = hooks_file_created or created_now
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

    all_actions = skill_actions + config_actions + agent_actions
    agents_newly = any(a.get("status") in ("created", "updated") for a in agent_actions)
    any_newly = bool(env_newly or plugin_newly or agents_newly)
    no_op = all(_action_is_unchanged(a) for a in all_actions) and not any_newly
    has_conflict = any(a.get("status") == "conflict" for a in all_actions)
    # --- manifest: record what install-harness manages (survives re-installs) ---
    if not dry_run:
        try:
            from importlib.metadata import version

            ver = version("agent-notes")
        except Exception:
            ver = "unknown"

        manifest_payload: dict = {
            "tool": TOOL_NAME,
            "version": ver,
            "harness": harness,
            "env_keys": env_managed,
            "skills": skill_hashes,
            "plugin": plugin_managed,
        }
        if harness == "codex":
            manifest_payload["hooks"] = installed_hook_hashes
            manifest_payload["hooks_file_created"] = hooks_file_created
        if harness == "opencode":
            manifest_payload["agents"] = installed_agent_hashes
        _write_manifest(
            paths["manifest"],
            manifest_payload,
        )

    # Contract §4: status is one of installed|degraded|unsupported|failed.
    # A conflict (user-modified file preserved) means the install did not
    # complete safely → "failed".  In dry-run the status still reflects the
    # conflict (the preview detected an unsafe condition), but the exit code
    # remains 2 (dry-run takes precedence in the exit-code logic).
    install_status = "failed" if has_conflict else "installed"

    result = {
        "tool": TOOL_NAME,
        "harness": harness,
        "user": user,
        "status": install_status,
        "actions": all_actions,
        "no_op": no_op,
        "warnings": warns,
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
                "status": "installed",
                "actions": [],
                "no_op": True,
                "uninstalled": True,
                "warnings": [],
            },
            [],
        )

    # If the user added the plugin manually before install-harness ran, we treat
    # it as managed so uninstall remains safe. Detect by checking whether the
    # plugin path is present in the live config without a prior manifest record.
    if harness == "opencode" and not manifest.get("plugin"):
        oc_cfg = _load_json(paths["config"])
        plugins = oc_cfg.get("plugin", [])
        ppath = str(_plugin_path())
        if any(
            p == ppath or (isinstance(p, list) and p and p[0] == ppath)
            for p in (plugins if isinstance(plugins, list) else [])
        ):
            manifest["plugin"] = True

    # --- remove Codex lifecycle groups (Codex only) ---
    preserved_hooks: dict[str, str] = {}
    if harness == "codex":
        hook_actions, preserved_hooks, hook_warnings = _unwire_codex_hooks(
            paths["hooks"],
            _normalize_hook_hashes(manifest.get("hooks", {})),
            remove_created_file=bool(manifest.get("hooks_file_created", False)),
        )
        actions += hook_actions
        warns += hook_warnings

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
    elif harness == "hermes":
        # Hermes .env: remove the sentinel-wrapped managed block entirely.
        env_path = paths["config"]
        if env_path.is_file():
            content = env_path.read_text(encoding="utf-8")
            lines = content.splitlines()
            in_block = False
            new_lines: list[str] = []
            removed_any = False
            for line in lines:
                if line.strip() == _BEGIN_SENTINEL:
                    in_block = True
                    removed_any = True
                    continue
                if line.strip() == _END_SENTINEL:
                    in_block = False
                    continue
                if in_block:
                    continue
                new_lines.append(line)
            if removed_any:
                # Clean up trailing blank lines left by block removal.
                while new_lines and new_lines[-1].strip() == "":
                    new_lines.pop()
                result_content = "\n".join(new_lines) + "\n" if new_lines else ""
                env_path.write_text(result_content, encoding="utf-8")
                a = _act("remove_key", ["managed-block"], "removed managed env block")
                a["path"] = str(env_path)
                actions.append(a)
    elif harness == "opencode":  # env lives in the agent-notes config file
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

    # --- remove opencode agents (opencode only) ---
    agent_hashes = _normalize_hash_map(manifest.get("agents", []))
    preserved_agents: dict[str, str] = {}
    if harness == "opencode" and agent_hashes:
        agents_dest = paths["agents_dest"]
        for name, recorded_hash in agent_hashes.items():
            agent_file = agents_dest / f"{name}.md"
            if agent_file.is_file():
                if recorded_hash is not None:
                    on_disk = _sha256_file(agent_file)
                    if on_disk != recorded_hash:
                        warns.append(f"agent '{name}': locally modified, preserved")
                        a = _act("skip_file", [], "agent locally modified, preserved")
                        a["path"] = str(agent_file)
                        actions.append(a)
                        preserved_agents[name] = recorded_hash
                        continue
                agent_file.unlink()
                a = _act("remove_file", [], "removed agent")
                a["path"] = str(agent_file)
                actions.append(a)

    # --- remove skills ---
    skill_hashes = _normalize_hash_map(manifest.get("skills", []))
    preserved_skills: dict[str, str] = {}
    skills_dest = paths["skills_dest"]
    for name, recorded_hash in skill_hashes.items():
        if harness in ("claude", "hermes", "codex"):
            skill_file = skills_dest / name / "SKILL.md"
        else:
            skill_file = skills_dest / f"{name}.md"
        if skill_file.is_file():
            if recorded_hash is not None:
                on_disk = _sha256_file(skill_file)
                if on_disk != recorded_hash:
                    warns.append(f"skill '{name}': locally modified, preserved")
                    a = _act("skip_file", [], "skill locally modified, preserved")
                    a["path"] = str(skill_file)
                    actions.append(a)
                    preserved_skills[name] = recorded_hash
                    continue
            skill_file.unlink()
            a = _act("remove_file", [], "removed skill")
            a["path"] = str(skill_file)
            actions.append(a)
            # Clean up empty parent dir (claude/hermes/codex lay out <name>/SKILL.md).
            parent = skill_file.parent
            if (
                harness in ("claude", "hermes", "codex")
                and parent.is_dir()
                and not any(parent.iterdir())
            ):
                parent.rmdir()

    # If any skills/agents were preserved (user-modified), retain a reduced
    # manifest so a future install-harness can detect the conflict via hash
    # comparison. Without this, the next install would see no manifest, treat
    # the files as first-install (recorded_hash=None), and silently overwrite
    # the user's modifications (review Finding 1).
    if preserved_skills or preserved_agents or preserved_hooks:
        reduced: dict = {
            "tool": TOOL_NAME,
            "version": manifest.get("version", "unknown"),
            "harness": harness,
            "env_keys": [],
            "skills": preserved_skills,
            "plugin": False,
        }
        if harness == "opencode" and preserved_agents:
            reduced["agents"] = preserved_agents
        if harness == "codex" and preserved_hooks:
            reduced["hooks"] = preserved_hooks
            reduced["hooks_file_created"] = bool(manifest.get("hooks_file_created", False))
        _write_manifest(paths["manifest"], reduced)
    else:
        _remove_manifest(paths["manifest"])

    has_preserved = bool(preserved_skills or preserved_agents or preserved_hooks)
    result = {
        "tool": TOOL_NAME,
        "harness": harness,
        "user": None,
        "status": "failed" if has_preserved else "installed",
        "actions": actions,
        "no_op": not actions,
        "uninstalled": True,
        "warnings": warns,
    }
    return result, warns


# ---------------------------------------------------------------------------
# command
# ---------------------------------------------------------------------------


def _targets_for(harness: str) -> list[str]:
    if harness == "all":
        return list(_STABLE_HARNESS_TARGETS)
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
        print(
            f"Unknown harness: {harness!r} (expected: claude|opencode|codex|hermes|all)",
            file=sys.stderr,
        )
        return EXIT_GENERIC

    all_warns: list[str] = []
    results: list[dict] = []
    for tgt in targets:
        if uninstall:
            try:
                res, warns = _uninstall_one(tgt, home=home)
            except RuntimeError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return EXIT_GENERIC
        else:
            try:
                res, warns = _install_harness_one(tgt, dry_run, user, source, dest, home=home)
            except RuntimeError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return EXIT_GENERIC
        results.append(res)
        all_warns += warns

    # Contract §4 exit codes: 0 success, 1 failed/unsupported, 2 dry-run.
    # Unsupported takes precedence over dry-run (contract: "unsupported
    # takes precedence over the informational dry-run state").  A failed
    # status (conflict in non-dry-run) exits 1; in dry-run the preview
    # completed, so exit 2.
    has_unsupported = any(res.get("status") == "unsupported" for res in results)
    has_failed = any(res.get("status") == "failed" for res in results)
    exit_code = (
        EXIT_GENERIC
        if has_unsupported
        else (EXIT_DRY_RUN if dry_run else (EXIT_GENERIC if has_failed else EXIT_SUCCESS))
    )

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
            if res.get("status") == "unsupported":
                print(f"[{res['harness']}] unsupported (not wired).")
                for action in res["actions"]:
                    print(f"  {action['kind']}: {action.get('detail', '')}")
                continue
            verb = "Uninstalled" if uninstall else "Installed"
            if res.get("status") == "failed":
                print(f"[{res['harness']}] {verb} with conflicts (user-modified files preserved).")
            elif res["no_op"]:
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
    p.add_argument("harness", help="claude | opencode | codex | hermes | all")
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
