"""Health-check script for agent-notes installations (Phase 6.3 + Plan 008 P4).

Checks:
1. DSN reachable
2. Schema up to date (core tables + kernel tables present)
3. Coordination mode (degrade contract — local-lease is default)
4. Embedding model loads (opt-in via --check-embed)
5. Links audit (dangling links)
6. Vocabulary integrity
7. Bridge target reachable
8. Stale MCP entries in harness configs

Exit code: 0 if all healthy, 1 if any check failed.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from agent_notes.core import config as reg_config
from agent_notes.core import outbox, projection
from agent_notes.core.db import _conn


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


def _check_coordination_mode() -> tuple[bool, str]:
    """Report the coordination mode (Plan 008 P4 degrade contract).

    Coordinator-absent / local-lease is the default safe mode.
    This check is always PASS because the absence of a coordinator is
    a normal, first-class state — not a failure.
    """
    try:
        from agent_notes.core.coordinator import get_coordination_mode

        mode = get_coordination_mode()
        return True, mode
    except Exception as exc:
        return False, _sanitize_conn_error(exc)


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


def _check_links_audit() -> tuple[bool, str]:
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

            cur.execute(
                """
                SELECT COUNT(*) AS cnt FROM links l
                LEFT JOIN memories m
                  ON m.project_id = l.to_project
                  AND m.name = l.to_identifier
                  AND m.active = true
                WHERE l.to_kind = 'memory' AND m.id IS NULL
                """
            )
            dangle_mem_to = cur.fetchone()["cnt"]

            cur.execute(
                """
                SELECT COUNT(*) AS cnt FROM links l
                LEFT JOIN memories m
                  ON m.project_id = l.from_project
                  AND m.name = l.from_identifier
                  AND m.active = true
                WHERE l.from_kind = 'memory' AND m.id IS NULL
                """
            )
            dangle_mem_from = cur.fetchone()["cnt"]

        total = dangle_wi_from + dangle_mem_to + dangle_mem_from
        if total:
            return (
                False,
                f"Dangling links: work_item-from={dangle_wi_from}, "
                f"memory-to={dangle_mem_to}, memory-from={dangle_mem_from}",
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
    """Return the installed agent-notes distribution version."""
    from importlib.metadata import version

    try:
        return version("agent-notes")
    except Exception:
        return "unknown"


def _check_regista_reachable(cfg: reg_config.RegistaConfig) -> tuple[bool | None, str]:
    """Probe the regista DSN. Returns (reachable, detail).

    ``reachable`` is ``None`` (not configured — degrade mode), ``True`` (connected),
    or ``False`` (configured but unreachable). Per Plan 017 WI-3.1 AC, an
    *unconfigured* regista is a clean state (coordinator-absent is the default
    safe mode), not a failure. A *configured-but-unreachable* regista is a real
    failure (the operator wired a store the face cannot reach).

    Honor ``cfg.require_ssl``: if the DSN does not already specify an sslmode,
    a connection that demands SSL is probed with ``sslmode=require`` so the
    reachability result reflects the configured security posture (not a
    silently-insecure handshake).
    """
    if not cfg.dsn:
        return None, "regista DSN not configured (native op_log path — coordinator-absent)"
    try:
        import psycopg

        from agent_notes.core import secrets as suite_secrets

        # Resolve a backend ref (env:/vault:/...) to the real DSN before probing
        # (Plan 017 WI-4.1) — cfg.dsn may hold the ref string, not the DSN.
        dsn = suite_secrets.resolve_dsn(cfg.dsn)
        if dsn is None:
            return None, "regista DSN not configured (native op_log path — coordinator-absent)"
        # Honor require_ssl without mutating the caller's DSN string parsing —
        # only inject sslmode if the DSN does not already carry one.
        if cfg.require_ssl and "sslmode=" not in dsn:
            sep = "&" if "?" in dsn else "?"
            dsn = f"{dsn}{sep}sslmode=require"
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
        return True, "regista DSN reachable"
    except Exception as exc:
        return False, f"regista DSN configured but unreachable ({_sanitize_conn_error(exc)})"


def _check_chain_ok() -> tuple[bool | None, str]:
    """Verify agent-notes' own op-log chain integrity.

    agent-notes owns an append-only ``op_log`` (Plan 008) that is the chain it is
    responsible for; when regista writes are on, this log is the source the
    write-through face replays into the spine. Verifying regista's *own* event
    log is regista's doctor job — agent-notes cannot reach it from here. So
    ``regista.chain_ok`` reports the integrity of the op-log the agent face owns
    (the chain that feeds the spine), not a re-verification of the spine's
    internal chain.

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


