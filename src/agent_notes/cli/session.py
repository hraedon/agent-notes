"""``agent-notes session`` — session-scoped identity records (WI-067).

The session record is the correct shape for lineage on multi-agent hosts: a
host-wide value declares one model for every session on that host, which is
false for most sessions and *worse* than declaring nothing (a wrong lineage
passes a same-lineage review as cross-lineage — fail-open). A record keyed by
the harness session id is correct by construction for the common case, and
concurrent sessions on one host do not collide because the key is the session
id.

``session declare`` writes the record (once per session, via ``/start`` or a
SessionStart hook); ``session status`` reads back what currently resolves.
Both honor the precedence chain: session record (once declared) > explicit
``--model-lineage`` > process env > per-user suite.env > system suite.env.

Once a session has declared, the record is the **stable source**: declaring a
*different* lineage is refused (a session cannot relabel itself mid-session to
manufacture cross-lineage independence). Re-declaring the same value is
idempotent.
"""

from __future__ import annotations

import argparse
import json

from agent_notes.cli.common import (
    EXIT_CONFLICT,
    EXIT_GENERIC,
    EXIT_NOT_CONFIGURED,
    EXIT_SUCCESS,
    emit_error,
)


def _session_id_or_error(
    use_json: bool,
    explicit: str | None = None,
) -> str | None:
    """Resolve the session id: an explicit ``--session-id`` wins; otherwise the
    harness session id is read from the environment.

    The explicit flag is the safe mechanism for harnesses that cannot export
    their session id to tool subprocesses (opencode): the agent names the
    session it is running in, per invocation, without any process-global state.
    """
    if explicit:
        return explicit
    from agent_notes.core.session_identity import harness_session_id

    session_id = harness_session_id()
    if not session_id:
        emit_error(
            "NO_SESSION_ID",
            "no harness session id is resolvable. Session-scoped identity is "
            "keyed by the harness session id (CLAUDE_CODE_SESSION_ID / "
            "OPENCODE_SESSION_ID / CODEX_SESSION_ID). If your harness cannot "
            "export a session id to tool subprocesses (e.g. opencode), pass "
            "the session id explicitly with --session-id.",
            use_json=use_json,
            exit_code=EXIT_NOT_CONFIGURED,
        )
        return None
    return session_id


def _canonical_families_or_error(use_json: bool) -> tuple[str, ...] | None:
    """Return canonical lineage families, or emit a contract error if regista
    (which owns the registry) is unavailable. Returns ``None`` only after an
    error was already emitted."""
    from agent_notes.core.session_identity import canonical_lineage_families

    families = canonical_lineage_families()
    if families is None:
        emit_error(
            "REGISTA_UNAVAILABLE",
            "cannot validate lineage families: the regista package (owner of "
            "the canonical lineage registry) is not importable. Install the "
            "pinned regista-hraedon dependency and retry.",
            use_json=use_json,
            exit_code=EXIT_GENERIC,
        )
        return None
    return families


def cmd_session_declare(args: argparse.Namespace) -> int:
    """Write the session-scoped identity record (WI-067).

    ``--model-lineage`` names the canonical family the session's agent belongs
    to. The value is validated against the canonical family registry so a
    misspelled or invented lineage is refused here (cheap) rather than at every
    subsequent regista write (expensive).

    Once a session has declared, the record is the stable source: declaring a
    *different* lineage is refused (fail-closed) so a session cannot relabel
    itself mid-session; re-declaring the same value is idempotent.
    """
    use_json = getattr(args, "json", False)
    session_id = _session_id_or_error(use_json, getattr(args, "session_id", None))
    if session_id is None:
        return EXIT_NOT_CONFIGURED

    lineage = getattr(args, "model_lineage", None)
    if not lineage:
        return emit_error(
            "INVALID_ARGUMENT",
            "--model-lineage is required: name the canonical family this "
            "session's agent belongs to (e.g. claude-opus, glm, qwen).",
            use_json=use_json,
            exit_code=EXIT_GENERIC,
        )

    families = _canonical_families_or_error(use_json)
    if families is None:
        return EXIT_GENERIC
    if lineage not in families:
        return emit_error(
            "INVALID_MODEL_LINEAGE",
            f"{lineage!r} is not a canonical lineage family. Allowed families: "
            f"{', '.join(families)}",
            use_json=use_json,
            exit_code=EXIT_GENERIC,
        )

    from agent_notes.core.session_identity import (
        MODEL_LINEAGE_ENV,
        read_session_record,
        write_session_record,
    )

    previous = read_session_record(session_id).get(MODEL_LINEAGE_ENV)
    if previous is not None and previous != lineage:
        return emit_error(
            "SESSION_LINEAGE_CONFLICT",
            f"session {session_id} has already declared lineage {previous!r}; "
            f"refusing to change it to {lineage!r}. A declared session record "
            "is the stable source for the cross-lineage review gate — changing "
            "it mid-session would let one session manufacture false "
            "independence. Re-declare the same value to confirm, or start a "
            "new session.",
            use_json=use_json,
            exit_code=EXIT_CONFLICT,
        )

    path = None
    try:
        path = write_session_record(session_id, {MODEL_LINEAGE_ENV: lineage})
    except OSError as exc:
        return emit_error(
            "SESSION_RECORD_WRITE_FAILED",
            f"could not write the session record for session {session_id}: {exc}",
            use_json=use_json,
            exit_code=EXIT_GENERIC,
        )

    if use_json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "session_id": session_id,
                    "model_lineage": lineage,
                    "record_path": str(path),
                },
                indent=2,
            )
        )
    else:
        changed = "" if previous == lineage else " (first declaration)"
        print(f"Session identity declared: {lineage} for session {session_id}{changed} -> {path}")
    return EXIT_SUCCESS


