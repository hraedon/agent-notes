"""``agent-notes invariants`` — read-only evidentiary measurements (WI-067).

Mirrors the regista/cairn ``invariants probe`` split: each component owns the
measurements of its own surface. agent-notes owns the session-scoped identity
measurement — whether a model lineage resolves for the *current session*
through the session-identity precedence chain. agent-suite aggregates the
component probes with ``agent-suite invariant-probes`` and applies the genesis
gate against the emitted check ids.
"""

from __future__ import annotations

import argparse
import json

from agent_notes.cli.common import EXIT_GENERIC, EXIT_SUCCESS


def cmd_invariants_probe(args: argparse.Namespace) -> int:
    """Measure session-identity resolvability (WI-067 / genesis gate).

    Emits the ``agent_notes.session_identity_resolvable`` check. Fail-closed:
    when no lineage resolves (no session record, no env declaration, no
    host-wide suite.env value), the check is ``fail`` and the process exits
    non-zero — a session that never declares reads as UNKNOWN, never as ok.
    """
    use_json = getattr(args, "json", False)

    from agent_notes.core.session_identity import session_identity_probe

    check = session_identity_probe()
    ok = check["status"] == "pass"
    report = {
        "component": "agent_notes",
        "probe_version": 1,
        "ok": ok,
        "checks": [check],
    }

    if use_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] agent_notes.session_identity_resolvable: {check['detail']}")
        if not ok:
            print(
                "  Identity is not session-resolvable. Run 'agent-notes session "
                "declare --model-lineage <family>' at session start (the /start "
                "skill does this), or set AGENT_NOTES_MODEL_LINEAGE for a "
                "single-model host."
            )
    return EXIT_SUCCESS if ok else EXIT_GENERIC


def register_invariants_parsers(sub: argparse._SubParsersAction) -> None:
    invariants = sub.add_parser(
        "invariants",
        help="Read-only evidentiary invariant measurements (WI-067)",
    )
    invariants_sub = invariants.add_subparsers(dest="invariants_cmd")
    probe = invariants_sub.add_parser("probe", help="Measure session-identity resolvability")
    probe.add_argument("--json", action="store_true", help="Emit JSON instead of human text")
    probe.set_defaults(func=cmd_invariants_probe)
    invariants.set_defaults(func=lambda args: invariants.print_help())
