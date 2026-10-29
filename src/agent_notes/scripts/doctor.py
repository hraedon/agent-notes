"""Health-check script for agent-notes installations (Phase 6.3 + Plan 008 P4).

Checks:
1. DSN reachable
2. Schema up to date (core tables + kernel tables present)
3. Coordination mode (always local-lease: there is no coordinator)
4. Embedding model loads (opt-in via --check-embed)
5. Links audit (dangling links)
6. Vocabulary integrity
7. Bridge target reachable
8. Stale MCP entries in harness configs
9. Retired settings (regista write-through, Plan 026 D1)
10. Legacy regista outbox queue left on disk (Plan 026 D5)

Regista is not checked: write-through was deleted in Plan 026 D1, so an
absent or unset regista is the normal state, never "degraded".

Exit code: 0 if all healthy, 1 if any check failed.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from agent_notes.core import config as reg_config


def _print_section(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def _print_result(ok: bool, msg: str) -> None:
    status = "PASS" if ok else "FAIL"
    print(f"  {status}: {msg}")


def _sanitize_conn_error(exc: Exception) -> str:
    """Return a secret-safe summary of an exception from a DB check.

    ``psycopg`` exception messages can embed the DSN, username, or host (e.g.
    ``password authentication failed for user 'nobody'``). The doctor JSON is
    machine-readable and may land in logs / aggregators, so we never surface
    ``str(exc)`` directly — only the exception type name. Diagnostic detail is
    intentionally traded for secret safety; the type name is enough to point an
    operator at the right area.
    """
    return f"{type(exc).__name__}"


def _check_dsn() -> tuple[bool, str]:
    try:
        from agent_notes.core.db import _conn

        with _conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
        return True, "Connected successfully"
    except Exception as exc:
        return False, _sanitize_conn_error(exc)


def _check_schema() -> tuple[bool, str]:
    expected = {
        "memories",
        "links",
        "change_log",
        "all_notes_search_v",
        "op_log",
        "work_items",
        "content_blobs",
        "op_log_events",
        "work_item_sequences",
        "work_item_leases",
        "review_delegation_attempts",
    }
    try:
        from agent_notes.core.db import _conn

        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                """
            )
            actual = {r["table_name"] for r in cur.fetchall()}
        missing = expected - actual
        if missing:
            return False, f"Missing tables/views: {sorted(missing)}"
        return True, f"All expected tables/views present ({len(expected)} total)"
    except Exception as exc:
        return False, _sanitize_conn_error(exc)


def _check_vm_identity() -> tuple[str, str]:
    """Report the VM ID that names this machine's memory scope (Plan 025 WI-2.2).

    Returns ``(status, detail)``. An unresolvable ID is a ``warn``, not a
    failure: containers and pods legitimately have no stable hostname, and VM
    memory is optional.
    """
    from agent_notes.core.config import VmIdError, resolve_vm_id

    try:
        return "ok", f"vm id: {resolve_vm_id()}"
    except VmIdError as exc:
        return "warn", str(exc)


_COORDINATION_MODE = "local-lease"


def _check_coordination_mode() -> tuple[bool, str]:
    """Report the coordination mode (Plan 008 P4).

    Claims are local leases in the native op-log. The Tier B coordinator stub
    was deleted in Plan 026 D1, so this is always ``local-lease`` and always
    PASS: it is the only mode, not a degraded one.
    """
    return True, _COORDINATION_MODE


_LEGACY_OUTBOX_DIR_ENV = "AGENT_NOTES_OUTBOX_DIR"


def _legacy_outbox_dir() -> Path:
    """Where the deleted regista outbox queued ops (``core/outbox.py``, Plan 009).

    Same resolution as the old module: ``AGENT_NOTES_OUTBOX_DIR`` if set, else
    ``platformdirs.user_state_dir("regista") / "outbox"``.
    """
    override = os.environ.get(_LEGACY_OUTBOX_DIR_ENV)
    if override:
        return Path(override)
    import platformdirs

    return Path(platformdirs.user_state_dir("regista")) / "outbox"


def _check_legacy_outbox() -> tuple[bool, str]:
    """Fail when the retired regista outbox still holds queued files (Plan 026 D5).

    Read-only: it lists ``*.jsonl`` files under the old outbox directory and
    checks their size. It never opens, parses or replays them; there is no
    write-through left to replay into. A non-empty queue is something the
    operator must archive by hand before the files are lost.
    """
    root = _legacy_outbox_dir()
    try:
        if not root.exists():
            return True, f"no legacy outbox at {root}"
        queued = sorted(p for p in root.rglob("*.jsonl") if p.is_file() and p.stat().st_size > 0)
    except OSError as exc:
        return False, f"legacy outbox at {root} unreadable: {type(exc).__name__}"
    if not queued:
        return True, f"legacy outbox at {root} is empty"
    shown = ", ".join(str(p.relative_to(root)) for p in queued[:5])
    more = f" (+{len(queued) - 5} more)" if len(queued) > 5 else ""
    return False, (
        f"{len(queued)} non-empty legacy regista outbox file(s) under {root}: {shown}{more}. "
        "Regista write-through is gone (Plan 026 D1) and nothing will replay them; "
        "archive the directory (e.g. to NFS), then remove it"
    )


