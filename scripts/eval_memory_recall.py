#!/usr/bin/env python3
"""Static memory-recall evaluation (Plan 027 Phase 0, WI-0.3).

Runs every query in a fixture (``tests/memory_eval/fixture.jsonl``) through the
installed ``agent-notes`` CLI and scores the ranked memory names against the
fixture labels. It never writes: the only commands it runs are the read-only
``agent-notes memory search`` and ``agent-notes memory recall``, in a
subprocess, so internal refactors cannot break it.

Metrics (k defaults to 5):
  precision@k          relevant names in the top k, divided by k
  norm_precision@k     relevant names in the top k, divided by min(k, |relevant|)
  mrr                  1 / rank of the first relevant name within --limit
  stale_hit_rate       share of top-k slots holding a stale_or_wrong name or a
                       row reported inactive
  reflection_above_fact share of queries with a relevant non-reflection memory
                       returned where a non-relevant reflection ranks above it

Usage:
  python scripts/eval_memory_recall.py --fixture tests/memory_eval/fixture.jsonl
  python scripts/eval_memory_recall.py --command both --json-out out.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from statistics import mean

READ_ONLY_COMMANDS = {"search", "recall"}


def run_query(cli: str, command: str, row: dict, args: argparse.Namespace) -> dict:
    if command not in READ_ONLY_COMMANDS:  # belt and braces: never call a writer
        raise ValueError(f"refusing non-read-only command {command!r}")
    argv = [cli, "memory", command, row["query"], "--json", "--limit", str(args.limit)]
    argv += ["--scope", row.get("scope", "composed"), "--workspace", args.workspace]
    argv += ["--project", row["project"]]
    started = time.monotonic()
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=args.timeout, check=False)
    elapsed = round(time.monotonic() - started, 2)
    if proc.returncode != 0:
        return {"error": (proc.stderr or proc.stdout).strip()[-400:], "seconds": elapsed}
    payload = json.loads(proc.stdout)
    items = payload.get("memories") if command == "search" else payload.get("results")
    ranked = []
    for item in items or []:
        ranked.append(
            {
                "name": item.get("name") or item.get("source_ref"),
                "memory_type": item.get("memory_type"),
                "score": round(float(item.get("score") or 0.0), 4),
                "scope": item.get("scope"),
                "active": item.get("active", True),
            }
        )
    return {"ranked": ranked, "warnings": payload.get("warnings", []), "seconds": elapsed}


def score(row: dict, ranked: list[dict], k: int) -> dict:
    relevant, stale = set(row["relevant"]), set(row["stale_or_wrong"])
    names = [r["name"] for r in ranked]
    top = ranked[:k]
    hits = [r["name"] for r in top if r["name"] in relevant]
    first = next((i + 1 for i, n in enumerate(names) if n in relevant), None)
    stale_top = [r["name"] for r in top if r["name"] in stale or r.get("active") is False]
    # Does a non-relevant reflection outrank the best relevant non-reflection memory?
    fact_rank = next(
        (
            i
            for i, r in enumerate(ranked)
            if r["name"] in relevant and r["memory_type"] != "reflection"
        ),
        None,
    )
    refl_above = None
    if fact_rank is not None:
        refl_above = any(
            r["memory_type"] == "reflection" and r["name"] not in relevant
            for r in ranked[:fact_rank]
        )
    return {
        "top": [f"{r['name']} ({r['memory_type']}, {r['score']})" for r in top],
        "hits": hits,
        "precision_at_k": len(hits) / k if relevant else None,
        "norm_precision_at_k": len(hits) / min(k, len(relevant)) if relevant else None,
        "mrr": (1 / first if first else 0.0) if relevant else None,
        "first_relevant_rank": first,
        "missed": sorted(relevant - set(names)),
        "stale_in_top": stale_top,
        "reflections_in_top": sum(r["memory_type"] == "reflection" for r in top),
        "misfiled_reflections_in_top": sum(
            r["name"].startswith("reflection-") and r["memory_type"] != "reflection" for r in top
        ),
        "slots": len(top),
        "reflection_above_fact": refl_above,
    }


def aggregate(per_query: list[dict]) -> dict:
    ok = [q for q in per_query if "error" not in q]

    def avg(key: str) -> float | None:
        vals = [q[key] for q in ok if q.get(key) is not None]
        return round(mean(vals), 3) if vals else None

    slots = sum(q["slots"] for q in ok) or 1
    labelled_stale = [q for q in ok if q["stale_labels"]]
    refl = [q["reflection_above_fact"] for q in ok if q["reflection_above_fact"] is not None]
    return {
        "queries": len(per_query),
        "errors": len(per_query) - len(ok),
        "precision_at_k": avg("precision_at_k"),
        "norm_precision_at_k": avg("norm_precision_at_k"),
        "mrr": avg("mrr"),
        "hit_rate_at_k": avg_bool([bool(q["hits"]) for q in ok if q["relevant_labels"]]),
        "stale_hit_rate": round(sum(len(q["stale_in_top"]) for q in ok) / slots, 3),
        "queries_with_stale_in_top": avg_bool([bool(q["stale_in_top"]) for q in labelled_stale]),
        "reflection_above_fact": avg_bool(refl),
        "reflection_above_fact_n": len(refl),
        "reflection_share_of_top": round(sum(q["reflections_in_top"] for q in ok) / slots, 3),
        "misfiled_reflection_share_of_top": round(
            sum(q["misfiled_reflections_in_top"] for q in ok) / slots, 3
        ),
        "mean_seconds": avg("seconds"),
    }


def avg_bool(values: list[bool]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--fixture", default="tests/memory_eval/fixture.jsonl")
    ap.add_argument("--cli", default="agent-notes", help="agent-notes executable")
    ap.add_argument("--command", choices=["search", "recall", "both"], default="search")
    ap.add_argument("--workspace", default="default")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--only", help="comma-separated query ids")
    ap.add_argument("--json-out", help="write the full JSON report here (default: stdout)")
    args = ap.parse_args()

    rows = [json.loads(x) for x in Path(args.fixture).read_text("utf-8").splitlines() if x.strip()]
    if args.only:
        wanted = set(args.only.split(","))
        rows = [r for r in rows if r["id"] in wanted]
    commands = ["search", "recall"] if args.command == "both" else [args.command]

    report: dict = {"fixture": args.fixture, "k": args.k, "limit": args.limit, "runs": {}}
    for command in commands:
        per_query = []
        for row in rows:
            result = run_query(args.cli, command, row, args)
            entry = {"id": row["id"], "project": row["project"], "query": row["query"]}
            entry |= {"relevant_labels": row["relevant"], "stale_labels": row["stale_or_wrong"]}
            entry["seconds"] = result["seconds"]
            if "error" in result:
                entry["error"] = result["error"]
            else:
                entry |= score(row, result["ranked"], args.k)
                entry["warnings"] = result["warnings"]
            per_query.append(entry)
            print(f"[{command}] {row['id']} done in {result['seconds']}s", file=sys.stderr)
        report["runs"][command] = {"aggregate": aggregate(per_query), "per_query": per_query}

    text = json.dumps(report, indent=2)
    if args.json_out:
        Path(args.json_out).write_text(text + "\n", "utf-8")
    else:
        print(text)
    for command, run in report["runs"].items():
        a = run["aggregate"]
        print(
            f"{command}: n={a['queries']} err={a['errors']} P@{args.k}={a['precision_at_k']} "
            f"nP@{args.k}={a['norm_precision_at_k']} MRR={a['mrr']} "
            f"hit@{args.k}={a['hit_rate_at_k']} "
            f"stale_slots={a['stale_hit_rate']} stale_queries={a['queries_with_stale_in_top']} "
            f"refl>fact={a['reflection_above_fact']} (n={a['reflection_above_fact_n']}) "
            f"refl_share={a['reflection_share_of_top']}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
