from __future__ import annotations

import argparse
import base64
import json

from agent_notes.cli.common import (
    EXIT_GENERIC,
    EXIT_NOT_CONFIGURED,
    EXIT_SUCCESS,
    _add_common,
    _print_sub_help,
)


def cmd_verify(args: argparse.Namespace) -> int:
    """Standalone verifier CLI (Plan 008 P1).

    Exit codes:
    - 0: all ops verified (or only warnings)
    - 1: verification errors found
    - 2: not configured (no DB)
    """
    use_json = getattr(args, "json", False)
    public_key: bytes | None = None

    if args.public_key:
        try:
            public_key = base64.b64decode(args.public_key)
        except Exception as exc:
            if use_json:
                print(json.dumps({"error": f"Invalid --public-key: {exc}"}, indent=2))
            else:
                print(f"Error: invalid --public-key: {exc}")
            return EXIT_GENERIC

    from agent_notes.core.verifier import (
        verify_all,
        verify_cache,
        verify_entity,
        verify_gate_integrity,
    )

    check_cache = getattr(args, "check_cache", False)
    check_gate = getattr(args, "check_gate", False)

    try:
        if args.entity_id:
            result = verify_entity(
                entity_id=args.entity_id,
                public_key=public_key,
                check_policy=not args.no_policy,
            )
        else:
            result = verify_all(
                public_key=public_key,
                check_policy=not args.no_policy,
                entity_type=args.entity_type,
            )

        if check_cache:
            # Verify the work_items cache matches the op-log fold, and merge
            # into the op-level result so a single report covers both.
            cache_result = verify_cache(entity_id=args.entity_id)
            result.checked += cache_result.checked
            result.passed += cache_result.passed
            result.failed += cache_result.failed
            result.violations.extend(cache_result.violations)

        if check_gate:
            # Plan 014 WI-3: surface terminal items completed without the
            # cross-lineage review gate (degraded / unverified completions).
            # These are warnings, not errors — force-close is a legitimate
            # admin action; the detector makes the bypass legible.
            gate_result = verify_gate_integrity(entity_id=args.entity_id)
            result.checked += gate_result.checked
            result.passed += gate_result.passed
            result.failed += gate_result.failed
            result.violations.extend(gate_result.violations)
    except RuntimeError as exc:
        if use_json:
            print(json.dumps({"error": str(exc)}, indent=2))
        else:
            print(f"Error: {exc}")
        return EXIT_NOT_CONFIGURED

    if use_json:
        output = {
            "ok": result.ok(),
            "checked": result.checked,
            "passed": result.passed,
            "failed": result.failed,
            "violations": [
                {
                    "op_id": v.op_id,
                    "rule": v.rule,
                    "message": v.message,
                    "severity": v.severity,
                }
                for v in result.violations
            ],
        }
        print(json.dumps(output, indent=2))
    else:
        print(f"Verified {result.checked} ops: {result.passed} passed, {result.failed} failed")
        if result.violations:
            print()
            for v in result.violations:
                prefix = "⚠" if v.severity == "warning" else "✗"
                print(f"{prefix} [{v.op_id[:16]}…] {v.rule}: {v.message}")
        else:
            print("All checks passed.")

    return EXIT_SUCCESS if result.ok() else EXIT_GENERIC


def register_verify_parsers(sub: argparse._SubParsersAction) -> None:
    verify = sub.add_parser(
        "verify",
        help="Verify op-log integrity, signatures, and policy (Plan 008 P1)",
    )
    verify_sub = verify.add_subparsers(dest="verify_cmd")

    v_run = verify_sub.add_parser("run", help="Run the verifier")
    v_run.add_argument(
        "--entity-id",
        default=None,
        dest="entity_id",
        help="Verify only ops for this entity_id",
    )
    v_run.add_argument(
        "--entity-type",
        default=None,
        dest="entity_type",
        choices=["work_item", "memory", "link"],
        help="Filter to a specific entity type",
    )
    v_run.add_argument(
        "--public-key",
        default=None,
        dest="public_key",
        help="Base64-encoded Ed25519 public key for signature verification",
    )
    v_run.add_argument(
        "--no-policy",
        action="store_true",
        dest="no_policy",
        help="Skip built-in policy checks",
    )
    v_run.add_argument(
        "--check-cache",
        action="store_true",
        dest="check_cache",
        help="Also verify that the work_items cache matches the op-log fold",
    )
    v_run.add_argument(
        "--check-gate",
        action="store_true",
        dest="check_gate",
        help="Also flag terminal items completed without the review gate (Plan 014 WI-3)",
    )
    _add_common(v_run)
    v_run.set_defaults(func=cmd_verify)

    verify.set_defaults(func=lambda args: (_print_sub_help(verify), EXIT_SUCCESS)[1])