def _check_skills_installed() -> tuple[bool, str]:
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
    ]
    found: list[str] = []
    for install_dir, is_claude in layouts:
        for name in repo_skills:
            target = install_dir / name / "SKILL.md" if is_claude else install_dir / f"{name}.md"
            if target.exists():
                found.append(name)
    installed = sorted(set(found))
    if not installed:
        return False, "no skills installed (run 'agent-notes install-harness <harness>')"
    return True, f"{len(installed)} skill(s) installed: {', '.join(installed)}"


def _check_harness_wired() -> tuple[bool, str]:
    """Detect whether install-harness left a manifest in either harness config."""
    manifests = [
        Path.home() / ".claude" / ".agent-notes-harness.json",
        Path.home() / ".config" / "opencode" / ".agent-notes-harness.json",
        Path.home() / ".hermes" / ".agent-notes-harness.json",
    ]
    wired = [str(p) for p in manifests if p.exists()]
    if not wired:
        return False, "no harness manifest found (run 'agent-notes install-harness <harness>')"
    return True, f"harness wired: {', '.join(wired)}"


def _check_secrets_backend(cfg: reg_config.RegistaConfig) -> tuple[bool | None, str]:
    """Verify configured suite secret refs resolve (Plan 017 WI-4.1).

    Only meaningful when a backend ref (``env:``/``vault:``/``azure:``/``file:``)
    is configured for the regista DSN or signing key. A plaintext/file-path
    deployment (the default) has nothing to verify → returns ``None`` (skipped,
    not a failure). When a ref is present, this *contacts the backend* once to
    confirm the secret is reachable and the material parses — a custody check a
    regulated deployment wants before trusting writes. Failures surface the
    exception type only (the message may echo partial material).

    The key-set manifest, when materialized, leaves a 0600 temp file that is
    scrubbed at interpreter exit; this check does not leave persistent state.
    """
    from agent_notes.core import secrets as suite_secrets

    refs: list[tuple[str, str]] = []
    if cfg.dsn and suite_secrets.is_backend_ref(cfg.dsn):
        refs.append(("REGISTA_DSN", cfg.dsn))
    if cfg.hmac_key_path and suite_secrets.is_backend_ref(cfg.hmac_key_path):
        # Only remote refs (env/vault/azure) materialize a temp file; a file:
        # ref is read directly. is_backend_ref covers both, which is fine — we
        # resolve either way to confirm reachability.
        refs.append(("REGISTA_KEY_PATH", cfg.hmac_key_path))
    if not refs:
        return None, "no backend refs configured (plaintext/file path)"

    resolved = 0
    for label, ref in refs:
        try:
            if label == "REGISTA_DSN":
                suite_secrets.resolve_dsn(ref)
            else:
                path, cleanup = suite_secrets.materialize_key_manifest(ref)
                # A bare/file: path is returned unread — confirm it actually
                # exists so a missing manifest is a named failure, not a pass.
                if cleanup is None and path is not None:
                    if not Path(path).is_file():
                        raise FileNotFoundError(path)
                if cleanup is not None:
                    cleanup()
            resolved += 1
        except Exception as exc:
            return False, f"{label} ref unresolvable: {type(exc).__name__}"
    names = ", ".join(label for label, _ in refs)
    return True, f"{resolved} backend ref(s) resolved ({names})"