def _check_retired_settings() -> tuple[bool, str]:
    """Fail when a removed feature is still switched on (Plan 026 D1).

    ``AGENT_NOTES_REGISTA_WRITES`` / ``regista.writes_enabled`` used to route
    writes through regista. Every write now refuses to run while it is set, so
    doctor names it rather than letting the first write discover it.
    """
    try:
        if reg_config.regista_writes_requested():
            return False, (
                "AGENT_NOTES_REGISTA_WRITES or regista.writes_enabled is on, but regista "
                "write-through was removed (Plan 026 D1); every write will refuse. "
                "Unset it; see docs/spikes/025-regista-cutover.md"
            )
        return True, "no retired settings in use"
    except Exception as exc:
        return False, f"retired-settings check error: {type(exc).__name__}"


def _check_embedding() -> tuple[bool, str]:
    try:
        from agent_notes.core import embed

        expected_dim = int(os.environ.get("AGENT_NOTES_EMBED_DIM", "0"))
        text = "hello"
        t0 = time.perf_counter()
        vec = embed.embed(text, task="query")
        elapsed = time.perf_counter() - t0
        dim = int(vec.shape[0])
        msg = f"Model loaded in {elapsed:.2f}s, dim={dim}"
        if expected_dim and expected_dim != dim:
            warn = f"{msg}; WARNING: AGENT_NOTES_EMBED_DIM={expected_dim} but model emits {dim}"
            return False, warn
        return True, msg
    except Exception as exc:
        return False, _sanitize_conn_error(exc)


