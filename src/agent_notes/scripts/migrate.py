"""Migration runner: applies schema/*.sql files in lexicographic order (decision 18).

Usage:
    agent-notes-migrate --all
    agent-notes-migrate --list [--json]
    agent-notes-migrate --file schema/000_core.sql

Each file is run inside a single transaction. If it succeeds, the run is
idempotent because every DDL statement uses IF NOT EXISTS / OR REPLACE /
ON CONFLICT DO NOTHING.

**Where the DDL comes from (WI-047).** The migrations are packaged *inside* the
wheel at ``agent_notes/schema/`` (``force-include`` in ``pyproject.toml``) and
resolved through ``importlib.resources``, because an artifact-only host has no
repository. The previous resolver walked ``Path(__file__).parents`` for a
``schema/`` directory, which only ever succeeds in a source checkout: on a wheel
install it raised ``FileNotFoundError`` and the projection database could not be
migrated at all. A bounded source-checkout fallback remains for editable installs
(where ``force-include`` has not run), but it is a named second choice, not a
search.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import as_file, files
from importlib.resources.abc import Traversable
from pathlib import Path

# The wheel's force-include target: ``agent_notes/schema/``.
_SCHEMA_ANCHOR = "agent_notes"
_SCHEMA_RESOURCE = "schema"


def _packaged_schema() -> Traversable | None:
    """The schema directory inside the installed package, or ``None``.

    ``None`` (rather than an exception) when the anchor exists but carries no
    migrations, which is the editable-install case: ``force-include`` only runs
    for a real wheel build.
    """
    try:
        candidate = files(_SCHEMA_ANCHOR) / _SCHEMA_RESOURCE
    except (ImportError, TypeError):  # pragma: no cover - anchor is this package
        return None
    try:
        if not candidate.is_dir():
            return None
        has_sql = any(child.name.endswith(".sql") for child in candidate.iterdir())
    except OSError:  # pragma: no cover - unreadable resource root
        return None
    return candidate if has_sql else None


def _source_checkout_schema() -> Path | None:
    """``<repo>/schema`` for an editable/source install, or ``None``.

    A fixed number of parents (``scripts/`` -> ``agent_notes/`` -> ``src/`` ->
    repo root), not a walk: an unbounded walk is how a stray ``schema/``
    directory anywhere above the install prefix silently becomes the answer.
    """
    candidate = Path(__file__).resolve().parents[3] / "schema"
    if candidate.is_dir() and any(candidate.glob("*.sql")):
        return candidate
    return None


@contextmanager
def schema_dir() -> Iterator[Path]:
    """Yield a real filesystem directory holding the migration DDL.

    ``as_file`` materialises the resource for the zip-import case; for an
    ordinary wheel install it is already a directory on disk.
    """
    packaged = _packaged_schema()
    if packaged is not None:
        with as_file(packaged) as path:
            yield path
        return
    source = _source_checkout_schema()
    if source is not None:
        yield source
        return
    raise FileNotFoundError(
        f"No migrations found: the installed {_SCHEMA_ANCHOR} package ships no "
        f"{_SCHEMA_RESOURCE}/*.sql and this is not a source checkout. The wheel is "
        f"built wrong — check [tool.hatch.build.targets.wheel.force-include]."
    )


def _run_file(dsn: str, sql_path: Path) -> None:
    import psycopg

    print(f"  applying {sql_path.name} ...", end=" ", flush=True)
    sql = sql_path.read_text(encoding="utf-8")
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            conn.execute(sql)
    print("ok")


def _is_applied(dsn: str, filename: str) -> bool:
    """Check if a migration file was already applied."""
    import psycopg

    try:
        with psycopg.connect(dsn) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM schema_migrations WHERE filename = %s",
                (filename,),
            )
            return cur.fetchone() is not None
    except psycopg.Error:
        # Table doesn't exist yet (first run) — not applied.
        return False


def _record_applied(dsn: str, filename: str) -> None:
    """Record that a migration file was applied."""
    import psycopg

    try:
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s) ON CONFLICT DO NOTHING",
                (filename,),
            )
            conn.commit()
    except psycopg.Error:
        pass  # Best-effort; if the table doesn't exist yet, skip.


def run_all(dsn: str, schema_dir: Path) -> None:
    sql_files = sorted(schema_dir.glob("*.sql"))
    if not sql_files:
        print(f"No .sql files found in {schema_dir}")
        return
    print(f"Applying {len(sql_files)} migration file(s) from {schema_dir}")
    applied = 0
    skipped = 0
    for f in sql_files:
        if _is_applied(dsn, f.name):
            print(f"  {f.name} ... already applied, skipping")
            skipped += 1
            continue
        _run_file(dsn, f)
        _record_applied(dsn, f.name)
        applied += 1
    print(f"Done. Applied {applied}, skipped {skipped}.")


def list_schema(*, as_json: bool) -> None:
    """Report the migrations this install would apply, and where they came from.

    Needs no database, which is the point: on a host where ``--all`` fails, this
    answers whether the DDL is even present before anyone reaches for a DSN. It
    is also the seam ``tests/test_wheel_install.py`` uses to prove the wheel
    resolves its own migrations with no source tree reachable.
    """
    with schema_dir() as directory:
        sql_files = sorted(directory.glob("*.sql"))
        if as_json:
            print(
                json.dumps(
                    {
                        "origin": str(directory),
                        "files": [{"name": f.name, "bytes": f.stat().st_size} for f in sql_files],
                    },
                    indent=2,
                )
            )
            return
        print(f"{len(sql_files)} migration file(s) from {directory}")
        for f in sql_files:
            print(f"  {f.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run agent-notes schema migrations")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="Apply all schema/*.sql files in order")
    group.add_argument("--file", metavar="PATH", help="Apply a single SQL file")
    group.add_argument(
        "--list",
        action="store_true",
        help="List the packaged migrations and where they resolved from (no database needed)",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output for --list")
    args = parser.parse_args()

    # --list is a packaging question, not a database one: resolve and report
    # before demanding a DSN.
    if args.list:
        list_schema(as_json=args.json)
        return

    # WI-051: resolve the DSN through the same layered chain as the rest of
    # agent-notes (process env > suite.env per-user > suite.env system > tool
    # config file), not a bare os.environ read. The bootstrap writes
    # AGENT_NOTES_DSN into suite.env, and the config contract says every suite
    # tool resolves it from there without the operator exporting anything.
    from agent_notes.core.config import resolve_dsn

    try:
        dsn = resolve_dsn()
    except RuntimeError as exc:
        sys.exit(f"Error: {exc}")

    if args.all:
        # Resolved inside the branch that needs it (WI-047): resolving
        # unconditionally in main() killed the --file escape hatch on exactly
        # the hosts where it was the only way forward.
        with schema_dir() as directory:
            run_all(dsn, directory)
    else:
        path = Path(args.file)
        if not path.exists():
            sys.exit(f"Error: file not found: {path}")
        _run_file(dsn, path)


if __name__ == "__main__":
    main()
