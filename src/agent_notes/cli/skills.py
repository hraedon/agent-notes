from __future__ import annotations

import argparse

from agent_notes.cli.common import EXIT_SUCCESS


def cmd_install_skills(args: argparse.Namespace) -> int:
    target = args.target or "claude"
    dry_run = args.dry_run
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    skills_src = repo_root / "skills" / target
    if not skills_src.exists():
        print(f"No skills directory found at {skills_src}; creating stub.")
        skills_src.mkdir(parents=True, exist_ok=True)

    if target == "claude":
        skill_dir = Path.home() / ".claude" / "skills"
    else:
        skill_dir = Path.home() / ".config" / "opencode" / "skills"

    if not skill_dir.exists():
        if dry_run:
            print(f"Would create skill directory: {skill_dir}")
        else:
            skill_dir.mkdir(parents=True, exist_ok=True)

    installed = 0
    for src in skills_src.iterdir():
        if src.is_file() and src.suffix == ".md":
            dest = skill_dir / src.name
            if dry_run:
                print(f"Would install: {src} -> {dest}")
            else:
                import shutil

                shutil.copy2(src, dest)
                print(f"Installed: {dest}")
            installed += 1

    print(f"Installed {installed} skill(s) for {target}.")
    return EXIT_SUCCESS


def register_skills_parser(sub: argparse._SubParsersAction) -> None:
    install = sub.add_parser("install-skills", help="Install skill files")
    install.add_argument("--target", default="claude", choices=["claude", "opencode"])
    install.add_argument("--dry-run", action="store_true")
    install.set_defaults(func=cmd_install_skills)
