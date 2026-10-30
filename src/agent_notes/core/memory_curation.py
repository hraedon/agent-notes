"""Memory-curation notices from the hygiene report (Plan 027 Phase 2, WI-2.1).

A nightly job runs :func:`agent_notes.core.memory_hygiene.hygiene_report`
across every project and posts one Plan 025 ``notice`` to the inbox of each
repo project with actionable findings. A session there (or a human) acts on it
with existing commands; see the ``curate-memory`` skill. Nothing here edits a
memory.

**Dedupe.** Each notice carries a digest of its findings in
``payload.curation.digest``. The digest keys findings by scope and name (by row
ID for possible secrets), never by score or size, so revising a still-oversized
fact or adding one more reflection to an existing backlog does not count as a
change. Before posting, the job reads the latest curation notice addressed to
that project, live or expired:

- same digest, not retired: nothing is posted (an unchanged report never
  reposts, even after its notice has expired);
- a different digest: the new notice is posted and the older live ones are
  retired;
- no findings any more: the live notice is retired, so the same findings
  coming back later post again.

Retiring sets ``expires_at`` to now (inbox reads filter on it) and marks
``payload.curation.retired``. The notice row is the state: no extra table.
Only ``msg prune`` deleting an old notice makes an unchanged report post once
more.

Findings in the vm or global tiers with no repo memory involved have no
project inbox; they are counted in ``unrouted`` and left to the full report.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta
from typing import Any

import psycopg

from agent_notes.core import messaging

CURATION_SENDER = "memory-curation"
NOTICE_MAX_BYTES = 2048
NOTICE_TTL = timedelta(days=30)
# A reflection backlog smaller than this is normal between facts: report it
# in the full report only, not in a notice.
MIN_REFLECTIONS = 3
_NAME_CHARS = 80

# (report section, heading in the notice), in the order the notice lists them.
_SECTIONS = (
    ("possible_secrets", "Possible secrets, for the human"),
    ("contradictions", "Open contradictions"),
    ("near_duplicates", "Near-duplicate facts"),
    ("shadowed", "Same name in several tiers"),
    ("stale", "Stale facts (paths or versions)"),
    ("oversized", "Oversized facts"),
    ("reflections_without_followup", "Reflections with no follow-up fact"),
)

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\s]+")


def _clean(text: Any) -> str:
    """One line, bounded: names come from the store and are framed as data."""
    flat = _CONTROL_RE.sub(" ", str(text)).strip()
    return flat if len(flat) <= _NAME_CHARS else flat[: _NAME_CHARS - 1] + "…"


def _key(ref: dict) -> str:
    return f"{ref['scope']}:{ref['scope_key']}:{ref['name']}"


def _home(ref: dict) -> tuple[str, str] | None:
    """The repo project a reference belongs to, or None for vm/global rows."""
    if ref.get("scope") != "repo":
        return None
    return (ref["workspace"], ref["scope_key"])


def _label(ref: dict, home: tuple[str, str]) -> str:
    """A memory's name as seen from ``home``: bare in its own project."""
    if _home(ref) == home:
        return _clean(ref["name"])
    return _clean(f"{ref['scope']}:{ref['name']}")