def _check_regista_face() -> tuple[bool, str]:
    try:
        cfg = reg_config.regista_config()
        if not cfg.enabled:
            return True, "regista writes disabled (legacy op_log path)"
        pending = outbox.count_ops(cfg.project)
        conflicts = outbox.count_sidecar(cfg.project, "conflicts.jsonl")
        rejected = outbox.count_sidecar(cfg.project, "rejected.jsonl")
        with _conn() as conn:
            pending_rows = projection.count_pending(conn)
        return (
            True,
            f"enabled (project={cfg.project}); outbox pending={pending}, "
            f"conflicts={conflicts}, rejected={rejected}, "
            f"pending_sync rows={pending_rows}",
        )
    except Exception as exc:
        # An operational error (outbox unreadable, projection query failed,
        # DB unreachable) is a real failure — surface it rather than masking
        # the problem as a pass. Only the explicitly-disabled state above is a
        # clean pass.
        return False, f"regista face check error: {type(exc).__name__}"


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
    does not apply to this deployment shape — e.g. regista chain verify when
    writes are off). A ``skip`` never fails the suite-doctor umbrella. The
    ``ok``/``fail``/``skip`` vocabulary matches regista's canonical doctor
    contract (blueprint §2.4).
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
          "degraded": bool,    # umbrella-read: healthy-but-spine-absent
          "status": "healthy" | "degraded" | "unhealthy",
          "regista": {
            "reachable": true|false|null,
            "project": "<slug>",
            "writes_enabled": bool,
            "chain_ok": true|false|null,
            "mode": "<coordination mode>"
          },
          "checks": [{"name": ..., "status": ..., "detail": ...}, ...]
        }

    ``status`` is ``healthy`` when every check passed/skipped AND regista is
    reachable; ``degraded`` when no check failed but the spine is absent
    (coordinator-absent / regista DSN not configured) — degrade mode is a safe,
    fully-functional state, just not the full-suite posture, so the umbrella
    gets a distinct signal; ``unhealthy`` when any check failed. Per Plan 017
    WI-3.1 AC, degrade mode never makes the suite *unhealthy*.
    """
    checks: list[dict] = []
    failed = False

    def add(name: str, ok: bool | None, detail: str) -> None:
        nonlocal failed
        status = _status_of(ok)
        if status == "fail":
            failed = True
        checks.append({"name": name, "status": status, "detail": detail})

    cfg = reg_config.regista_config()

    # --- native store checks (always run; the native op_log is the floor) ---
    ok, msg = _check_dsn()
    add("dsn_reachable", ok, msg)
    dsn_ok = bool(ok)

    ok, msg = _check_schema()
    add("schema_up_to_date", ok, msg)
    schema_ok = bool(ok)

    ok, msg = _check_coordination_mode()
    add("coordination_mode", ok, msg)
    # Reuse the coordination-mode string for the regista block instead of
    # calling get_coordination_mode() a second time.
    mode = msg if ok else "unknown"

    chain_ok: bool | None = None
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

        ok, msg = _check_regista_face()
        add("regista_face", ok, msg)

        # agent-notes owns its op_log chain (the chain the write-through face
        # replays into regista); verify it whenever the native store is up. It
        # is independent of whether the regista face is wired.
        chain_ok, chain_msg = _check_chain_ok()
        add("chain_integrity", chain_ok, chain_msg)
    else:
        for name in (
            "embedding_model",
            "links_audit",
            "vocabulary_integrity",
            "bridge_target",
            "stale_mcp_entries",
            "regista_face",
            "chain_integrity",
        ):
            add(name, None, "skipped (prerequisite dsn_reachable/schema_up_to_date failed)")

    # --- suite-layer checks (run regardless of native DB reachability) ---
    ok, msg = _check_skills_installed()
    add("skills_installed", ok, msg)

    ok, msg = _check_harness_wired()
    add("harness_wired", ok, msg)

    ok, msg = _check_secrets_backend(cfg)
    add("secrets_backend", ok, msg)

    # --- regista block (the suite-shared facts) ---
    reachable, reach_msg = _check_regista_reachable(cfg)
    if reachable is False:
        # Configured but unreachable is a real failure.
        add("regista_reachable", False, reach_msg)
    elif reachable is None:
        add("regista_reachable", None, reach_msg)
    else:
        add("regista_reachable", True, reach_msg)

    if failed:
        overall = "unhealthy"
    elif reachable is None:
        # No check failed, but the spine is absent — degrade mode. Safe and
        # functional, but not the full-suite posture; give the umbrella a
        # distinct signal (Plan 017 WI-3.1 AC: not a failure).
        overall = "degraded"
    else:
        overall = "healthy"

    payload = {
        "component": "agent-notes",
        "version": _component_version(),
        # The suite-doctor umbrella classifies a component from the top-level
        # ``ok``/``degraded`` booleans (blueprint §2.4 / bootstrap-contract §3);
        # ``status`` is kept as the richer human-facing tri-state.
        "ok": overall != "unhealthy",
        "degraded": overall == "degraded",
        "status": overall,
        "regista": {
            "reachable": reachable,
            "project": cfg.project,
            "writes_enabled": cfg.writes_enabled,
            "chain_ok": chain_ok,
            "mode": mode,
        },
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

    ok, msg = _check_regista_face()
    _print_section("9. Regista Face")
    _print_result(ok, msg)
    all_ok = all_ok and ok

    # Suite-layer checks (Plan 017 WI-3.1) — same surface as `doctor --json`,
    # so the human-readable report does not silently miss a gap the JSON
    # umbrella would catch.
    ok, msg = _check_chain_ok()
    _print_section("10. Op-Log Chain Integrity")
    if ok is None:
        print(f"  SKIP: {msg}")
    else:
        _print_result(ok, msg)
        all_ok = all_ok and ok

    ok, msg = _check_skills_installed()
    _print_section("11. Skills Installed")
    if ok is None:
        print(f"  SKIP: {msg}")
    else:
        _print_result(ok, msg)
        all_ok = all_ok and ok

    ok, msg = _check_harness_wired()
    _print_section("12. Harness Wired")
    _print_result(ok, msg)
    all_ok = all_ok and ok

    cfg = reg_config.regista_config()
    reachable, reach_msg = _check_regista_reachable(cfg)
    _print_section("13. Regista Reachable")
    if reachable is None:
        print(f"  SKIP: {reach_msg}")
    else:
        _print_result(reachable, reach_msg)
        all_ok = all_ok and reachable

    ok, msg = _check_secrets_backend(cfg)
    _print_section("14. Secrets Backend")
    if ok is None:
        print(f"  SKIP: {msg}")
    else:
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
