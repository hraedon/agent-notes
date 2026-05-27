"""Health-check script for agent-notes installations (Phase 6.3).

Checks:
1. DSN reachable
2. Schema up to date (core tables present)
3. Embedding model loads
4. Links audit (dangling links)
5. Vocabulary integrity

Exit code: 0 if all healthy, 1 if any check failed.
"""

from __future__ import annotations

import os
import sys
import time


def _print_section(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def _print_result(ok: bool, msg: str) -> None:
    status = "PASS" if ok else "FAIL"
    print(f"  {status}: {msg}")


def _check_dsn() -> tuple[bool, str]:
    try:
        from agent_notes.core.db import _conn

        with _conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
        return True, "Connected successfully"
    except Exception as exc:
        return False, str(exc)


def _check_schema() -> tuple[bool, str]:
    expected = {
        "breadcrumbs",
        "memories",
        "links",
        "change_log",
        "all_notes_search_v",
    }
    try:
        from agent_notes.core.db import _conn

        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                """
            )
            actual = {r["table_name"] for r in cur.fetchall()}
        missing = expected - actual
        if missing:
            return False, f"Missing tables/views: {sorted(missing)}"
        return True, f"All expected tables/views present: {sorted(expected)}"
    except Exception as exc:
        return False, str(exc)


def _check_embedding() -> tuple[bool, str]:
    try:
        from agent_notes.core import embed

        expected_dim = int(os.environ.get("AGENT_NOTES_EMBED_DIM", "0"))
        text = "hello"
        t0 = time.perf_counter()
        vec = embed.embed(text, task="query")
        elapsed = time.perf_counter() - t0
        dim = int(vec.shape[0])
        msg = f"Model loaded in {elapsed:.2f}s, dim={dim}"
        if expected_dim and expected_dim != dim:
            warn = f"{msg}; WARNING: AGENT_NOTES_EMBED_DIM={expected_dim} but model emits {dim}"
            return False, warn
        return True, msg
    except Exception as exc:
        return False, str(exc)


def _check_links_audit() -> tuple[bool, str]:
    try:
        from agent_notes.core.db import _conn

        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COUNT(*) AS cnt FROM links l
                LEFT JOIN breadcrumbs b
                  ON b.project_id = l.from_project
                 AND b.identifier = l.from_identifier
                WHERE l.from_kind = 'breadcrumb' AND b.project_id IS NULL
                """
            )
            dangle_bc_from = cur.fetchone()["cnt"]

            cur.execute(
                """
                SELECT COUNT(*) AS cnt FROM links l
                LEFT JOIN memories m
                  ON m.project_id = l.to_project
                 AND m.name = l.to_identifier
                 AND m.active = true
                WHERE l.to_kind = 'memory' AND m.id IS NULL
                """
            )
            dangle_mem_to = cur.fetchone()["cnt"]

            cur.execute(
                """
                SELECT COUNT(*) AS cnt FROM links l
                LEFT JOIN memories m
                  ON m.project_id = l.from_project
                 AND m.name = l.from_identifier
                 AND m.active = true
                WHERE l.from_kind = 'memory' AND m.id IS NULL
                """
            )
            dangle_mem_from = cur.fetchone()["cnt"]

        total = dangle_bc_from + dangle_mem_to + dangle_mem_from
        if total:
            return (
                False,
                f"Dangling links: breadcrumb-from={dangle_bc_from}, "
                f"memory-to={dangle_mem_to}, memory-from={dangle_mem_from}",
            )
        return True, "No dangling links"
    except Exception as exc:
        return False, str(exc)


