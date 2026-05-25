from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from agent_notes.cli.common import (
    EXIT_GENERIC,
    EXIT_NOT_CONFIGURED,
    EXIT_SUCCESS,
)


def _repo_skills_root(override: Path | None = None) -> Path:
    """Resolve the directory containing skill subdirectories.

    Defaults to `<repo-root>/skills/` (sibling of `src/`). The override
    is used by tests to point at a fixture tree.
    """
    if override is not None:
        return override
    return Path(__file__).resolve().parents[3] / "skills"


def _discover_skills(src_root: Path) -> list[Path]:
    """Return the SKILL.md path for each direct child directory of src_root.

    Skips the `opencode/` subtree (per Plan 004 Q4 — not yet authored
    in this repo) and any directory that lacks a SKILL.md.
    """
    skills: list[Path] = []
    if not src_root.exists():
        return skills
    for child in sorted(src_root.iterdir()):
        if not child.is_dir():
            continue
        if child.name == "opencode":
            continue
        skill_file = child / "SKILL.md"
        if skill_file.is_file():
            skills.append(skill_file)
    return skills


def _install_one(src: Path, dest: Path, dry_run: bool) -> str:
    """Install a single skill; return one of: 'created', 'updated', 'unchanged'."""
    src_content = src.read_text(encoding="utf-8")
    if dest.exists():
        existing = dest.read_text(encoding="utf-8")
        if existing == src_content:
            return "unchanged"
        if dry_run:
            return "updated"
        dest.write_text(src_content, encoding="utf-8")
        return "updated"
    if dry_run:
        return "created"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return "created"


def cmd_install_skills(args: argparse.Namespace) -> int:
    target = args.target or "claude"
    dry_run = bool(args.dry_run)
    use_json = bool(getattr(args, "json", False))

    if target == "opencode":
        msg = (
            "install-skills --target opencode is not yet implemented. "
            "opencode's skill format is open question Q4 in Plan 004; "
            "see skills/opencode/README.md."
        )
        if use_json:
            print(json.dumps({"error": msg, "code": EXIT_NOT_CONFIGURED}))
        else:
            print(msg, file=sys.stderr)
        return EXIT_NOT_CONFIGURED

    if target != "claude":
        print(f"Unknown target: {target!r}", file=sys.stderr)
        return EXIT_GENERIC

    src_root = _repo_skills_root(
        override=Path(args.source) if getattr(args, "source", None) else None
    )
    if getattr(args, "dest", None):
        dest_root = Path(args.dest)
    else:
        dest_root = Path.home() / ".claude" / "skills"

    skills = _discover_skills(src_root)
    if not skills:
        msg = f"No skills found at {src_root}"
        if use_json:
            print(json.dumps({"error": msg, "code": EXIT_NOT_CONFIGURED}))
        else:
            print(msg, file=sys.stderr)
        return EXIT_NOT_CONFIGURED

    results: list[dict] = []
    for src in skills:
        name = src.parent.name
        dest = dest_root / name / "SKILL.md"
        status = _install_one(src, dest, dry_run)
        results.append({"name": name, "src": str(src), "dest": str(dest), "status": status})

    if use_json:
        print(
            json.dumps(
                {"target": target, "dry_run": dry_run, "skills": results},
                indent=2,
                default=str,
            )
        )
    else:
        action = "Would install" if dry_run else "Installed"
        for r in results:
            print(f"{action} [{r['status']}]: {r['name']} -> {r['dest']}")
        summary = {
            "created": sum(1 for r in results if r["status"] == "created"),
            "updated": sum(1 for r in results if r["status"] == "updated"),
            "unchanged": sum(1 for r in results if r["status"] == "unchanged"),
        }
        print(
            f"{action.lower()} {len(results)} skill(s): "
            f"{summary['created']} created, {summary['updated']} updated, "
            f"{summary['unchanged']} unchanged."
        )
    return EXIT_SUCCESS


def register_skills_parser(sub: argparse._SubParsersAction) -> None:
    install = sub.add_parser("install-skills", help="Install skill files")
    install.add_argument("--target", default="claude", choices=["claude", "opencode"])
    install.add_argument("--dry-run", action="store_true")
    install.add_argument("--json", action="store_true")
    # Hidden flags used by tests; intentionally undocumented in --help.
    install.add_argument("--source", default=None, help=argparse.SUPPRESS)
    install.add_argument("--dest", default=None, help=argparse.SUPPRESS)
    install.set_defaults(func=cmd_install_skills)
