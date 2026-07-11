"""Projection management CLI (Plan 009 P3)."""

from __future__ import annotations

import argparse
import json
import sys

from agent_notes.cli.common import (
    EXIT_NOT_CONFIGURED,
    EXIT_SUCCESS,
    _add_common,
    _resolve,
    report_resolution_failure,
)
from agent_notes.core import projection
from agent_notes.core.db import _conn
from agent_notes.core.face_factory import get_face


def cmd_projection_rebuild(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    face = get_face()
    if face is None:
        msg = "regista writes not enabled; set REGISTA_DSN and AGENT_NOTES_REGISTA_WRITES=1"
        if use_json:
            print(json.dumps({"error": msg}, indent=2))
        else:
            print(f"Error: {msg}", file=sys.stderr)
        return EXIT_NOT_CONFIGURED

    with _conn() as conn:
        report = projection.rebuild_from_regista(conn, face, project_id=proj_id)

    if use_json:
        print(json.dumps(report.__dict__, indent=2, default=str))
    else:
        print(
            f"Rebuilt projection from regista: {report.mirrored} mirrored, "
            f"{report.created} created, {report.skipped} skipped, "
            f"{report.failed} failed."
        )
    return EXIT_SUCCESS


def register_projection_parsers(sub: argparse._SubParsersAction) -> None:
    proj = sub.add_parser("projection", help="Projection management")
    proj_sub = proj.add_subparsers(dest="projection_cmd")

    rebuild = proj_sub.add_parser(
        "rebuild-from-regista",
        help="Rebuild the local projection from the regista authority",
    )
    _add_common(rebuild)
    rebuild.set_defaults(func=cmd_projection_rebuild)

    proj.set_defaults(func=lambda args: (_print_sub_help(proj), EXIT_SUCCESS)[1])


def _print_sub_help(parser: argparse.ArgumentParser) -> None:
    parser.print_help()
