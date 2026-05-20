"""Migration runner: applies schema/*.sql files in lexicographic order (decision 18).

Usage:
    agent-notes-migrate --all
    agent-notes-migrate --file schema/000_core.sql

Each file is run inside a single transaction. If it succeeds, the run is
idempotent because every DDL statement uses IF NOT EXISTS / OR REPLACE /
ON CONFLICT DO NOTHING.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _find_schema_dir() -> Path:
    here = Path(__file__).resolve()
    # Walk up from scripts/ to find schema/ at the package root.
    for parent in here.parents:
        candidate = parent / "schema"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        "Could not locate schema/ directory. Run from the repo root or install the package."
    )


def _run_file(dsn: str, sql_path: Path) -> None:
    import psycopg

    print(f"  applying {sql_path.name} ...", end=" ", flush=True)
    sql = sql_path.read_text(encoding="utf-8")
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            conn.execute(sql)
    print("ok")


def run_all(dsn: str, schema_dir: Path) -> None:
    sql_files = sorted(schema_dir.glob("*.sql"))
    if not sql_files:
        print(f"No .sql files found in {schema_dir}")
        return
    print(f"Applying {len(sql_files)} migration file(s) from {schema_dir}")
    for f in sql_files:
        _run_file(dsn, f)
    print("Done.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run agent-notes schema migrations")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="Apply all schema/*.sql files in order")
    group.add_argument("--file", metavar="PATH", help="Apply a single SQL file")
    args = parser.parse_args()

    dsn = os.environ.get("AGENT_NOTES_DSN", "")
    if not dsn:
        sys.exit("Error: AGENT_NOTES_DSN environment variable is not set.")

    schema_dir = _find_schema_dir()

    if args.all:
        run_all(dsn, schema_dir)
    else:
        path = Path(args.file)
        if not path.exists():
            sys.exit(f"Error: file not found: {path}")
        _run_file(dsn, path)


if __name__ == "__main__":
    main()
