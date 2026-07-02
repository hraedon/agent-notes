"""Plan 015 — collapse duplicate work-items in a regista project schema.

The pre-fix file-sync / migration paths re-created the same logical breadcrumb as
multiple work-items (the local-projection-vs-SoT staleness bug), under two
identifier formats ("050" and "BC-050"). This repair collapses each duplicate
group onto one canonical item.

regista is event-sourced and hash-chained — work-items are never deleted. So a
"retired" duplicate is driven to a terminal state via a real signed event
(`close_from_open`, the gate-exempt dismissal transition), with the duplicate
relationship recorded in the event payload. Nothing is destroyed; the open
backlog simply stops double-counting, and terminal duplicates age out via the
normal archival sweep.

Winner selection: the copy with the richest history wins (most events, tie-break
most-recent activity) — i.e. the one actually worked. This keeps in-flight items
(`in_review`, etc.) as winners and leaves losers in cleanly-terminable states.
Any group where a *done* loser would be retired under a *non-done* winner (a
resolution-loss risk) is FLAGGED and SKIPPED unless ``--force-resolution-loss``.

Default is a read-only dry run. Pass ``--execute`` to apply.

Usage:
    python -m agent_notes.scripts.dedup_regista_source_identifiers [--project regista] [--execute]
"""

from __future__ import annotations

import argparse
import os
from collections import defaultdict
from typing import Any

import psycopg

from agent_notes.core.actor import Actor
from agent_notes.core.regista_face import RegistaFace, normalize_source_identifier

TERMINAL = {"done", "closed"}
DEDUP_ACTOR = Actor(
    actor_id="agent-notes-dedup",
    actor_kind="system",
    role="system",
    display_name="agent-notes dedup repair (Plan 015)",
)


def _load_items(dsn: str, schema: str) -> list[dict]:
    with psycopg.connect(dsn) as conn:
        cur = conn.cursor(row_factory=psycopg.rows.dict_row)
        cur.execute(
            f'''
            SELECT w.work_item_id, w.current_state,
                   w.custom_fields->>'source_identifier' AS sid,
                   w.custom_fields->>'title' AS title,
                   (SELECT count(*) FROM "{schema}".events e
                      WHERE e.work_item_id = w.work_item_id) AS nev,
                   w.last_event_at
            FROM "{schema}".work_items_current w
            '''
        )
        return cur.fetchall()


def _winner_key(item: dict) -> tuple:
    # Richest history wins: most events, then most-recent activity.
    return (item["nev"], item["last_event_at"] or "")


def _plan(items: list[dict]) -> dict:
    groups: dict[str | None, list[dict]] = defaultdict(list)
    for it in items:
        groups[normalize_source_identifier(it["sid"])].append(it)

    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
    actions: list[dict] = []
    risk_groups: list[dict] = []
    for key, members in dup_groups.items():
        ordered = sorted(members, key=_winner_key, reverse=True)
        winner = ordered[0]
        losers = ordered[1:]
        resolution_loss = winner["current_state"] not in TERMINAL and any(
            loser["current_state"] in TERMINAL for loser in losers
        )
        if resolution_loss:
            risk_groups.append({"key": key, "winner": winner, "losers": losers})
            continue
        for loser in losers:
            actions.append({"key": key, "winner": winner, "loser": loser})
    return {
        "total": len(items),
        "distinct_keys": len(groups),
        "dup_groups": len(dup_groups),
        "actions": actions,
        "risk_groups": risk_groups,
    }


def _retire(face: RegistaFace, loser: dict, winner_id: Any) -> None:
    """Drive a loser to terminal-as-duplicate via signed events (no deletion)."""
    state = loser["current_state"]
    wid = loser["work_item_id"]
    payload = {"reason": "duplicate", "canonical_work_item_id": str(winner_id)}
    if state in TERMINAL:
        # Already terminal — record the duplicate relationship as a comment event.
        face.comment(DEDUP_ACTOR, wid, f"duplicate of {winner_id} (Plan 015 dedup)")
        return
    if state == "deferred":
        face.transition_breadcrumb(DEDUP_ACTOR, wid, "resume")  # deferred -> open
        state = "open"
    if state == "open":
        face.transition_breadcrumb(DEDUP_ACTOR, wid, "close_from_open", payload=payload)
        return
    raise RuntimeError(f"unexpected loser state {state!r} for {wid} — not retireable here")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--project", default="regista", help="regista project schema (default: regista)"
    )
    ap.add_argument("--execute", action="store_true", help="apply (default: dry run)")
    ap.add_argument(
        "--force-resolution-loss",
        action="store_true",
        help="also retire done losers under non-done winners (risky)",
    )
    args = ap.parse_args()

    dsn = os.environ["AGENT_NOTES_REGISTA_DSN"]
    items = _load_items(dsn, args.project)
    plan = _plan(items)

    print(f"project schema: {args.project}")
    print(
        f"items={plan['total']}  distinct-keys={plan['distinct_keys']}  "
        f"duplicate-groups={plan['dup_groups']}"
    )
    state_counts: dict[str, int] = defaultdict(int)
    for a in plan["actions"]:
        state_counts[a["loser"]["current_state"]] += 1
    print(f"losers to retire={len(plan['actions'])}  by-state={dict(state_counts)}")
    print(f"resolution-loss groups FLAGGED (skipped)={len(plan['risk_groups'])}")
    for rg in plan["risk_groups"][:20]:
        ls = ",".join(f"{loser['current_state']}({loser['nev']})" for loser in rg["losers"])
        print(
            f"  RISK key={rg['key']} winner={rg['winner']['current_state']}"
            f"({rg['winner']['nev']}) losers=[{ls}] title={rg['winner']['title']!r:.50}"
        )

    if not args.execute:
        print("\n[dry run] no changes made. Re-run with --execute to apply.")
        return 0

    print("\n[execute] retiring duplicates...")
    import regista

    from agent_notes.core.config import regista_config

    cfg = regista_config()
    reg = regista.Regista(cfg.dsn, args.project, cfg.hmac_key_path, require_ssl=cfg.require_ssl)
    face = RegistaFace(reg)
    done = 0
    try:
        for a in plan["actions"]:
            _retire(face, a["loser"], a["winner"]["work_item_id"])
            done += 1
            if done % 25 == 0:
                print(f"  ...{done}/{len(plan['actions'])}")
    finally:
        face.close()
    print(f"retired {done} duplicates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
