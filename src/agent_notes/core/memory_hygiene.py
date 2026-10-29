"""Deterministic, read-only memory hygiene report (Plan 027 WI-1.5).

One function, :func:`hygiene_report`, runs a fixed set of SELECTs inside a
read-only transaction and returns a JSON-ready dict. Nothing here writes, calls
an LLM, or loads the embedding model: near-duplicates compare the stored
embeddings in SQL.

Checks:

- ``near_duplicates``: differently named fact pairs (reflections excluded)
  whose embeddings are at least ``HYGIENE_DUPLICATE_SIMILARITY`` similar, in
  the same project or in tiers that compose (repo with vm/global, vm with
  global). A pair that is already linked (any relationship, e.g. ``memory add
  --related``) has been judged and is not reported again.
- ``oversized``: ``project``/``user``/``feedback`` bodies over the WI-1.4 size
  guidance.
- ``shadowed``: the same name active in tiers that compose; the more specific
  one hides the other from composed search.
- ``contradictions``: open ``contradicts`` links (both ends active).
- ``stale``: facts not updated for ``stale_days`` whose body mentions a path or
  a version string, the details most likely to rot.
- ``reflections_without_followup``: reflections with no fact written or
  updated in the same project after them, and no fact linking to them; grouped
  per project.
- ``possible_secrets``: secret-shaped strings (by shape or by context) in the
  name, body or attributes of any row, active or superseded. Each finding is
  the row ID and the pattern labels; the matched text is never reported.

Every memory in every section is identified by its row ID. Names, workspace
slugs and scope keys that themselves contain a secret are replaced by a
``[redacted: memory id N]`` marker wherever they would appear.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.rows import dict_row

from agent_notes.core.db import _conn
from agent_notes.core.memory_model import (
    FACT_SIZE_GUIDANCE_CHARS,
    HYGIENE_DUPLICATE_SIMILARITY,
    SIZE_GUIDED_TYPES,
    scope_of_project,
)
from agent_notes.core.secret_patterns import redact_if_secret, scan_secrets

STALE_DAYS = 180

# A filesystem-looking path: /a/b, ~/a/b, ./a/b, C:\a, or a relative file with
# a source/config extension. URLs are excluded by the look-behind.
#
# Every pattern here is linear in the body. The absolute and drive forms can
# only start where the look-behind allows. A relative path is matched as one
# whole token (the look-behind stops a match starting inside a token) and its
# extension is checked in code: a single regex that demanded the extension
# re-tried the rest of an extension-less path from every "/", which was
# quadratic ("a/" * N).
_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![\w./:])(?:~|\.{1,2})?/(?:[\w.\-]+/)+[\w.\-]*"
    r"|\b[A-Za-z]:\\[\w.\-\\]+"
)
_RELATIVE_PATH_TOKEN_RE = re.compile(r"(?<![\w./\-])[\w\-]+(?:/[\w.\-]+)+")
_PATH_EXTENSION_RE = re.compile(
    r"\.(?:py|md|sql|toml|ya?ml|json|sh|ts|js|rs|go|ps1|cfg|ini|txt)(?:\.|$)"
)


def _mentions_path(text: str) -> bool:
    """True when ``text`` mentions a filesystem path (see the patterns above)."""
    if _ABSOLUTE_PATH_RE.search(text):
        return True
    for match in _RELATIVE_PATH_TOKEN_RE.finditer(text):
        if _PATH_EXTENSION_RE.search(match.group(0).rsplit("/", 1)[-1]):
            return True
    return False


# A version string: 1.2.3 (optionally v-prefixed, with a suffix), or v1.2.
_VERSION_RE = re.compile(r"\bv?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.]+)?\b|\bv\d+\.\d+\b")


def _ref(row: dict, prefix: str = "") -> dict:
    """A memory reference for the report: row ID, plus metadata with secrets redacted.

    Names, workspace slugs and scope keys are scanned like bodies: one that
    carries a secret is replaced by a marker, so no section of the report
    (JSON or text) can echo it. The row ID always identifies the memory.
    """
    row_id = row.get(f"{prefix}id")
    scope, scope_key = scope_of_project(row[f"{prefix}slug"], row[f"{prefix}kind"])
    return {
        "id": row_id,
        "name": redact_if_secret(row[f"{prefix}name"], row_id=row_id),
        "memory_type": row.get(f"{prefix}memory_type"),
        "workspace": redact_if_secret(row[f"{prefix}ws"]),
        "scope": scope,
        "scope_key": redact_if_secret(scope_key),
    }


def hygiene_report(
    *,
    project_id: int | None = None,
    stale_days: int = STALE_DAYS,
    duplicate_similarity: float = HYGIENE_DUPLICATE_SIMILARITY,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the report for one repo project (``project_id``) or for every project.

    In project mode a finding is included when it involves a memory of that
    project; its partners may live in the vm/global tiers.
    """
    started = time.monotonic()
    now = now or datetime.now(timezone.utc)
    stale_before = now - timedelta(days=stale_days)
    # A NULL project filter means "all"; every query takes it as `%(pid)s`.
    p: dict[str, Any] = {"pid": project_id}

    with _conn() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        cur = conn.cursor(row_factory=dict_row)

        cur.execute(
            """
            WITH f AS (
                SELECT m.id, m.workspace_id, m.project_id, m.name, m.memory_type,
                       m.embedding, p.slug, p.kind, w.slug AS ws
                FROM memories m
                JOIN projects p ON p.id = m.project_id
                JOIN workspaces w ON w.id = m.workspace_id
                WHERE m.active AND m.embedding IS NOT NULL
                  AND m.memory_type <> 'reflection'
            )
            SELECT a.id AS a_id, b.id AS b_id,
                   a.name AS a_name, a.memory_type AS a_memory_type, a.slug AS a_slug,
                   a.kind AS a_kind, a.ws AS a_ws,
                   b.name AS b_name, b.memory_type AS b_memory_type, b.slug AS b_slug,
                   b.kind AS b_kind, b.ws AS b_ws,
                   1 - (a.embedding <=> b.embedding) AS similarity
            FROM f a
            JOIN f b ON a.id < b.id AND a.workspace_id = b.workspace_id
             AND (a.project_id = b.project_id
                  OR a.kind = 'global' OR b.kind = 'global'
                  OR (a.kind = 'repo') <> (b.kind = 'repo'))
            WHERE 1 - (a.embedding <=> b.embedding) >= %(sim)s
              AND a.name <> b.name  -- same name across tiers is reported as shadowed
              AND (%(pid)s::int IS NULL OR %(pid)s IN (a.project_id, b.project_id))
              AND NOT EXISTS (
                  SELECT 1 FROM links l
                  WHERE l.from_kind = 'memory' AND l.to_kind = 'memory'
                    AND ((l.from_project = a.project_id AND l.from_identifier = a.name
                          AND l.to_project = b.project_id AND l.to_identifier = b.name)
                      OR (l.from_project = b.project_id AND l.from_identifier = b.name
                          AND l.to_project = a.project_id AND l.to_identifier = a.name)))
            ORDER BY similarity DESC, a.name, b.name
            """,
            {**p, "sim": duplicate_similarity},
        )
        near_duplicates = [
            {
                "a": _ref(r, "a_"),
                "b": _ref(r, "b_"),
                "similarity": round(float(r["similarity"]), 4),
            }
            for r in cur.fetchall()
        ]

        cur.execute(
            """
            SELECT m.id, m.name, m.memory_type, length(m.body) AS chars, p.slug, p.kind,
                   w.slug AS ws
            FROM memories m
            JOIN projects p ON p.id = m.project_id
            JOIN workspaces w ON w.id = m.workspace_id
            WHERE m.active AND m.memory_type = ANY(%(types)s)
              AND length(m.body) > %(chars)s
              AND (%(pid)s::int IS NULL OR m.project_id = %(pid)s)
            ORDER BY chars DESC, m.name
            """,
            {**p, "types": list(SIZE_GUIDED_TYPES), "chars": FACT_SIZE_GUIDANCE_CHARS},
        )
        oversized = [{**_ref(r), "chars": r["chars"]} for r in cur.fetchall()]

        cur.execute(
            """
            SELECT a.id, a.name, a.memory_type, ap.slug, ap.kind, w.slug AS ws,
                   b.id AS b_id, b.memory_type AS b_memory_type, bp.slug AS b_slug,
                   bp.kind AS b_kind, b.name AS b_name, w.slug AS b_ws
            FROM memories a
            JOIN projects ap ON ap.id = a.project_id
            JOIN workspaces w ON w.id = a.workspace_id
            JOIN memories b ON b.workspace_id = a.workspace_id AND b.name = a.name
                           AND b.active AND b.project_id <> a.project_id
            JOIN projects bp ON bp.id = b.project_id
            WHERE a.active
              AND (CASE ap.kind WHEN 'repo' THEN 0 WHEN 'vm' THEN 1 ELSE 2 END)
                < (CASE bp.kind WHEN 'repo' THEN 0 WHEN 'vm' THEN 1 ELSE 2 END)
              AND (%(pid)s::int IS NULL OR a.project_id = %(pid)s)
            ORDER BY a.name, ap.slug, bp.kind, bp.slug
            """,
            p,
        )
        shadow_map: dict[tuple, dict] = {}
        for r in cur.fetchall():
            winner = _ref(r)
            key = winner["id"]
            entry = shadow_map.setdefault(key, {**winner, "shadows": []})
            entry["shadows"].append(_ref(r, "b_"))
        shadowed = list(shadow_map.values())

        cur.execute(
            """
            SELECT fm.id AS a_id, l.from_identifier AS a_name, fp.slug AS a_slug,
                   fp.kind AS a_kind, tm.id AS b_id, l.to_identifier AS b_name,
                   tp.slug AS b_slug, tp.kind AS b_kind,
                   w.slug AS a_ws, w.slug AS b_ws, l.created_at
            FROM links l
            JOIN projects fp ON fp.id = l.from_project
            JOIN projects tp ON tp.id = l.to_project
            JOIN workspaces w ON w.id = l.from_workspace
            JOIN memories fm ON fm.active AND fm.project_id = l.from_project
                            AND fm.name = l.from_identifier
            JOIN memories tm ON tm.active AND tm.project_id = l.to_project
                            AND tm.name = l.to_identifier
            WHERE l.relationship = 'contradicts'
              AND l.from_kind = 'memory' AND l.to_kind = 'memory'
              AND (%(pid)s::int IS NULL OR %(pid)s IN (l.from_project, l.to_project))
            ORDER BY l.created_at
            """,
            p,
        )
        contradictions = [
            {"memory": _ref(r, "a_"), "contradicts": _ref(r, "b_"), "since": r["created_at"]}
            for r in cur.fetchall()
        ]

        cur.execute(
            """
            SELECT m.id, m.name, m.memory_type, m.body, m.updated_at, p.slug, p.kind,
                   w.slug AS ws
            FROM memories m
            JOIN projects p ON p.id = m.project_id
            JOIN workspaces w ON w.id = m.workspace_id
            WHERE m.active AND m.memory_type <> 'reflection'
              AND m.updated_at < %(before)s
              AND (%(pid)s::int IS NULL OR m.project_id = %(pid)s)
            ORDER BY m.updated_at, m.name
            """,
            {**p, "before": stale_before},
        )
        stale = []
        for r in cur.fetchall():
            body = r["body"] or ""
            mentions = [
                label
                for label, hit in (
                    ("path", _mentions_path(body)),
                    ("version", bool(_VERSION_RE.search(body))),
                )
                if hit
            ]
            if mentions:
                stale.append(
                    {
                        **_ref(r),
                        "updated_at": r["updated_at"],
                        "age_days": (now - r["updated_at"]).days,
                        "mentions": mentions,
                    }
                )

        cur.execute(
            """
            SELECT r.id, r.name, r.created_at, p.slug, p.kind, w.slug AS ws
            FROM memories r
            JOIN projects p ON p.id = r.project_id
            JOIN workspaces w ON w.id = r.workspace_id
            WHERE r.active AND r.memory_type = 'reflection'
              AND (%(pid)s::int IS NULL OR r.project_id = %(pid)s)
              AND NOT EXISTS (
                  SELECT 1 FROM memories f
                  WHERE f.project_id = r.project_id AND f.active
                    AND f.memory_type <> 'reflection' AND f.updated_at > r.created_at)
              AND NOT EXISTS (
                  SELECT 1 FROM links l
                  JOIN memories f ON f.project_id = l.from_project
                                 AND f.name = l.from_identifier AND f.active
                                 AND f.memory_type <> 'reflection'
                  WHERE l.from_kind = 'memory' AND l.to_kind = 'memory'
                    AND l.to_project = r.project_id AND l.to_identifier = r.name)
            ORDER BY p.slug, r.created_at
            """,
            p,
        )
        groups: dict[tuple, dict] = {}
        for r in cur.fetchall():
            ref = _ref(r)
            key = (ref["workspace"], ref["scope"], ref["scope_key"])
            g = groups.setdefault(
                key,
                {
                    "workspace": ref["workspace"],
                    "scope": ref["scope"],
                    "scope_key": ref["scope_key"],
                    "count": 0,
                    "oldest": r["created_at"],
                    "newest": r["created_at"],
                    "ids": [],
                    "names": [],
                },
            )
            g["count"] += 1
            g["newest"] = r["created_at"]
            g["ids"].append(ref["id"])
            g["names"].append(ref["name"])
        reflections_without_followup = sorted(groups.values(), key=lambda g: -g["count"])

        cur.execute(
            """
            SELECT m.id, m.name, m.memory_type, m.active, m.body, m.attributes::text AS attrs,
                   p.slug, p.kind, w.slug AS ws
            FROM memories m
            JOIN projects p ON p.id = m.project_id
            JOIN workspaces w ON w.id = m.workspace_id
            WHERE (%(pid)s::int IS NULL OR m.project_id = %(pid)s)
            ORDER BY m.id
            """,
            p,
        )
        possible_secrets = []
        for r in cur.fetchall():
            found = scan_secrets("\n".join((r["name"] or "", r["body"] or "", r["attrs"] or "")))
            if found:
                possible_secrets.append({**_ref(r), "active": r["active"], "patterns": found})
        conn.rollback()

    sections = {
        "near_duplicates": near_duplicates,
        "oversized": oversized,
        "shadowed": shadowed,
        "contradictions": contradictions,
        "stale": stale,
        "reflections_without_followup": reflections_without_followup,
        "possible_secrets": possible_secrets,
    }
    summary = {k: len(v) for k, v in sections.items()}
    summary["reflections_without_followup"] = sum(g["count"] for g in reflections_without_followup)
    return redact_strings(
        {
            "summary": summary,
            "thresholds": {
                "duplicate_similarity": duplicate_similarity,
                "size_guidance_chars": FACT_SIZE_GUIDANCE_CHARS,
                "stale_days": stale_days,
            },
            **sections,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    )


def redact_strings(obj: Any) -> Any:
    """Every string in ``obj`` (recursively) that carries a secret becomes ``[redacted]``.

    The report's last line of defence: names are already replaced by an
    ID-bearing marker in :func:`_ref`, but memory types, slugs, mentions and
    the caller's own ``scope`` header go through here too, so no string field
    of the report, JSON or text, can echo a secret. Keys are fixed schema.
    """
    if isinstance(obj, str):
        return redact_if_secret(obj)
    if isinstance(obj, dict):
        return {k: redact_strings(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [redact_strings(v) for v in obj]
    return obj
