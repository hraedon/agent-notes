"""Plan 025 Phase 1 — make the native op-log authoritative for every work item.

While regista was the write path, work-item writes were mirrored straight into
the ``work_items`` cache and wrote no op-log entries. Before native Postgres
becomes the sole authority, two gaps must close:

1. **Seal** (always). Every cache row whose op-log fold does not reproduce it
   (no op chain, or a chain that stopped when the item moved to regista) gets
   a ``create`` stub (if missing) and a ``snapshot`` op carrying the cached
   state. See ``core/work_item/_seal.py``. The native write path also seals
   lazily, so this pass is about verifying and front-loading the work, not
   about correctness of later writes.
2. **Import** (opt-in, ``--import-unmirrored``). Canonical breadcrumbs that
   exist in regista but were never mirrored locally are filed natively, keeping
   their source identifier and regista id. Regista is only read.

Default is a dry run: it opens read-only transactions and writes nothing. Pass
``--apply`` to write. Idempotent: a second ``--apply`` finds nothing to do.

Usage:
    python -m agent_notes.scripts.backfill_native_op_log [--project SLUG] [--json]
    python -m agent_notes.scripts.backfill_native_op_log --apply
    python -m agent_notes.scripts.backfill_native_op_log --import-unmirrored \\
        [--skip <regista-uuid-or-identifier> ...] [--apply]
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_notes.core import kernel
from agent_notes.core.actor import migration_actor
from agent_notes.core.change_log import write_change
from agent_notes.core.db import _conn
from agent_notes.core.projection import state_to_status
from agent_notes.core.regista_face import normalize_source_identifier
from agent_notes.core.work_item import _common, _seal

ENTITY_TYPE = "work_item"


def _projects(conn: psycopg.Connection, slug: str | None) -> list[dict]:
    cur = conn.cursor(row_factory=dict_row)
    if slug:
        cur.execute("SELECT id, workspace_id, slug FROM projects WHERE slug = %s", (slug,))
    else:
        cur.execute("SELECT id, workspace_id, slug FROM projects ORDER BY slug")
    return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Seal
# ---------------------------------------------------------------------------


def seal_pass(*, project: str | None, apply: bool, actor_id: str) -> dict[str, Any]:
    """Seal every drifted cache row. Each row commits on its own."""
    per_project: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    fields: collections.Counter = collections.Counter()
    failures: list[dict] = []
    with _conn() as conn:
        if not apply:
            conn.execute("SET TRANSACTION READ ONLY")
        projects = _projects(conn, project)
        for proj in projects:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT * FROM work_items WHERE project_id = %s ORDER BY identifier",
                (proj["id"],),
            )
            rows = [dict(r) for r in cur.fetchall()]
            counts = per_project[proj["slug"]]
            for row in rows:
                counts["rows"] += 1
                _folded, diffs = _seal.drift(conn, row)
                if not diffs:
                    counts["in_sync"] += 1
                    continue
                kind = "no_op_chain" if diffs == ["op_chain"] else "stale_op_chain"
                counts[kind] += 1
                fields.update(diffs)
                if not apply:
                    continue
                try:
                    _seal.seal(conn, row, actor_id)
                    conn.commit()
                    counts["sealed"] += 1
                except Exception as exc:  # report and keep going; nothing half-written
                    conn.rollback()
                    counts["failed"] += 1
                    failures.append(
                        {
                            "project": proj["slug"],
                            "identifier": row["identifier"],
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
        if not apply:
            conn.rollback()
    total: collections.Counter = collections.Counter()
    for counts in per_project.values():
        total.update(counts)
    return {
        "per_project": {k: dict(v) for k, v in sorted(per_project.items())},
        "total": dict(total),
        "drifted_fields": dict(fields),
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# Import never-mirrored regista items
# ---------------------------------------------------------------------------


def _regista_dsn(explicit: str | None) -> str:
    if explicit:
        return explicit
    from agent_notes.core import secrets
    from agent_notes.core.config import RegistaConfig

    dsn = secrets.resolve_dsn(RegistaConfig().dsn)
    if not dsn:
        raise SystemExit("no regista DSN configured; pass --regista-dsn")
    return dsn


def load_unmirrored(regista_dsn: str, projects: list[dict], mirrored: set[str]) -> list[dict]:
    """Read canonical breadcrumbs from regista that have no local row.

    Each agent-notes project maps to the regista schema of the same name with
    dashes replaced (``face_factory.regista_project_name``). Other workflows in
    a schema (e.g. cairn's tool-call items) are ignored.
    """
    found: list[dict] = []
    with psycopg.connect(
        regista_dsn, options="-c default_transaction_read_only=on", row_factory=dict_row
    ) as rconn:
        cur = rconn.cursor()
        cur.execute("SELECT schema_name FROM information_schema.schemata")
        schemas = {r["schema_name"] for r in cur.fetchall()}
        for proj in projects:
            schema = proj["slug"].replace("-", "_")
            if schema not in schemas:
                continue
            cur.execute(
                f"""
                SELECT work_item_id::text AS wid, current_state, custom_fields, last_event_at
                FROM "{schema}".work_items_current
                WHERE workflow_name = 'canonical' AND work_item_type = 'breadcrumb'
                  AND NOT (work_item_id::text = ANY(%s))
                ORDER BY last_event_at
                """,
                (sorted(mirrored),),
            )
            for r in cur.fetchall():
                found.append({**dict(r), "project": proj})
    return found


def _plan_import(conn: psycopg.Connection, item: dict, skip: set[str]) -> dict:
    proj = item["project"]
    cf = dict(item["custom_fields"] or {})
    sid = normalize_source_identifier(cf.get("source_identifier"))
    plan = {
        "project": proj["slug"],
        "regista_work_item_id": item["wid"],
        "identifier": sid,
        "title": str(cf.get("title") or sid or ""),
        "state": item["current_state"],
        "last_event_at": str(item["last_event_at"]),
        "action": "import",
    }
    if item["wid"] in skip or (sid is not None and sid in skip):
        plan["action"] = "skip_requested"
        return plan
    if not sid:
        plan["action"] = "skip_no_source_identifier"
        return plan
    try:
        plan["status"] = state_to_status(item["current_state"])
        for ns, value in (
            ("wi_status", plan["status"]),
            ("wi_kind", str(cf.get("kind") or "todo")),
            ("wi_severity", str(cf.get("severity") or "medium")),
        ):
            _common.validate_vocab(conn, proj["workspace_id"], ns, value)
    except ValueError as exc:
        plan["action"] = "skip_invalid"
        plan["reason"] = str(exc)
        return plan
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM work_items WHERE project_id = %s AND identifier = %s",
        (proj["id"], sid),
    )
    if cur.fetchone() is not None:
        plan["action"] = "skip_identifier_in_use"
    return plan


def _import_one(conn: psycopg.Connection, item: dict, plan: dict, actor_id: str) -> None:
    proj = item["project"]
    cf = dict(item["custom_fields"] or {})
    body_hash = kernel.store_blob(conn, str(cf.get("description") or ""))
    payload = {
        "project_id": proj["id"],
        "identifier": plan["identifier"],
        "title": plan["title"],
        "body_hash": body_hash,
        "kind": str(cf.get("kind") or "todo"),
        "status": plan["status"],
        "severity": str(cf.get("severity") or "medium"),
        "external_refs": dict(cf.get("external_refs") or {}),
        "diagnostic_keys": dict(cf.get("diagnostic_keys") or {}),
        "embedding": None,
        "frontmatter_version": 1,
        "regista_work_item_id": item["wid"],
    }
    entity_id = kernel._make_op_id(ENTITY_TYPE, "create", payload, [])
    kernel.commit_op(
        conn,
        entity_id=entity_id,
        entity_type=ENTITY_TYPE,
        op_type="create",
        payload=payload,
        actor_id=actor_id,
    )
    if kernel.fold_work_item(conn, entity_id) is None:
        raise RuntimeError("fold_work_item returned None after import create op")
    cur = conn.cursor()
    cur.execute(
        "UPDATE work_items SET regista_work_item_id = %s WHERE entity_id = %s",
        (item["wid"], entity_id),
    )
    write_change(
        conn,
        kind="work_item",
        workspace_id=proj["workspace_id"],
        project_id=proj["id"],
        identifier=plan["identifier"],
        event="filed",
        payload={
            "title": plan["title"],
            "status": plan["status"],
            "imported_from_regista": item["wid"],
        },
        actor=actor_id,
    )


def import_pass(
    *,
    project: str | None,
    apply: bool,
    actor_id: str,
    regista_dsn: str,
    skip: set[str],
) -> dict[str, Any]:
    results: list[dict] = []
    with _conn() as conn:
        if not apply:
            conn.execute("SET TRANSACTION READ ONLY")
        projects = _projects(conn, project)
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT regista_work_item_id::text AS wid FROM work_items "
            "WHERE regista_work_item_id IS NOT NULL"
        )
        mirrored = {r["wid"] for r in cur.fetchall()}
        items = load_unmirrored(regista_dsn, projects, mirrored)
        for item in items:
            plan = _plan_import(conn, item, skip)
            if plan["action"] == "import" and apply:
                try:
                    _import_one(conn, item, plan, actor_id)
                    conn.commit()
                    plan["action"] = "imported"
                except Exception as exc:
                    conn.rollback()
                    plan["action"] = "failed"
                    plan["reason"] = f"{type(exc).__name__}: {exc}"
            results.append(plan)
        if not apply:
            conn.rollback()
    summary = collections.Counter(r["action"] for r in results)
    return {"items": results, "summary": dict(summary)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_human(report: dict, apply: bool) -> None:
    mode = "APPLY" if apply else "DRY RUN (nothing written)"
    print(f"backfill_native_op_log — {mode}")
    seal = report["seal"]
    print("\nSeal pass (fold vs work_items cache):")
    for slug, counts in seal["per_project"].items():
        print(f"  {slug}: {counts}")
    print(f"  TOTAL: {seal['total']}")
    print(f"  drifted fields: {seal['drifted_fields']}")
    for f in seal["failures"]:
        print(f"  FAILED {f['project']} {f['identifier']}: {f['error']}")
    imp = report.get("import")
    if imp is not None:
        print("\nImport pass (regista items never mirrored locally):")
        for it in imp["items"]:
            extra = f" ({it['reason']})" if it.get("reason") else ""
            print(
                f"  [{it['action']}] {it['project']} {it['identifier']} "
                f"{it['state']} {it['regista_work_item_id']} — {it['title'][:60]}{extra}"
            )
        print(f"  summary: {imp['summary']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--project", help="Limit to one agent-notes project slug")
    ap.add_argument("--apply", action="store_true", help="Write (default: dry run)")
    ap.add_argument(
        "--import-unmirrored",
        action="store_true",
        help="Also file regista items that never reached the local cache (reads regista)",
    )
    ap.add_argument("--regista-dsn", help="Regista DSN for --import-unmirrored")
    ap.add_argument(
        "--skip",
        action="append",
        default=[],
        help="Regista work_item_id or source identifier to leave out of the import",
    )
    ap.add_argument("--json", action="store_true", help="Machine-readable report")
    args = ap.parse_args(argv)

    actor_id = migration_actor().actor_id
    report: dict[str, Any] = {"apply": args.apply}
    # Import first so imported rows are already folded when the seal pass runs.
    if args.import_unmirrored:
        report["import"] = import_pass(
            project=args.project,
            apply=args.apply,
            actor_id=actor_id,
            regista_dsn=_regista_dsn(args.regista_dsn),
            skip=set(args.skip),
        )
    report["seal"] = seal_pass(project=args.project, apply=args.apply, actor_id=actor_id)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_human(report, args.apply)
    failed = report["seal"]["failures"] or (
        args.import_unmirrored and report["import"]["summary"].get("failed")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