def _findings(report: dict) -> tuple[dict[tuple[str, str], dict[str, list]], dict[str, int]]:
    """Split the report per repo project.

    Returns ``{(workspace, project): {section: [(digest_key, label, items)]}}``
    and the unrouted counts. A pair finding is filed under every repo project
    it touches.
    """
    projects: dict[tuple[str, str], dict[str, list]] = {}
    unrouted: dict[str, int] = {}

    def file(refs: list[dict], section: str, key: Any, label, items: int = 1) -> None:
        homes = {h for h in (_home(r) for r in refs) if h is not None}
        if not homes:
            where = f"{refs[0]['scope']}:{refs[0]['scope_key']}"
            unrouted[where] = unrouted.get(where, 0) + items
            return
        for home in sorted(homes):
            entry = (json.dumps(key, sort_keys=True), label(home), items)
            projects.setdefault(home, {}).setdefault(section, []).append(entry)

    for d in report.get("near_duplicates", []):
        a, b = d["a"], d["b"]
        file(
            [a, b],
            "near_duplicates",
            ["dup", sorted([_key(a), _key(b)])],
            lambda h, a=a, b=b: f"{_label(a, h)} ~ {_label(b, h)}",
        )
    for r in report.get("oversized", []):
        file(
            [r],
            "oversized",
            ["big", _key(r)],
            lambda h, r=r: f"{_label(r, h)} ({r['chars']:,} chars)",
        )
    for r in report.get("shadowed", []):
        file(
            [r],
            "shadowed",
            ["shadow", _key(r), sorted(_key(s) for s in r["shadows"])],
            lambda h, r=r: (
                f"{_label(r, h)} hides " + ", ".join(_clean(s["scope"]) for s in r["shadows"])
            ),
        )
    for c in report.get("contradictions", []):
        m, o = c["memory"], c["contradicts"]
        file(
            [m, o],
            "contradictions",
            ["contra", sorted([_key(m), _key(o)])],
            lambda h, m=m, o=o: f"{_label(m, h)} vs {_label(o, h)}",
        )
    for r in report.get("stale", []):
        file(
            [r],
            "stale",
            ["stale", _key(r)],
            lambda h, r=r: f"{_label(r, h)} ({r['age_days']}d; {', '.join(r['mentions'])})",
        )
    for g in report.get("reflections_without_followup", []):
        if g["count"] < MIN_REFLECTIONS:
            continue
        ref = {"scope": g["scope"], "scope_key": g["scope_key"], "workspace": g["workspace"]}
        # The oldest reflection names the backlog: more reflections on top of
        # it are the same job, a fact written after it starts a new one.
        file(
            [ref],
            "reflections_without_followup",
            ["refl", g["scope"], g["scope_key"], g["names"][0] if g["names"] else ""],
            lambda h, g=g: f"{str(g['oldest'])[:10]} to {str(g['newest'])[:10]}",
            items=g["count"],
        )
    for r in report.get("possible_secrets", []):
        state = "" if r["active"] else ", inactive row"
        file(
            [r],
            "possible_secrets",
            ["secret", r["id"]],
            lambda h, r=r, state=state: (
                f"{_label(r, h)} id {r['id']} ({_clean(', '.join(r['patterns']))}{state})"
            ),
        )
    return projects, unrouted


def hygiene_command(workspace: str, project: str) -> str:
    return f"agent-notes memory hygiene --workspace {workspace} --project {project}"


def subject_for(items: int) -> str:
    return f"memory curation: {items} item{'' if items == 1 else 's'}"


def render_body(workspace: str, project: str, sections: dict[str, list]) -> str:
    """The notice body: a short list per section, at most ``NOTICE_MAX_BYTES``.

    Examples shrink (3, then 1, then none) until the body fits; the closing
    command line is always kept.
    """
    items = sum(e[2] for entries in sections.values() for e in entries)
    head = f"Memory hygiene found {items} item{'' if items == 1 else 's'} in this project:"
    foot = "Act on it with the curate-memory skill. Full report:\n" + hygiene_command(
        workspace, project
    )

    def build(examples: int) -> str:
        lines = [head, ""]
        for section, heading in _SECTIONS:
            entries = sections.get(section)
            if not entries:
                continue
            count = sum(e[2] for e in entries)
            line = f"- {heading} ({count})"
            shown = [e[1] for e in entries[:examples]]
            if shown:
                more = len(entries) - len(shown)
                line += ": " + "; ".join(shown) + (f"; and {more} more" if more else "")
            lines.append(line)
        lines += ["", foot]
        return "\n".join(lines)

    for examples in (3, 1, 0):
        body = build(examples)
        if len(body.encode("utf-8")) <= NOTICE_MAX_BYTES:
            return body
    # Only pathological slugs get here: cut the list, keep the command.
    tail = "\n…\n\n" + foot
    budget = max(0, NOTICE_MAX_BYTES - len(tail.encode("utf-8")))
    return body.encode("utf-8")[:budget].decode("utf-8", "ignore") + tail


def digest_of(sections: dict[str, list]) -> str:
    keys = sorted(e[0] for entries in sections.values() for e in entries)
    return hashlib.sha256(json.dumps(keys).encode("utf-8")).hexdigest()


def plan_notices(report: dict) -> tuple[list[dict], dict[str, int]]:
    """One planned notice per repo project with findings, plus unrouted counts."""
    projects, unrouted = _findings(report)
    planned = []
    for (workspace, project), sections in sorted(projects.items()):
        items = sum(e[2] for entries in sections.values() for e in entries)
        body = render_body(workspace, project, sections)
        planned.append(
            {
                "workspace": workspace,
                "project": project,
                "items": items,
                "subject": subject_for(items),
                "body": body,
                "bytes": len(body.encode("utf-8")),
                "digest": digest_of(sections),
            }
        )
    return planned, unrouted


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

_CURATION_NOTICES = (
    "kind = 'notice' AND to_session IS NULL AND to_vm IS NULL AND payload->>'sender' = %(sender)s"
)


def _last_notice(conn: psycopg.Connection, workspace_id: int, project: str) -> dict | None:
    row = conn.execute(
        f"SELECT id, payload FROM agent_messages WHERE {_CURATION_NOTICES} "  # noqa: S608
        "AND workspace_id = %(ws)s AND to_project = %(project)s ORDER BY id DESC LIMIT 1",
        {"sender": CURATION_SENDER, "ws": workspace_id, "project": project},
    ).fetchone()
    return dict(row) if row else None


