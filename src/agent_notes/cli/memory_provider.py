"""Memory-provider CLI subcommands (Plan 020 WI-3.1).

Exposes ``agent-notes memory-provider describe|configure|doctor`` so an
operator (or the suite doctor) can inspect the configured memory engine,
its capabilities, version, and health — without touching project-scoped
data.  The engine is global, not per-project, so these commands accept the
common args (for ``--json`` consistency) but do not require resolution.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any, assert_never

from agent_notes.cli.common import EXIT_GENERIC, EXIT_SUCCESS, _add_common, _print_sub_help


def _resolve_engine_source() -> str:
    """Determine how the memory engine was selected.

    Mirrors the precedence in ``memory_engine.get_engine()`` without
    instantiating the engine — the CLI needs the *source* for display, not
    the engine itself (it calls ``get_engine()`` separately for that).
    """
    if os.environ.get("AGENT_NOTES_MEMORY_ENGINE"):
        return "env"
    try:
        from agent_notes.core.suite_env import load_suite_env

        suite = load_suite_env()
        if suite.get("AGENT_NOTES_MEMORY_ENGINE"):
            return "suite_env"
    except Exception:
        pass
    try:
        from agent_notes.core.config import config_path

        path = config_path()
        if path.is_file():
            data = json.loads(path.read_text())
            if data.get("memory_engine"):
                return "config_file"
    except Exception:
        pass
    return "default"


def _redact_api_key(key: str | None) -> str | None:
    """Show only the first 4 characters plus ``***``.

    Enough to confirm *which* key is configured without exposing the
    secret value.  A key of 4 chars or fewer is fully masked.
    """
    if not key:
        return None
    if len(key) <= 4:
        return "***"
    return key[:4] + "***"


def _hindsight_config(engine: Any) -> dict[str, Any] | None:
    """Return the hindsight config block (redacted) if the engine is hindsight.

    Accesses private fields on the ``HindsightEngine`` instance — safe because
    we check ``engine_name`` first, and the CLI is in the same package.
    """
    if engine.engine_name != "hindsight":
        return None
    api_key: str | None = getattr(engine, "_api_key", None)
    return {
        "url": getattr(engine, "_url", None),
        "tenant": getattr(engine, "_tenant", "default"),
        "timeout": getattr(engine, "_timeout", 30),
        "api_key": _redact_api_key(api_key) if api_key else None,
    }


def cmd_memory_provider_describe(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    from agent_notes.core.memory_engine import get_engine

    try:
        engine = get_engine()
    except ValueError as exc:
        if use_json:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"Error: {exc}")
        return EXIT_GENERIC
    health = engine.describe()

    payload: dict[str, Any] = {
        "engine": engine.engine_name,
        "health": health.to_dict(),
    }

    if use_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        caps = ", ".join(sorted(c.value for c in health.capabilities))
        lines = [
            f"Engine: {engine.engine_name}",
            f"State: {health.state.value}",
            f"Version: {health.version or '(unknown)'}",
            f"Protocol: {health.protocol_version}",
            f"Capabilities: {caps}",
            f"Indexing Backlog: {health.indexing_backlog}",
            f"Indexing Freshness: {health.indexing_freshness or '(none)'}",
            f"Detail: {health.detail}",
        ]
        print("\n".join(lines))
    return EXIT_SUCCESS


def cmd_memory_provider_configure(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    from agent_notes.core.memory_engine import get_engine

    try:
        engine = get_engine()
    except ValueError as exc:
        if use_json:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"Error: {exc}")
        return EXIT_GENERIC
    configured_via = _resolve_engine_source()

    payload: dict[str, Any] = {
        "engine": engine.engine_name,
        "configured_via": configured_via,
    }

    hs_config = _hindsight_config(engine)
    if hs_config is not None:
        payload["hindsight"] = hs_config

    if use_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        lines = [
            f"Engine: {engine.engine_name}",
            f"Configured via: {configured_via}",
        ]
        if hs_config is not None:
            lines.append(f"URL: {hs_config['url']}")
            lines.append(f"Tenant: {hs_config['tenant']}")
            lines.append(f"Timeout: {hs_config['timeout']}")
            lines.append(f"API Key: {hs_config['api_key']}")
        print("\n".join(lines))
    return EXIT_SUCCESS


def cmd_memory_provider_doctor(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    from agent_notes.core.memory_engine import EngineHealthState, get_engine

    try:
        engine = get_engine()
    except ValueError as exc:
        if use_json:
            print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            print(f"Error: {exc}")
        return EXIT_GENERIC
    health = engine.describe()
    state = health.state

    match state:
        case EngineHealthState.HEALTHY:
            ok = True
            degraded = False
        case EngineHealthState.DEGRADED:
            ok = True
            degraded = True
        case EngineHealthState.UNREACHABLE:
            ok = False
            degraded = False
        case EngineHealthState.NOT_CONFIGURED:
            ok = False
            degraded = False
        case EngineHealthState.UNAVAILABLE:
            ok = False
            degraded = False
        case other:
            assert_never(other)

    payload: dict[str, Any] = {
        "ok": ok,
        "degraded": degraded,
        "engine": engine.engine_name,
        "state": state.value,
        "capabilities": sorted(c.value for c in health.capabilities),
        "version": health.version,
        "detail": health.detail,
        "indexing_backlog": health.indexing_backlog,
        "indexing_freshness": health.indexing_freshness,
    }

    if use_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        caps = ", ".join(sorted(c.value for c in health.capabilities))
        status = "OK" if ok else "FAIL"
        lines = [
            f"Memory Provider: {engine.engine_name}",
            f"State: {state.value}",
            status,
        ]
        if degraded:
            lines.append("Degraded: true")
        lines.extend([
            f"Version: {health.version or '(unknown)'}",
            f"Detail: {health.detail}",
            f"Capabilities: {caps}",
            f"Indexing Backlog: {health.indexing_backlog}",
            f"Indexing Freshness: {health.indexing_freshness or '(none)'}",
        ])
        print("\n".join(lines))

    return EXIT_SUCCESS if ok else EXIT_GENERIC


def register_memory_provider_parsers(sub: argparse._SubParsersAction) -> None:
    mp = sub.add_parser(
        "memory-provider", help="Memory provider engine operations"
    )
    mp_sub = mp.add_subparsers(dest="mem_prov_cmd")

    mp_describe = mp_sub.add_parser(
        "describe",
        help="Show the configured engine's name, capabilities, version, and health",
    )
    _add_common(mp_describe)
    mp_describe.set_defaults(func=cmd_memory_provider_describe)

    mp_configure = mp_sub.add_parser(
        "configure",
        help="Show current configuration (redacted)",
    )
    _add_common(mp_configure)
    mp_configure.set_defaults(func=cmd_memory_provider_configure)

    mp_doctor = mp_sub.add_parser(
        "doctor",
        help="Run the engine's health check",
    )
    _add_common(mp_doctor)
    mp_doctor.set_defaults(func=cmd_memory_provider_doctor)

    mp.set_defaults(func=lambda args: _print_sub_help(mp))
