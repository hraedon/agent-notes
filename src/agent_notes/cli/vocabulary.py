from __future__ import annotations

import argparse
import json

from agent_notes.cli.common import EXIT_NOT_FOUND, EXIT_SUCCESS


def cmd_vocab_list(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    from agent_notes.core.db import list_vocabulary, list_workspaces

    ws = next((w for w in list_workspaces() if w.slug == args.workspace), None)
    if ws is None:
        print(f"Workspace '{args.workspace}' not found.")
        return EXIT_NOT_FOUND

    vocabs = list_vocabulary(
        ws.id, kind_namespace=args.kind, include_archived=args.include_archived
    )
    if use_json:
        print(json.dumps({"vocabulary": [v.__dict__ for v in vocabs]}, indent=2, default=str))
    else:
        print(f"{len(vocabs)} vocab entry(ies):")
        for v in vocabs:
            print(f"- {v.kind_namespace}/{v.name} (sort={v.sort_order})")
    return EXIT_SUCCESS


def cmd_vocab_add(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    from agent_notes.core.db import add_vocabulary, list_workspaces

    ws = next((w for w in list_workspaces() if w.slug == args.workspace), None)
    if ws is None:
        print(f"Workspace '{args.workspace}' not found.")
        return EXIT_NOT_FOUND

    vocab = add_vocabulary(
        ws.id,
        args.kind,
        args.name,
        is_terminal=bool(args.terminal),
        is_open=not bool(args.closed),
        sort_order=int(args.sort_order),
    )
    if use_json:
        print(json.dumps({"vocabulary": vocab.__dict__}, indent=2, default=str))
    else:
        print(f"Added vocabulary entry: {vocab.kind_namespace}/{vocab.name} (sort={vocab.sort_order})")
    return EXIT_SUCCESS


def cmd_vocab_archive(args: argparse.Namespace) -> int:
    from agent_notes.core.db import archive_vocabulary, list_workspaces

    ws = next((w for w in list_workspaces() if w.slug == args.workspace), None)
    if ws is None:
        print(f"Workspace '{args.workspace}' not found.")
        return EXIT_NOT_FOUND
    archive_vocabulary(ws.id, args.kind, args.name)
    print(f"Archived vocabulary entry: {args.kind}/{args.name}")
    return EXIT_SUCCESS


def register_vocabulary_parsers(sub: argparse._SubParsersAction) -> None:
    vocab = sub.add_parser("vocabulary", help="Vocabulary operations")
    vocab_sub = vocab.add_subparsers(dest="vocab_cmd")

    vocab_list = vocab_sub.add_parser("list", help="List vocabularies")
    vocab_list.add_argument("--workspace", required=True)
    vocab_list.add_argument("--kind", default=None)
    vocab_list.add_argument("--include-archived", action="store_true", default=False)
    vocab_list.add_argument("--json", action="store_true")
    vocab_list.set_defaults(func=cmd_vocab_list)

    vocab_add = vocab_sub.add_parser("add", help="Add (or upsert) a vocabulary entry")
    vocab_add.add_argument("--workspace", required=True)
    vocab_add.add_argument("kind")
    vocab_add.add_argument("name")
    vocab_add.add_argument("--terminal", action="store_true", help="Mark as terminal status (e.g. resolved)")
    vocab_add.add_argument("--closed", action="store_true", help="Mark as closed (non-open) status")
    vocab_add.add_argument("--sort-order", type=int, default=100)
    vocab_add.add_argument("--json", action="store_true")
    vocab_add.set_defaults(func=cmd_vocab_add)

    vocab_archive = vocab_sub.add_parser("archive", help="Archive a vocabulary entry")
    vocab_archive.add_argument("--workspace", required=True)
    vocab_archive.add_argument("kind")
    vocab_archive.add_argument("name")
    vocab_archive.set_defaults(func=cmd_vocab_archive)

    vocab.set_defaults(func=lambda args: (_print_sub_help(vocab), EXIT_SUCCESS)[1])


def _print_sub_help(parser: argparse.ArgumentParser) -> None:
    parser.print_help()