def cmd_session_status(args: argparse.Namespace) -> int:
    """Read back the resolved session identity (WI-067)."""
    use_json = getattr(args, "json", False)

    from agent_notes.core.session_identity import (
        harness_session_id,
        harness_session_source,
        read_session_record,
        resolve_model_lineage,
    )

    explicit_session = getattr(args, "session_id", None)
    session_id: str | None
    session_source: str | None
    if explicit_session:
        session_id = explicit_session
        session_source = "explicit"
    else:
        session_id = harness_session_id()
        session_source = harness_session_source()
    record = read_session_record(session_id) if session_id else {}
    try:
        lineage, source = resolve_model_lineage(session_id=session_id)
    except ValueError as exc:
        return emit_error(
            "SESSION_LINEAGE_CONFLICT",
            str(exc),
            use_json=use_json,
            exit_code=EXIT_CONFLICT,
        )
    families = _canonical_families_or_error(use_json)
    if families is None:
        # _canonical_families_or_error already emitted the REGISTA_UNAVAILABLE
        # contract error; do not also emit the status payload (that would put
        # two JSON documents on stdout).
        return EXIT_GENERIC
    canonical = lineage is not None and lineage in families

    payload = {
        "session_id": session_id,
        "session_source": session_source,
        "session_record": record,
        "model_lineage": lineage,
        "source": source,
        "canonical": canonical,
        "resolvable": lineage is not None and canonical,
    }
    if use_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        if not session_id:
            print("Session: (no harness session id resolvable)")
        else:
            print(f"Session: {session_id}")
            print(f"  source: {session_source or '(none)'}")
            print(f"  record: {record if record else '(none)'}")
        print(
            f"Lineage: {lineage or '(undeclared)'} "
            f"[source={source or 'none'}, canonical={canonical}]"
        )
    return EXIT_SUCCESS


def register_session_parsers(sub: argparse._SubParsersAction) -> None:
    session = sub.add_parser(
        "session",
        help="Session-scoped identity records (WI-067)",
    )
    session_sub = session.add_subparsers(dest="session_cmd")

    declare = session_sub.add_parser("declare", help="Declare this session's model lineage")
    declare.add_argument(
        "--model-lineage",
        default=None,
        help="Canonical model lineage family for this session (required)",
    )
    declare.add_argument(
        "--session-id",
        default=None,
        help="Explicit session id (harnesses that cannot export their session "
        "id to tool subprocesses, e.g. opencode, must name it explicitly)",
    )
    declare.add_argument("--json", action="store_true", help="Emit JSON instead of human text")
    declare.set_defaults(func=cmd_session_declare)

    status = session_sub.add_parser("status", help="Show the resolved session identity")
    status.add_argument(
        "--session-id",
        default=None,
        help="Explicit session id (harnesses that cannot export their session "
        "id to tool subprocesses, e.g. opencode, must name it explicitly)",
    )
    status.add_argument("--json", action="store_true", help="Emit JSON instead of human text")
    status.set_defaults(func=cmd_session_status)

    session.set_defaults(func=lambda args: session.print_help())
