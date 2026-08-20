"""``agent-notes invariants probe`` — the genesis-gate probe surface (WI-071).

Two contracts meet here, and both are load-bearing:

**The report contract** (``agent_suite.genesis_gate._parse_probe_result``). Under
``--json`` stdout must be exactly one UTF-8 JSON object with ``component``,
``ok``, ``probe_version: 1``, and ``checks``; the process must exit 0 or 1; and
``(exit == 0)`` must equal ``ok``. Any deviation is MALFORMED at the gate — a
verdict that says "agent-notes' probe is broken" rather than "this host's
identity does not resolve", which is strictly less useful. Two consequences
shape this module:

- **The probe never routes a failure through** :func:`agent_notes.cli.common.emit_error`.
  agent-notes' own taxonomy would render an undeclared lineage as an
  ``UNDECLARED_LINEAGE`` envelope with exit 3 (``EXIT_NOT_CONFIGURED``), and the
  CLI's ``_dispatch`` wrapper does exactly that for every other command. Both
  halves of that are contract violations here: exit 3 is not in ``(0, 1)`` and
  the envelope is not a probe report. ``invariant_probe_report()`` never raises,
  so ``_dispatch``'s handlers never see anything to translate — and
  ``tests/test_invariant_probe_cli.py`` pins that with an undeclared-lineage
  environment.
- Diagnostics go to **stderr** (the CLI configures logging to stderr at
  startup), so stdout stays a single parseable document.

**The parser-preflight contract** (``agent_suite.schedule._help_exposes_invariant_probe``).
``schedule install`` greps the child's ``--help`` for a usage line naming the
exact command, so ``invariants probe --help`` must print
``usage: agent-notes invariants probe ...``. argparse derives that from the
top-level ``prog="agent-notes"`` plus the two subparser names, which is why the
verb lives at ``invariants probe`` and not, say, ``probe --invariants``.
``tests/test_invariant_probe_cli.py`` re-implements the umbrella's regex against
the live help output rather than trusting the shape by inspection.
"""

from __future__ import annotations

import argparse
import json

from agent_notes.cli.common import EXIT_GENERIC, EXIT_SUCCESS, _print_sub_help


def _render_human(report: dict) -> None:
    print(f"component:     {report.get('component')}")
    print(f"probe_version: {report.get('probe_version')}")
    print(f"ok:            {report.get('ok')}")
    for check in report.get("checks", []):
        print()
        print(f"[{check.get('status')}] {check.get('id')}")
        print(f"  reason: {check.get('reason')}")
        print(f"  detail: {check.get('detail')}")
        for key, value in sorted((check.get("evidence") or {}).items()):
            print(f"    {key}: {value}")


def cmd_invariants_probe(args: argparse.Namespace) -> int:
    """Emit the read-only session-identity invariant report.

    Read-only: resolves identity, consults regista's lineage registry constant,
    and runs the boundary validation. It opens no database connection, builds no
    regista face, and writes nothing anywhere.
    """
    from agent_notes.core.invariant_probe import invariant_probe_report

    report = invariant_probe_report()
    if getattr(args, "json", False):
        # `sort_keys` is deliberately off: the top-level order (component,
        # probe_version, ok, checks) reads as a verdict. Compact-ish indent keeps
        # a hand-run report legible without breaking single-document framing.
        print(json.dumps(report, indent=2))
    else:
        _render_human(report)
    # The gate requires (exit == 0) == ok. Nothing else may set the exit code.
    return EXIT_SUCCESS if report.get("ok") else EXIT_GENERIC


def register_invariants_parsers(sub: argparse._SubParsersAction) -> None:
    invariants = sub.add_parser(
        "invariants",
        help="Read-only invariant measurements for the suite genesis gate",
    )
    invariants_sub = invariants.add_subparsers(dest="invariants_cmd")

    probe = invariants_sub.add_parser(
        "probe",
        help="Measure whether a work-item write issued now would carry a resolvable identity",
        description=(
            "Read-only probe of this environment's work-item write identity. "
            "Resolves the actor and model lineage through the same entry point "
            "every authored write uses and reports whether a write issued right "
            "now would carry a resolvable, valid identity. Writes nothing."
        ),
    )
    # Deliberately no --path/--workspace/--project (`_add_common`): the subject
    # is the process's session identity, which is project-independent, and
    # resolving a project would touch the database — this command must not.
    # Deliberately no --actor-id/--model-lineage either; see
    # core/invariant_probe.py for why the probe measures only the ambient
    # environment.
    probe.add_argument(
        "--json",
        action="store_true",
        help="Emit the probe report as a single JSON document (the gate contract)",
    )
    probe.set_defaults(func=cmd_invariants_probe)

    invariants.set_defaults(func=lambda args: _print_sub_help(invariants))