def _check_vocab_integrity() -> tuple[bool, str]:
    try:
        from agent_notes.core.db import _conn

        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT DISTINCT kind FROM breadcrumbs b
                WHERE NOT EXISTS (
                    SELECT 1 FROM vocabularies v
                    JOIN projects p ON p.id = b.project_id
                    WHERE v.workspace_id = p.workspace_id
                      AND v.kind_namespace = 'bc_kind' AND v.name = b.kind
                )
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if row:
                return False, f"Orphan breadcrumb kind: {row[0]}"

            cur.execute(
                """
                SELECT DISTINCT status FROM breadcrumbs b
                WHERE NOT EXISTS (
                    SELECT 1 FROM vocabularies v
                    JOIN projects p ON p.id = b.project_id
                    WHERE v.workspace_id = p.workspace_id
                      AND v.kind_namespace = 'bc_status' AND v.name = b.status
                )
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if row:
                return False, f"Orphan breadcrumb status: {row[0]}"

            cur.execute(
                """
                SELECT DISTINCT severity FROM breadcrumbs b
                WHERE NOT EXISTS (
                    SELECT 1 FROM vocabularies v
                    JOIN projects p ON p.id = b.project_id
                    WHERE v.workspace_id = p.workspace_id
                      AND v.kind_namespace = 'bc_severity' AND v.name = b.severity
                )
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if row:
                return False, f"Orphan breadcrumb severity: {row[0]}"

            cur.execute(
                """
                SELECT DISTINCT memory_type FROM memories m
                WHERE NOT EXISTS (
                    SELECT 1 FROM vocabularies v
                    WHERE v.workspace_id = m.workspace_id
                      AND v.kind_namespace = 'memory_type' AND v.name = m.memory_type
                )
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if row:
                return False, f"Orphan memory_type: {row[0]}"

        return True, "All kind/status/severity/memory_type values have matching vocab entries"
    except Exception as exc:
        return False, str(exc)


def _check_bridge_target() -> tuple[bool, str]:
    """If AGENT_NOTES_BRIDGE_TARGET is set, attempt a probe POST.

    Accepts any 2xx/4xx as 'reachable' — agent-wake's ingest returns 403 for an
    unsigned/empty body, which still proves the server is up. Connection-refused
    or timeout is a fail.
    """
    target = os.environ.get("AGENT_NOTES_BRIDGE_TARGET")
    if not target:
        return True, "AGENT_NOTES_BRIDGE_TARGET not set (bridge disabled — OK)"
    try:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(target, data=b"", method="POST")
        try:
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                return True, f"Bridge target reachable ({target}, HTTP {resp.status})"
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                return True, f"Bridge target reachable ({target}, HTTP {exc.code})"
            return False, f"Bridge target HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return False, f"Bridge target unreachable: {exc}"
    except Exception as exc:
        return False, str(exc)


def run() -> int:
    """Run all checks and print a summary. Returns exit code."""
    print("agent-notes-doctor — health check\n")

    all_ok = True

    ok, msg = _check_dsn()
    _print_section("1. DSN Reachable")
    _print_result(ok, msg)
    all_ok = all_ok and ok
    dsn_ok = ok

    ok, msg = _check_schema()
    _print_section("2. Schema Up to Date")
    _print_result(ok, msg)
    all_ok = all_ok and ok
    schema_ok = ok

    if not (dsn_ok and schema_ok):
        for name in (
            "3. Embedding Model",
            "4. Links Audit",
            "5. Vocabulary Integrity",
        ):
            _print_section(name)
            print("  SKIPPED: prerequisite check(s) failed (DSN / Schema)")
        _print_section("Summary")
        print("One or more prerequisite checks failed.")
        return 1

    ok, msg = _check_embedding()
    _print_section("3. Embedding Model")
    _print_result(ok, msg)
    all_ok = all_ok and ok

    ok, msg = _check_links_audit()
    _print_section("4. Links Audit")
    _print_result(ok, msg)
    all_ok = all_ok and ok

    ok, msg = _check_vocab_integrity()
    _print_section("5. Vocabulary Integrity")
    _print_result(ok, msg)
    all_ok = all_ok and ok

    ok, msg = _check_bridge_target()
    _print_section("6. Bridge Target")
    _print_result(ok, msg)
    all_ok = all_ok and ok

    _print_section("Summary")
    if all_ok:
        print("All checks passed.")
        return 0
    else:
        print("One or more checks failed.")
        return 1


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