def _check_links_audit() -> tuple[bool | None, str]:
    """Audit referential integrity of the local links table.

    Three classes of memory-to-memory edges live in ``links``:

    * **Strict referential edges** — ``from_kind='work_item'`` source rows and
      any memory-to-memory edge whose ``relationship`` is NOT ``relates_to``.
      The strict set is intentionally negatively enumerated: ``relates_to`` is
      the only known soft relationship today, and any new relationship defaults
      to strict (safer) until explicitly added to the soft set. In practice
      today this catches any future relationship type that should resolve
      locally (e.g. a hypothetical ``derived_from``). These must resolve to a
      row that exists in this database; a dangling strict edge is a real
      integrity violation and fails the audit.

    * **Local orphan edges** — ``relates_to`` wikilinks whose target memory
      *exists in this DB* but is inactive (``active = false``, i.e. soft-deleted
      via ``delete_memory``). The source memory's body still contains the
      ``[[name]]`` wikilink, but the target is gone — a real broken edge in the
      local knowledge graph. These are integrity violations and fail the audit.
      (The supersede path in ``add_memory`` does NOT create orphans: it creates
      a new active memory with the same name, so inbound links resolve to the
      replacement by name.)

    * **Soft cross-project edges** — auto-created ``relates_to`` wikilinks
      (see ``memory_model._auto_create_wikilinks``) whose target memory does
      NOT exist in this DB at all (neither active nor inactive). The link
      writer intentionally stores these with ``to_project = from_project``
      even when the wikilink target is a foreign-project or workspace-scoped
      anchor (e.g. a memory body that says ``[[project-gpo-lens]]`` or
      ``[[user-central-pillars]]``). Such targets legitimately do not exist
      in the local DB, so a dangling ``relates_to`` memory edge of this kind
      is a **cross-project knowledge reference**, not an integrity violation.
      These are reported as informational (``skip``) rather than failing the
      audit, with the count disclosed so an operator can see how many foreign
      references are unrouted.
    """
    try:
        from agent_notes.core.db import _conn

        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COUNT(*) AS cnt FROM links l
                LEFT JOIN work_items wi
                  ON wi.project_id = l.from_project
                 AND wi.identifier = l.from_identifier
                WHERE l.from_kind = 'work_item' AND wi.id IS NULL
                """
            )
            dangle_wi_from = cur.fetchone()["cnt"]

            # Strict memory edges: anything that is not a soft relates_to wikilink.
            cur.execute(
                """
                SELECT COUNT(*) AS cnt FROM links l
                LEFT JOIN memories m
                  ON m.project_id = l.to_project
                  AND m.name = l.to_identifier
                  AND m.active = true
                WHERE l.to_kind = 'memory'
                  AND m.id IS NULL
                  AND COALESCE(l.relationship, '') <> 'relates_to'
                """
            )
            dangle_mem_to_strict = cur.fetchone()["cnt"]

            cur.execute(
                """
                SELECT COUNT(*) AS cnt FROM links l
                LEFT JOIN memories m
                  ON m.project_id = l.from_project
                  AND m.name = l.from_identifier
                  AND m.active = true
                WHERE l.from_kind = 'memory'
                  AND m.id IS NULL
                  AND COALESCE(l.relationship, '') <> 'relates_to'
                """
            )
            dangle_mem_from_strict = cur.fetchone()["cnt"]

            # Local orphan relates_to refs: target memory EXISTS in this DB but
            # is inactive (soft-deleted). A real broken edge — the source body
            # still wikilinks [[name]] but the target is gone. This is distinct
            # from a cross-project ref (where no row exists at all).
            cur.execute(
                """
                SELECT COUNT(*) AS cnt FROM links l
                WHERE l.to_kind = 'memory'
                  AND COALESCE(l.relationship, '') = 'relates_to'
                  AND EXISTS (
                    SELECT 1 FROM memories m
                    WHERE m.project_id = l.to_project
                      AND m.name = l.to_identifier
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM memories m
                    WHERE m.project_id = l.to_project
                      AND m.name = l.to_identifier
                      AND m.active = true
                  )
                """
            )
            local_orphan_refs = cur.fetchone()["cnt"]

            # Soft cross-project knowledge references (relates_to wikilinks
            # whose target lives in a sibling project's DB — no row exists
            # locally at all). Informational only.
            cur.execute(
                """
                SELECT COUNT(*) AS cnt FROM links l
                WHERE l.to_kind = 'memory'
                  AND COALESCE(l.relationship, '') = 'relates_to'
                  AND NOT EXISTS (
                    SELECT 1 FROM memories m
                    WHERE m.project_id = l.to_project
                      AND m.name = l.to_identifier
                  )
                """
            )
            cross_project_refs = cur.fetchone()["cnt"]

        strict_total = dangle_wi_from + dangle_mem_to_strict + dangle_mem_from_strict
        if strict_total:
            # Report the strict failure with the soft context so the operator
            # sees the full picture, not just the strict bucket.
            detail = (
                f"Dangling strict links: work_item-from={dangle_wi_from}, "
                f"memory-to={dangle_mem_to_strict}, "
                f"memory-from={dangle_mem_from_strict}"
            )
            if local_orphan_refs:
                detail += f"; local orphan relates_to refs={local_orphan_refs}"
            if cross_project_refs:
                detail += f"; cross-project relates_to refs={cross_project_refs}"
            return False, detail
        if local_orphan_refs:
            return (
                False,
                f"Local orphan relates_to refs: {local_orphan_refs} "
                f"(target memory soft-deleted; inbound wikilink dangles)",
            )
        if cross_project_refs:
            return (
                None,
                f"No dangling strict links; {cross_project_refs} cross-project "
                f"relates_to reference(s) resolve in sibling DBs",
            )
        return True, "No dangling links"
    except Exception as exc:
        return False, _sanitize_conn_error(exc)


def _check_vocab_integrity() -> tuple[bool, str]:
    try:
        from agent_notes.core.db import _conn

        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT DISTINCT kind FROM work_items wi
                WHERE NOT EXISTS (
                    SELECT 1 FROM vocabularies v
                    JOIN projects p ON p.id = wi.project_id
                    WHERE v.workspace_id = p.workspace_id
                      AND v.kind_namespace = 'wi_kind' AND v.name = wi.kind
                )
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if row:
                return False, f"Orphan work_item kind: {row[0]}"

            cur.execute(
                """
                SELECT DISTINCT status FROM work_items wi
                WHERE NOT EXISTS (
                    SELECT 1 FROM vocabularies v
                    JOIN projects p ON p.id = wi.project_id
                    WHERE v.workspace_id = p.workspace_id
                      AND v.kind_namespace = 'wi_status' AND v.name = wi.status
                )
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if row:
                return False, f"Orphan work_item status: {row[0]}"

            cur.execute(
                """
                SELECT DISTINCT severity FROM work_items wi
                WHERE NOT EXISTS (
                    SELECT 1 FROM vocabularies v
                    JOIN projects p ON p.id = wi.project_id
                    WHERE v.workspace_id = p.workspace_id
                      AND v.kind_namespace = 'wi_severity' AND v.name = wi.severity
                )
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if row:
                return False, f"Orphan work_item severity: {row[0]}"

            cur.execute(
                """
                SELECT DISTINCT memory_type FROM memories m
                WHERE NOT EXISTS (
                    SELECT 1 FROM vocabularies v
                    WHERE v.workspace_id = m.workspace_id
                      AND v.kind_namespace = 'memory_type' AND v.name = m.memory_type
                )
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if row:
                return False, f"Orphan memory_type: {row[0]}"

        return True, "All kind/status/severity/memory_type values have matching vocab entries"
    except Exception as exc:
        return False, _sanitize_conn_error(exc)


def _check_bridge_target() -> tuple[bool, str]:
    """If AGENT_NOTES_BRIDGE_TARGET is set, attempt a probe POST.

    Accepts any 2xx/4xx as 'reachable' — agent-wake's ingest returns 403 for an
    unsigned/empty body, which still proves the server is up. Connection-refused
    or timeout is a fail.
    """
    target = os.environ.get("AGENT_NOTES_BRIDGE_TARGET")
    if not target:
        return True, "AGENT_NOTES_BRIDGE_TARGET not set (bridge disabled — OK)"
    try:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(target, data=b"", method="POST")
        try:
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                return True, f"Bridge target reachable ({target}, HTTP {resp.status})"
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                return True, f"Bridge target reachable ({target}, HTTP {exc.code})"
            return False, f"Bridge target HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return False, f"Bridge target unreachable: {exc}"
    except Exception as exc:
        return False, _sanitize_conn_error(exc)


def _component_version() -> str:
    """Return the installed agent-notes distribution version.

    The published distribution is ``agent-notes-hraedon``; the pre-rename name
    ``agent-notes`` is still used by editable/local installs. Try both so
    version introspection survives the PyPI rename (parallels regista's fix).
    """
    from importlib.metadata import version

    for dist in ("agent-notes-hraedon", "agent-notes"):
        try:
            return version(dist)
        except Exception:
            continue
    return "unknown"


def _check_chain_ok() -> tuple[bool | None, str]:
    """Verify agent-notes' own op-log chain integrity.

    agent-notes owns an append-only ``op_log`` (Plan 008), the native store of
    record for work items.

    Returns ``(True, ...)`` for a valid (possibly empty) chain. A verifier error
    (missing key file, import failure) returns ``(False, ...)`` — a verifier
    that cannot run indicates a real misconfiguration the operator should see,
    so it fails the suite rather than silently skipping.
    """
    try:
        from agent_notes.core import verifier

        result = verifier.verify_with_auto_key(check_policy=False)
        if result.checked == 0:
            return True, "agent-notes op-log chain: empty (fresh install, no violations)"
        ok = result.ok()
        return ok, (
            f"agent-notes op-log chain: {result.checked} ops checked, {result.failed} violation(s)"
        )
    except Exception as exc:
        return False, f"chain verification error: {type(exc).__name__}"


def _check_skills_installed(*, codex_plugin_wired: bool = False) -> tuple[bool | None, str]:
    """Detect whether the agent-notes skills are present in either harness."""
    try:
        from agent_notes.cli.skills import _discover_skills, _repo_skills_root

        repo_skills = {p.parent.name for p in _discover_skills(_repo_skills_root())}
    except Exception:
        # Source unreadable: we cannot determine state, so skip (not a pass
        # that hides a real gap, and not a fail that false-alarms an editable
        # install whose repo root is elsewhere).
        return None, "skills source unreadable (informational)"
    # (install_dir, is_claude_layout) — the layout differs per harness.
    layouts = [
        (Path.home() / ".claude" / "skills", True),
        (Path.home() / ".config" / "opencode" / "command", False),
        (Path.home() / ".hermes" / "skills", True),
        # Codex user-scoped shared skills: $HOME/.agents/skills (suite contract
        # §2 Codex; Plan 019 Decision 2 restored 2026-07-18). SKILL.md layout.
        (Path.home() / ".agents" / "skills", True),
    ]
    found: list[str] = []
    for install_dir, is_claude in layouts:
        for name in repo_skills:
            target = install_dir / name / "SKILL.md" if is_claude else install_dir / f"{name}.md"
            if target.exists():
                found.append(name)
    installed = sorted(set(found))
    if not installed:
        if codex_plugin_wired:
            return True, "skills provided by the enabled agent-notes Codex plugin"
        return False, "no skills installed (run 'agent-notes install-harness <harness>')"
    return True, f"{len(installed)} skill(s) installed: {', '.join(installed)}"


def _check_harness_wired(*, codex_plugin_wired: bool = False) -> tuple[bool, str]:
    """Detect whether install-harness left a manifest in either harness config."""
    from agent_notes.cli.harness import _codex_home

    manifests = [
        Path.home() / ".claude" / ".agent-notes-harness.json",
        Path.home() / ".config" / "opencode" / ".agent-notes-harness.json",
        Path.home() / ".hermes" / ".agent-notes-harness.json",
        _codex_home() / ".agent-notes-harness.json",
    ]
    wired = [str(p) for p in manifests if p.exists()]
    if not wired:
        if codex_plugin_wired:
            return True, "harness wired by the enabled agent-notes Codex plugin"
        return False, "no harness manifest found (run 'agent-notes install-harness <harness>')"
    return True, f"harness wired: {', '.join(wired)}"


def _check_codex_harness(*, plugin_probe: tuple[str, str] | None = None) -> tuple[bool | None, str]:
    """Report plugin/direct/duplicate/stale Codex lifecycle states.

    The generic skills/manifest checks above answer whether *any* harness is
    installed. They cannot prove Codex readiness: a healthy Claude install
    must not hide a requested Codex install whose shared skills are missing or
    locally modified. A Codex ownership manifest is the request/configuration
    signal for this component-owned fallback installer. When it is absent the
    check is informational (the suite may instead own plugin installation);
    once present, every canonical skill and recorded hook-group hash must agree.
    Hook trust itself is intentionally named ``trust-unverified``: Codex exposes
    review through interactive ``/hooks`` but no machine-readable trust probe.
    """
    from agent_notes.cli.harness import (
        _harness_paths,
        _hook_group_has_command,
        _json_hash,
        _load_json,
        _normalize_hash_map,
        _normalize_hook_hashes,
        _read_manifest,
        _sha256_file,
    )
    from agent_notes.cli.skills import _discover_skills, _repo_skills_root
    from agent_notes.codex_lifecycle import canonical_hook_groups

    paths = _harness_paths("codex")
    manifest_path = paths["manifest"]
    plugin_state, plugin_detail = (
        plugin_probe if plugin_probe is not None else _probe_codex_agent_notes_plugin()
    )
    if not manifest_path.is_file():
        if plugin_state == "enabled":
            return True, "codex: plugin wired (trust-unverified via /hooks)"
        if plugin_state == "disabled":
            return False, "codex: plugin disabled (trust-unverified via /hooks)"
        if plugin_state == "stale":
            return False, f"codex: plugin stale ({plugin_detail}; trust-unverified via /hooks)"
        if plugin_state == "unknown":
            return None, (
                f"codex: absent (no direct manifest; plugin state unavailable: {plugin_detail})"
            )
        return None, "codex: absent (no plugin and no direct-install ownership manifest)"

    try:
        manifest = _read_manifest(manifest_path)
    except RuntimeError as exc:
        return False, f"codex: stale (unreadable manifest: {type(exc).__name__})"

    if manifest.get("harness") != "codex":
        return False, "codex: stale (ownership manifest names a different harness)"

    try:
        sources = {path.parent.name: path for path in _discover_skills(_repo_skills_root())}
    except Exception as exc:
        return False, f"codex: stale (canonical skills unreadable: {type(exc).__name__})"
    if not sources:
        return False, "codex: stale (canonical skill assets are missing)"

    recorded = _normalize_hash_map(manifest.get("skills"))
    if not recorded:
        return False, "codex: stale (manifest records no owned skills)"

    problems: list[str] = []
    for name, source in sorted(sources.items()):
        recorded_hash = recorded.get(name)
        target = paths["skills_dest"] / name / "SKILL.md"
        if name not in recorded:
            problems.append(f"{name}: untracked")
            continue
        if recorded_hash is None:
            problems.append(f"{name}: legacy manifest hash missing")
            continue
        if not target.is_file():
            problems.append(f"{name}: installed file missing")
            continue
        target_hash = _sha256_file(target)
        source_hash = _sha256_file(source)
        if target_hash != recorded_hash:
            problems.append(f"{name}: installed file modified")
        elif source_hash != recorded_hash:
            problems.append(f"{name}: installed version outdated")

    recorded_hooks = _normalize_hook_hashes(manifest.get("hooks"))
    canonical = canonical_hook_groups()
    if set(recorded_hooks) != set(canonical):
        missing = sorted(set(canonical) - set(recorded_hooks))
        extra = sorted(set(recorded_hooks) - set(canonical))
        if missing:
            problems.append("hooks untracked: " + ", ".join(missing))
        if extra:
            problems.append("unknown hook ownership: " + ", ".join(extra))
    try:
        hook_document = _load_json(paths["hooks"])
    except RuntimeError as exc:
        problems.append(f"hooks unreadable: {type(exc).__name__}")
        hook_document = {}
    live_hooks = hook_document.get("hooks", {})
    if not isinstance(live_hooks, dict):
        problems.append("hooks document has a non-object hooks value")
        live_hooks = {}
    for event, group in canonical.items():
        expected_hash = recorded_hooks.get(event)
        command = group["hooks"][0]["command"]
        groups = live_hooks.get(event, [])
        candidates = (
            [g for g in groups if _hook_group_has_command(g, str(command))]
            if isinstance(groups, list)
            else []
        )
        if len(candidates) == 0:
            problems.append(f"{event} hook missing")
        elif len(candidates) > 1:
            problems.append(f"{event} hook duplicated")
        elif expected_hash is None:
            problems.append(f"{event} hook ownership hash missing")
        elif _json_hash(candidates[0]) != expected_hash:
            problems.append(f"{event} hook modified")

    if problems:
        return False, "codex: stale (" + "; ".join(problems) + ")"
    if plugin_state in {"enabled", "stale"}:
        return False, (
            "codex: duplicate (plugin and direct lifecycle hooks configured; "
            "remove the direct fallback; trust-unverified via /hooks)"
        )
    if plugin_state == "unknown":
        return False, (
            "codex: direct wired but plugin state unavailable "
            f"({plugin_detail}; trust-unverified via /hooks)"
        )
    return True, (
        f"codex: direct wired ({len(sources)} canonical skills and lifecycle hooks current; "
        "trust-unverified via /hooks)"
    )


def _probe_codex_agent_notes_plugin() -> tuple[str, str]:
    """Return enabled/disabled/stale/absent/unknown from Codex's CLI probe."""
    import importlib.metadata
    import shutil
    import subprocess

    codex = shutil.which("codex")
    if codex is None:
        return "absent", "Codex CLI not installed"
    # An unfiltered list can wait on unrelated remote marketplaces. Query the
    # configured agent-notes sources directly so local readiness remains local.
    import tomllib

    from agent_notes.cli.harness import _codex_home

    try:
        config = tomllib.loads((_codex_home() / "config.toml").read_text(encoding="utf-8"))
        plugins = config.get("plugins", {})
        markets = (
            sorted(
                key.split("@", 1)[1]
                for key in plugins
                if isinstance(key, str) and key.startswith("agent-notes@")
            )
            if isinstance(plugins, dict)
            else []
        )
    except (OSError, ValueError):
        markets = []
    installed: list[object] = []
    try:
        for market in markets or [None]:
            # The resolved path: on Windows ``codex`` is an npm ``.cmd`` shim
            # that subprocess can't launch by its bare name.
            command = [codex, "plugin", "list", "--json"]
            if market is not None:
                command.extend(["--marketplace", market])
            completed = subprocess.run(command, capture_output=True, text=True, timeout=15)
            if completed.returncode != 0:
                return "unknown", "codex plugin list failed"
            try:
                payload = json.loads(completed.stdout)
            except ValueError:
                return "unknown", "codex plugin list emitted invalid JSON"
            entries = payload.get("installed") if isinstance(payload, dict) else None
            if not isinstance(entries, list):
                return "unknown", "codex plugin list omitted installed entries"
            installed.extend(entries)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "unknown", type(exc).__name__
    matches = [
        entry
        for entry in installed
        if isinstance(entry, dict) and entry.get("name") == "agent-notes"
    ]
    if not matches:
        return "absent", "agent-notes plugin not installed"
    enabled = [entry for entry in matches if entry.get("enabled") is not False]
    if enabled:
        try:
            expected_version = importlib.metadata.version("agent-notes-hraedon")
        except importlib.metadata.PackageNotFoundError:
            expected_version = "1.1.0"
        versions = {entry.get("version") for entry in enabled}
        if versions != {expected_version}:
            return (
                "stale",
                f"installed version(s) {sorted(str(v) for v in versions)}; "
                f"component version {expected_version}",
            )
        return "enabled", "agent-notes plugin installed and enabled"
    return "disabled", "agent-notes plugin installed but disabled"


def _check_memory_provider() -> tuple[bool | None, str]:
    """Check the memory engine setting (Plan 026 D6).

    Native is the only engine and its health is covered by ``dsn_reachable``
    (it uses the same pgvector database), so a native engine reports as
    ``skip``. A setting that names any other engine is a failure: it is a
    retired engine, and recall refuses it rather than silently falling back.
    """
    try:
        from agent_notes.core.memory_engine import get_engine

        engine = get_engine()
        state = engine.describe().state.value
        return None, f"{engine.engine_name} engine (state={state}; covered by dsn_reachable)"
    except ValueError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"memory provider check error: {type(exc).__name__}"


# Known console scripts shipped by this package (from pyproject.toml).
_KNOWN_SCRIPTS = {
    "agent-notes",
    "agent-notes-setup",
    "agent-notes-migrate",
    "agent-notes-import-reflections",
    "agent-notes-doctor",
    "agent-notes-bridge",
    "agent-notes-trigger-loop",
    "agent-notes-web",
}

# Harness config files to probe for stale MCP entries.
_HARNESS_CONFIGS = [
    Path.home() / ".claude.json",
    Path.home() / ".config" / "opencode" / "opencode.json",
]


def _extract_mcp_commands(config: dict) -> list[str]:
    """Extract command strings from MCP server entries in a harness config."""
    commands = []
    # Claude Code format: { "mcpServers": { "name": { "command": "...", ... } } }
    mcp_servers = config.get("mcpServers", {})
    if isinstance(mcp_servers, dict):
        for server_cfg in mcp_servers.values():
            if isinstance(server_cfg, dict) and "command" in server_cfg:
                commands.append(str(server_cfg["command"]))

    # OpenCode format: { "mcp": { "servers": { "name": { "command": "...", ... } } } }
    mcp = config.get("mcp", {})
    if isinstance(mcp, dict):
        servers = mcp.get("servers", {})
        if isinstance(servers, dict):
            for server_cfg in servers.values():
                if isinstance(server_cfg, dict) and "command" in server_cfg:
                    commands.append(str(server_cfg["command"]))

    return commands


def _check_harness_configs() -> tuple[bool, str]:
    """Detect stale MCP entries referencing removed agent-notes console scripts."""
    stale_entries: list[str] = []

    for config_path in _HARNESS_CONFIGS:
        if not config_path.exists():
            continue
        try:
            text = config_path.read_text(encoding="utf-8")
            config = json.loads(text)
        except (OSError, json.JSONDecodeError):
            continue

        commands = _extract_mcp_commands(config)
        for cmd in commands:
            cmd_name = Path(cmd).name
            # Check if the command looks like an agent-notes script but isn't
            # in the known set. Also check if it references a path that doesn't exist.
            if "agent-notes" in cmd_name:
                if cmd_name not in _KNOWN_SCRIPTS:
                    stale_entries.append(f"{config_path}: {cmd_name}")
                elif Path(cmd).is_absolute() and not Path(cmd).exists():
                    stale_entries.append(f"{config_path}: {cmd} (not on disk)")

    if stale_entries:
        return False, "Stale MCP entries found:\n    " + "\n    ".join(stale_entries)
    return True, "No stale MCP entries in harness configs"


def _status_of(ok: bool | None) -> str:
    """Map a check result to a suite status string.

    ``True`` -> ``ok``, ``False`` -> ``fail``, ``None`` -> ``skip`` (the check
    does not apply to this deployment shape — e.g. the embedding check without
    ``--check-embed``). A ``skip`` never fails the suite-doctor umbrella. The
    ``ok``/``fail``/``skip`` vocabulary matches the suite doctor contract
    (blueprint §2.4).
    """
    if ok is None:
        return "skip"
    return "ok" if ok else "fail"


def run_json(check_embed: bool = False) -> tuple[dict, int]:
    """Run all checks and return the suite-shape health object + exit code.

    Emits the contract shape defined in Plan 017 WI-3.1 / blueprint §2.4::

        {
          "component": "agent-notes",
          "version": "<dist-version>",
          "ok": bool,          # umbrella-read: false only when unhealthy
          "degraded": false,   # kept for the umbrella contract; never true now
          "status": "healthy" | "unhealthy",
          "checks": [{"name": ..., "status": ..., "detail": ...}, ...]
        }

    ``status`` is ``healthy`` when every check passed or was skipped and
    ``unhealthy`` when any check failed. The ``regista`` block and the
    ``degraded`` status are gone (Plan 026 D1): regista write-through was
    deleted, so an absent regista is the normal state and reports nothing.
    """
    checks: list[dict] = []
    failed = False

    def add(name: str, ok: bool | None, detail: str) -> None:
        nonlocal failed
        status = _status_of(ok)
        if status == "fail":
            failed = True
        checks.append({"name": name, "status": status, "detail": detail})

    # --- native store checks (always run; the native op_log is the floor) ---
    ok, msg = _check_dsn()
    add("dsn_reachable", ok, msg)
    dsn_ok = bool(ok)

    ok, msg = _check_schema()
    add("schema_up_to_date", ok, msg)
    schema_ok = bool(ok)

    ok, msg = _check_coordination_mode()
    add("coordination_mode", ok, msg)
    vm_status, vm_msg = _check_vm_identity()
    checks.append({"name": "vm_identity", "status": vm_status, "detail": vm_msg})

    ok, msg = _check_retired_settings()
    add("retired_settings", ok, msg)

    if dsn_ok and schema_ok:
        if check_embed:
            ok, msg = _check_embedding()
            add("embedding_model", ok, msg)
        else:
            add("embedding_model", None, "skipped (use --check-embed)")

        ok, msg = _check_links_audit()
        add("links_audit", ok, msg)

        ok, msg = _check_vocab_integrity()
        add("vocabulary_integrity", ok, msg)

        ok, msg = _check_bridge_target()
        add("bridge_target", ok, msg)

        ok, msg = _check_harness_configs()
        add("stale_mcp_entries", ok, msg)

        # agent-notes owns its op_log chain; verify it whenever the native
        # store is up.
        chain_ok, chain_msg = _check_chain_ok()
        add("chain_integrity", chain_ok, chain_msg)
    else:
        for name in (
            "embedding_model",
            "links_audit",
            "vocabulary_integrity",
            "bridge_target",
            "stale_mcp_entries",
            "chain_integrity",
        ):
            add(name, None, "skipped (prerequisite dsn_reachable/schema_up_to_date failed)")

    # --- suite-layer checks (run regardless of native DB reachability) ---
    plugin_probe = _probe_codex_agent_notes_plugin()
    codex_ok, codex_msg = _check_codex_harness(plugin_probe=plugin_probe)
    plugin_wired = codex_ok is True and plugin_probe[0] == "enabled"
    ok, msg = _check_skills_installed(codex_plugin_wired=plugin_wired)
    add("skills_installed", ok, msg)

    ok, msg = _check_harness_wired(codex_plugin_wired=plugin_wired)
    add("harness_wired", ok, msg)

    add("codex_harness", codex_ok, codex_msg)

    ok, msg = _check_memory_provider()
    add("memory_provider", ok, msg)

    ok, msg = _check_legacy_outbox()
    add("legacy_outbox", ok, msg)

    overall = "unhealthy" if failed else "healthy"

    payload = {
        "component": "agent-notes",
        "version": _component_version(),
        # The suite-doctor umbrella classifies a component from the top-level
        # ``ok``/``degraded`` booleans (blueprint §2.4 / bootstrap-contract §3);
        # ``status`` is kept as the richer human-facing tri-state.
        "ok": overall != "unhealthy",
        "degraded": False,
        "status": overall,
        "checks": checks,
    }
    return payload, (1 if failed else 0)


def run(skip_embed: bool = False, check_embed: bool = False) -> int:
    """Run all checks and print a summary. Returns exit code.

    Args:
        skip_embed: Deprecated; kept for backward compatibility (no-op).
        check_embed: If True, run the embedding model check (~270MB load).
    """
    print("agent-notes-doctor — health check\n")

    all_ok = True

    ok, msg = _check_dsn()
    _print_section("1. DSN Reachable")
    _print_result(ok, msg)
    all_ok = all_ok and ok
    dsn_ok = ok

    ok, msg = _check_schema()
    _print_section("2. Schema Up to Date")
    _print_result(ok, msg)
    all_ok = all_ok and ok
    schema_ok = ok

    ok, msg = _check_coordination_mode()
    _print_section("3. Coordination Mode")
    _print_result(ok, msg)
    all_ok = all_ok and ok

    vm_status, vm_msg = _check_vm_identity()
    _print_section("VM Identity")
    print(f"  {vm_status.upper()}: {vm_msg}")

    ok, msg = _check_retired_settings()
    _print_section("Retired Settings")
    _print_result(ok, msg)
    all_ok = all_ok and ok

    if not (dsn_ok and schema_ok):
        for name in (
            "4. Embedding Model",
            "5. Links Audit",
            "6. Vocabulary Integrity",
            "7. Bridge Target",
            "8. Harness Configs",
        ):
            _print_section(name)
            print("  SKIPPED: prerequisite check(s) failed (DSN / Schema)")
        _print_section("Summary")
        print("One or more prerequisite checks failed.")
        return 1

    if check_embed:
        ok, msg = _check_embedding()
        _print_section("4. Embedding Model")
        _print_result(ok, msg)
        all_ok = all_ok and ok
    else:
        _print_section("4. Embedding Model")
        print("  SKIPPED: use --check-embed to verify (~270MB model load)")

    ok, msg = _check_links_audit()
    _print_section("5. Links Audit")
    _print_result(ok, msg)
    all_ok = all_ok and ok

    ok, msg = _check_vocab_integrity()
    _print_section("6. Vocabulary Integrity")
    _print_result(ok, msg)
    all_ok = all_ok and ok

    ok, msg = _check_bridge_target()
    _print_section("7. Bridge Target")
    _print_result(ok, msg)
    all_ok = all_ok and ok

    ok, msg = _check_harness_configs()
    _print_section("8. Harness Configs")
    _print_result(ok, msg)
    all_ok = all_ok and ok

    # Suite-layer checks (Plan 017 WI-3.1) — same surface as `doctor --json`,
    # so the human-readable report does not silently miss a gap the JSON
    # umbrella would catch.
    ok, msg = _check_chain_ok()
    _print_section("9. Op-Log Chain Integrity")
    if ok is None:
        print(f"  SKIP: {msg}")
    else:
        _print_result(ok, msg)
        all_ok = all_ok and ok

    plugin_probe = _probe_codex_agent_notes_plugin()
    codex_ok, codex_msg = _check_codex_harness(plugin_probe=plugin_probe)
    plugin_wired = codex_ok is True and plugin_probe[0] == "enabled"
    ok, msg = _check_skills_installed(codex_plugin_wired=plugin_wired)
    _print_section("10. Skills Installed")
    if ok is None:
        print(f"  SKIP: {msg}")
    else:
        _print_result(ok, msg)
        all_ok = all_ok and ok

    ok, msg = _check_harness_wired(codex_plugin_wired=plugin_wired)
    _print_section("11. Harness Wired")
    _print_result(ok, msg)
    all_ok = all_ok and ok

    ok, msg = codex_ok, codex_msg
    _print_section("12. Codex Harness")
    if ok is None:
        print(f"  SKIP: {msg}")
    else:
        _print_result(ok, msg)
        all_ok = all_ok and ok

    ok, msg = _check_memory_provider()
    _print_section("13. Memory Provider")
    if ok is None:
        print(f"  SKIP: {msg}")
    else:
        _print_result(ok, msg)
        all_ok = all_ok and ok

    ok, msg = _check_legacy_outbox()
    _print_section("14. Legacy Outbox")
    _print_result(ok, msg)
    all_ok = all_ok and ok

    _print_section("Summary")
    if all_ok:
        print("All checks passed.")
        return 0
    else:
        print("One or more checks failed.")
        return 1


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="agent-notes health check")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the suite-shape health object (Plan 017 WI-3.1) and exit.",
    )
    parser.add_argument(
        "--skip-embed",
        action="store_true",
        help="Deprecated; embedding check is now opt-in via --check-embed",
    )
    parser.add_argument(
        "--check-embed",
        action="store_true",
        help="Run embedding model check (~270MB model load, ~30s on first run)",
    )
    args = parser.parse_args()
    if args.json:
        payload, code = run_json(check_embed=args.check_embed)
        print(json.dumps(payload, indent=2, default=str))
        sys.exit(code)
    sys.exit(run(skip_embed=args.skip_embed, check_embed=args.check_embed))


if __name__ == "__main__":
    main()