def _live_notices(conn: psycopg.Connection, project: tuple[int, str] | None = None) -> list[dict]:
    """Live, unretired curation notices: one project's, or every project's."""
    sql = (
        "SELECT m.id, m.workspace_id, w.slug AS workspace, m.to_project AS project "
        "FROM agent_messages m JOIN workspaces w ON w.id = m.workspace_id "
        f"WHERE {_CURATION_NOTICES} AND m.expires_at > now() "  # noqa: S608
        "AND NOT COALESCE((m.payload->'curation'->>'retired')::boolean, false)"
    )
    params: dict[str, Any] = {"sender": CURATION_SENDER}
    if project is not None:
        sql += " AND m.workspace_id = %(ws)s AND m.to_project = %(project)s"
        params.update(ws=project[0], project=project[1])
    return [dict(r) for r in conn.execute(sql + " ORDER BY m.id", params).fetchall()]


def _retire(conn: psycopg.Connection, ids: list[int]) -> None:
    if ids:
        conn.execute(
            "UPDATE agent_messages SET expires_at = LEAST(expires_at, now()), "
            "payload = jsonb_set(payload, '{curation,retired}', 'true') "
            "WHERE id = ANY(%s)",
            (ids,),
        )
        conn.commit()


def _is_current(last: dict | None, digest: str) -> bool:
    curation = messaging.payload_of(last).get("curation") if last else None
    return (
        isinstance(curation, dict)
        and curation.get("digest") == digest
        and not curation.get("retired")
    )


def post_curation_notices(
    report: dict,
    *,
    only: tuple[str, str] | None = None,
    dry_run: bool = False,
    vm: str | None = None,
) -> dict[str, Any]:
    """Post (or with ``dry_run``, show) the notices for ``report``.

    ``only`` is ``(workspace, project)`` when the report covers one project:
    stale notices are then retired for that project alone. With ``only=None``
    the report covers every project, and a live notice for any project absent
    from it is retired.
    """
    from agent_notes.core.db import _conn

    planned, unrouted = plan_notices(report)
    if only is not None:
        planned = [n for n in planned if (n["workspace"], n["project"]) == only]
    # A dry run needs no VM ID: it posts nothing.
    from_vm = None if dry_run else (vm or messaging.resolve_vm_id())
    result: dict[str, Any] = {
        "dry_run": dry_run,
        "posted": [],
        "unchanged": [],
        "retired": [],
        "unrouted": dict(sorted(unrouted.items())),
    }
    with _conn() as conn:
        with_findings: set[tuple[int, str]] = set()
        for notice in planned:
            located = messaging.lookup_project(notice["workspace"], notice["project"], conn=conn)
            if located is None:  # a redacted or vanished slug: nowhere to post
                where = f"repo:{notice['project']}"
                result["unrouted"][where] = result["unrouted"].get(where, 0) + notice["items"]
                continue
            workspace_id = located[0]
            with_findings.add((workspace_id, notice["project"]))
            last = _last_notice(conn, workspace_id, notice["project"])
            if _is_current(last, notice["digest"]):
                result["unchanged"].append(
                    {**_brief(notice), "message_id": last["id"] if last else None}
                )
                continue
            entry = {**_brief(notice), "body": notice["body"], "bytes": notice["bytes"]}
            if not dry_run:
                msg = messaging.send_message(
                    workspace_id=workspace_id,
                    from_vm=str(from_vm),
                    to_project=notice["project"],
                    kind="notice",
                    subject=notice["subject"],
                    body=notice["body"],
                    payload={
                        "sender": CURATION_SENDER,
                        "curation": {"digest": notice["digest"], "items": notice["items"]},
                    },
                    expires_in=NOTICE_TTL,
                    conn=conn,
                )
                entry["message_id"] = msg["id"]
                older = _live_notices(conn, (workspace_id, notice["project"]))
                _retire(conn, [r["id"] for r in older if r["id"] != msg["id"]])
            result["posted"].append(entry)

        scope = None
        if only is not None:
            located = messaging.lookup_project(only[0], only[1], conn=conn)
            scope = (located[0], only[1]) if located else None
        stale = []
        if only is None or scope is not None:
            stale = [
                r
                for r in _live_notices(conn, scope)
                if (r["workspace_id"], r["project"]) not in with_findings
            ]
        if not dry_run:
            _retire(conn, [r["id"] for r in stale])
        result["retired"] = [
            {"workspace": r["workspace"], "project": r["project"], "message_id": r["id"]}
            for r in stale
        ]
    return result


def _brief(notice: dict) -> dict:
    return {k: notice[k] for k in ("workspace", "project", "items", "subject", "digest")}
