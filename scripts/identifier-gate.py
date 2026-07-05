#!/usr/bin/env python3
"""CI identifier-gate: fail if known personal/internal identifiers appear in tracked files.

Run as a pre-publication gate and in CI to prevent re-introduction of
identifiers that were scrubbed before the repo goes public (blueprint §3 —
"Public after sanitization"; Plan 017 WI-4.3).

The denylist is a **starting point the repo owner curates** — it forbids the
personal and internal identifiers that must not appear in a public repo. It does
*not* forbid the bare ``hraedon`` / ``hraedon.com`` identity, which is the
project's allowed public identity (used e.g. in test-fixture principal_ids like
``paul@hraedon.com``). Add the work-domain identifier(s) here before the flip;
they are deliberately absent from this file so the gate script itself carries no
secret.

Usage::

    python3 scripts/identifier-gate.py

Exit 0 if clean, 1 if identifiers found.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# (pattern, why-forbidden). Case-insensitive substring match on each line.
# Keep this green on the current tree — a hit means either a real leak to scrub
# or a pattern that should be narrowed.
IDENTIFIERS: list[tuple[str, str]] = [
    ("plm@hraedon.com", "personal email"),
    ("Paul Merritt", "real name"),
    ("human:plm", "personal principal_id handle"),
    ("human:itadmin", "OS-username principal_id"),
    ("regista_app", "internal DB service account"),
    ("agent_notes_app", "internal DB service account"),
    ("mvmpostgres01", "internal hostname"),
]

EXCLUDE_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".mypy_cache", ".claude", ".ruff_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
SELF_REL = "scripts/identifier-gate.py"


def check_file(path: Path) -> list[tuple[str, int, str, str]]:
    findings: list[tuple[str, int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings
    for line_no, line in enumerate(text.splitlines(), 1):
        lower_line = line.lower()
        for pattern, description in IDENTIFIERS:
            if pattern.lower() in lower_line:
                findings.append((str(path), line_no, pattern, description))
    return findings


def main() -> int:
    all_findings: list[tuple[str, int, str, str]] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in EXCLUDE_DIRS for part in path.parts):
            continue
        if path.suffix in EXCLUDE_SUFFIXES:
            continue
        rel = path.relative_to(REPO_ROOT)
        if str(rel) == SELF_REL:
            continue
        all_findings.extend(check_file(path))

    if all_findings:
        print("IDENTIFIER GATE FAILED — personal/internal identifiers found:\n")
        for file_path, line_no, pattern, desc in all_findings:
            try:
                rel = Path(file_path).relative_to(REPO_ROOT)
            except ValueError:
                rel = file_path
            print(f"  {rel}:{line_no}  '{pattern}' ({desc})")
        print(f"\n{len(all_findings)} finding(s). Fix before publishing.")
        return 1

    print("Identifier gate: clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
