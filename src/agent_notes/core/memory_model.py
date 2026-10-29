"""Memory server model — encapsulates DB operations for the memory kind (Phase 3, 3.6).

Pulled from `agent_notes.servers.memory` during Plan 004 Phase 9a (decision 50).
The model layer is presentation-agnostic: it returns dicts/lists, not formatted
strings. CLI and legacy MCP server both consume the same model.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_notes.core.change_log import write_change
from agent_notes.core.config import reject_regista_writes
from agent_notes.core.db import _conn

_log = logging.getLogger(__name__)

_KIND = "memory"

_WIKILINK_RE = re.compile(r"(?<!`)\[\[([^\]`]+)\]\](?!`)")

_GAP_SECTION_RE = re.compile(
    r"^##\s+Gaps?\s+to\s+flag\s*\n(.*?)(?=\n##|\Z)",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)

_GAP_LINE_RE = re.compile(r"^\s*-\s+\[\[([^\]]+)\]\]\s*:\s*(.+)$", re.MULTILINE)

# ---------------------------------------------------------------------------
# Plan 027 Phase 1 tunables (calibration in plans/027 §10 and
# docs/memory-eval/phase1-2026-10-29.md)
# ---------------------------------------------------------------------------

# WI-1.1: `memory add` refuses, without an explicit choice, a fact whose body
# embedding is at least this similar (cosine) to an existing fact in the
# target tier or its parent tiers. Calibrated on production: a restated lead
# sentence of an existing fact scores >= 0.85 in 23/25 samples, while 0 of the
# 11 facts written since 2026-08 have a neighbour above 0.75.
DUPLICATE_GATE_SIMILARITY = 0.85
# WI-1.5: the hygiene report's "near-duplicate pair" (cosine distance < 0.08,
# the Plan 027 §1 definition). Stricter than the write gate, because a report
# nobody can act on gets ignored.
HYGIENE_DUPLICATE_SIMILARITY = 0.92
# WI-1.3: reflections (episodes) rank this far below a fact of equal
# similarity in default `memory search` / `memory recall`. Chosen with the
# Plan 027 recall fixture (0.04-0.12 never regresses it; 0.07 is best on all
# metrics).
REFLECTION_RANK_PENALTY = 0.07
REFLECTION_POLICIES = ("demote", "mixed", "exclude")
# WI-1.4: size guidance (warn, never refuse).
FACT_SIZE_GUIDANCE_CHARS = 2000
SIZE_GUIDED_TYPES = ("project", "user", "feedback")
# WI-1.2: link kinds `memory link` accepts. Links stay free-text in the
# schema (decision 5: no CHECK constraint); this only bounds the CLI verb.
MEMORY_LINK_KINDS = ("relates_to", "contradicts")


def size_warning(memory_type: str | None, body: str | None) -> str | None:
    """WI-1.4: a one-line warning for an oversized fact body, else None."""
    if body is None or memory_type not in SIZE_GUIDED_TYPES:
        return None
    if len(body) <= FACT_SIZE_GUIDANCE_CHARS:
        return None
    return (
        f"body is {len(body)} characters, over the {FACT_SIZE_GUIDANCE_CHARS}-character "
        f"guidance for a '{memory_type}' memory: consider a 'reference' memory or "
        "splitting it into smaller facts"
    )


def _reflection_terms(policy: str) -> tuple[str, float]:
    """(extra WHERE clause, rank penalty) for a reflection policy."""
    if policy not in REFLECTION_POLICIES:
        raise ValueError(f"unknown reflection policy {policy!r}")
    if policy == "exclude":
        return "memory_type <> 'reflection'", 0.0
    return "", REFLECTION_RANK_PENALTY if policy == "demote" else 0.0


def rank_key(row: dict, policy: str = "mixed") -> float:
    """The score a result list is ordered by (raw similarity minus any penalty)."""
    score = float(row.get("score") or 0.0)
    if policy == "demote" and row.get("memory_type") == "reflection":
        score -= REFLECTION_RANK_PENALTY
    return score


def _validate_memory_type(workspace_id: int, memory_type: str) -> None:
    from agent_notes.core.db import list_vocabulary

    vocabs = list_vocabulary(workspace_id, kind_namespace="memory_type")
    if not any(v.name == memory_type for v in vocabs):
        raise ValueError(
            f"memory_type '{memory_type}' not found in vocabularies. "
            f"Available: {', '.join(v.name for v in vocabs) or '(none)'}"
        )


def parse_wikilinks(body: str) -> list[str]:
    """Extract [[name]] references from body, skipping inline-code spans."""
    return _WIKILINK_RE.findall(body)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def add_memory(
    workspace_id: int,
    project_id: int,
    name: str,
    memory_type: str,
    body: str,
    attributes: dict | None = None,
    embedding: Any | None = None,
    supersedes_name: str | None = None,
    related: list[tuple[int, str]] | None = None,
) -> dict:
    """Add a memory, superseding an active one of the same name in the project.

    ``supersedes_name`` (WI-1.1) also deactivates a *differently named* active
    memory in the same project and records it in ``supersedes``.
    ``related`` is a list of ``(project_id, name)`` memories to link with
    ``relates_to``, in the same transaction.
    """
    reject_regista_writes()
    _validate_memory_type(workspace_id, memory_type)

    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)

        # Check for existing active memory with the same name — supersede it.
        cur.execute(
            "SELECT id FROM memories WHERE project_id = %s AND name = %s AND active = true",
            (project_id, name),
        )
        existing = cur.fetchone()

        if existing and supersedes_name and supersedes_name != name:
            # One predecessor per row: a revision of `name` cannot also
            # replace another memory (the `supersedes` column would lose one).
            raise ValueError(
                f"'{name}' already exists in this scope, so this add revises it; "
                f"--supersedes {supersedes_name} cannot be combined with a revision"
            )

        old_id = None
        if existing:
            old_id = existing["id"]
            cur.execute("UPDATE memories SET active = false WHERE id = %s", (old_id,))

        if supersedes_name and supersedes_name != name:
            cur.execute(
                "UPDATE memories SET active = false "
                "WHERE project_id = %s AND name = %s AND active = true RETURNING id",
                (project_id, supersedes_name),
            )
            target = cur.fetchone()
            if target is None:
                conn.rollback()
                raise ValueError(
                    f"--supersedes: no active memory '{supersedes_name}' in this scope"
                )
            old_id = target["id"]
            write_change(
                conn,
                kind=_KIND,
                workspace_id=workspace_id,
                project_id=project_id,
                identifier=supersedes_name,
                event="superseded",
                payload={"id": old_id, "superseded_by": name},
            )

        cur.execute(
            """
            INSERT INTO memories
                (workspace_id, project_id, name, memory_type, body, embedding,
                 active, supersedes, attributes)
            VALUES (%s, %s, %s, %s, %s, %s, true, %s, %s)
            RETURNING id, workspace_id, project_id, name, memory_type, body,
                     active, supersedes, attributes, created_at, updated_at
            """,
            (
                workspace_id,
                project_id,
                name,
                memory_type,
                body,
                embedding,
                old_id,
                psycopg.types.json.Jsonb(attributes or {}),
            ),
        )
        row = cur.fetchone()
        new_id = row["id"]

        write_change(
            conn,
            kind=_KIND,
            workspace_id=workspace_id,
            project_id=project_id,
            identifier=name,
            event="filed",
            payload={"memory_type": memory_type, "id": new_id},
        )

        # Auto-create [[name]] relates_to links inside the same transaction
        # so link + memory commit atomically (decision 20).
        _auto_create_wikilinks(conn, workspace_id, project_id, name, body)
        for rel_project, rel_name in related or []:
            _insert_memory_link(
                conn, workspace_id, project_id, name, rel_project, rel_name, "relates_to"
            )

        conn.commit()

    return dict(row)


def _insert_memory_link(
    conn: psycopg.Connection,
    workspace_id: int,
    from_project: int,
    from_name: str,
    to_project: int,
    to_name: str,
    relationship: str,
) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO links
            (from_kind, from_workspace, from_project, from_identifier,
             to_kind,   to_workspace,   to_project,   to_identifier, relationship)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (
            _KIND,
            workspace_id,
            from_project,
            from_name,
            _KIND,
            workspace_id,
            to_project,
            to_name,
            relationship,
        ),
    )
    if cur.rowcount > 0:
        write_change(
            conn,
            kind=_KIND,
            workspace_id=workspace_id,
            project_id=from_project,
            identifier=from_name,
            event="link_added",
            payload={"to_kind": _KIND, "to_identifier": to_name, "relationship": relationship},
        )


def _auto_create_wikilinks(
    conn: psycopg.Connection,
    workspace_id: int,
    project_id: int,
    name: str,
    body: str,
) -> None:
    """Create relates_to links for [[wikilinks]] in body. Runs inside caller's transaction."""
    from agent_notes.core.change_log import write_change

    for ref_name in set(parse_wikilinks(body)):
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO links
                    (from_kind, from_workspace, from_project, from_identifier,
                     to_kind,   to_workspace,   to_project,   to_identifier, relationship)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    _KIND,
                    workspace_id,
                    project_id,
                    name,
                    _KIND,
                    workspace_id,
                    project_id,
                    ref_name,
                    "relates_to",
                ),
            )
            if cur.rowcount > 0:
                write_change(
                    conn,
                    kind=_KIND,
                    workspace_id=workspace_id,
                    project_id=project_id,
                    identifier=name,
                    event="link_added",
                    payload={
                        "to_kind": _KIND,
                        "to_identifier": ref_name,
                        "relationship": "relates_to",
                    },
                )
        except (psycopg.Error, ValueError):
            _log.debug("wikilink auto-create skipped: %s -> %s", name, ref_name, exc_info=True)


def get_memory(workspace_id: int, project_id: int, name: str) -> dict | None:
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT id, name, memory_type, body, attributes,
                   supersedes, created_at, updated_at
            FROM memories WHERE project_id = %s AND workspace_id = %s
            AND name = %s AND active = true
            """,
            (project_id, workspace_id, name),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_memories(
    workspace_id: int,
    project_id: int | None = None,
    memory_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    conditions = ["active = true"]
    params: list[Any] = []

    if workspace_id is not None:
        conditions.append("workspace_id = %s")
        params.append(workspace_id)
    if project_id is not None:
        conditions.append("project_id = %s")
        params.append(project_id)
    if memory_type is not None:
        conditions.append("memory_type = %s")
        params.append(memory_type)

    where = " AND ".join(conditions)
    params.append(min(limit, 200))

    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT id, name, memory_type, LEFT(body, 120) AS body_preview,
                   created_at, updated_at
            FROM memories
            WHERE {where}
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            params,
        )
        return [dict(r) for r in cur.fetchall()]


def browse_knowledge(
    workspace_id: int,
    project_id: int | None = None,
    memory_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Rich, paginated read model for the public knowledge provider."""
    conditions = ["active = true", "workspace_id = %s"]
    params: list[Any] = [workspace_id]
    if project_id is not None:
        conditions.append("project_id = %s")
        params.append(project_id)
    if memory_type is not None:
        conditions.append("memory_type = %s")
        params.append(memory_type)
    params.extend([min(max(limit, 0), 200), max(offset, 0)])

    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT id, workspace_id, project_id, name, memory_type,
                   LEFT(body, 240) AS body_preview, attributes, regista_note_id,
                   pending_sync, created_at, updated_at
            FROM memories
            WHERE {" AND ".join(conditions)}
            ORDER BY updated_at DESC
            LIMIT %s OFFSET %s
            """,
            params,
        )
        return [dict(r) for r in cur.fetchall()]


def get_knowledge_memory(workspace_id: int, project_id: int, name: str) -> dict | None:
    """Return the rich exact-note shape used by the public provider."""
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT id, workspace_id, project_id, name, memory_type, body, attributes,
                   supersedes, regista_note_id, pending_sync, created_at, updated_at
            FROM memories WHERE project_id = %s AND workspace_id = %s
            AND name = %s AND active = true
            """,
            (project_id, workspace_id, name),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def search_memories_exact(
    workspace_id: int,
    query: str,
    project_id: int | None = None,
    memory_type: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search exact projected note text without loading the embedding model.

    This is intentionally a simple PostgreSQL lexical/substring search. It is
    deterministic and useful for the human knowledge browser, but it is not a
    relevance-ranked full-text or semantic search engine.
    """
    conditions = [
        "active = true",
        "workspace_id = %s",
        "(name ILIKE %s ESCAPE E'\\\\' OR body ILIKE %s ESCAPE E'\\\\')",
    ]
    escaped_query = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped_query}%"
    params: list[Any] = [workspace_id, pattern, pattern]
    if project_id is not None:
        conditions.append("project_id = %s")
        params.append(project_id)
    if memory_type is not None:
        conditions.append("memory_type = %s")
        params.append(memory_type)
    params.append(min(max(limit, 0), 100))

    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT id, workspace_id, project_id, name, memory_type, body,
                   attributes, regista_note_id, pending_sync, created_at, updated_at
            FROM memories
            WHERE {" AND ".join(conditions)}
            ORDER BY
                CASE WHEN lower(name) = lower(%s) THEN 0 ELSE 1 END,
                updated_at DESC, name
            LIMIT %s
            """,
            [*params[:-1], query, params[-1]],
        )
        return [dict(r) for r in cur.fetchall()]


def knowledge_index_health(
    workspace_id: int,
    project_id: int | None = None,
) -> dict:
    """Return exact-projection and vector-index coverage for active notes."""
    conditions = ["active = true", "workspace_id = %s"]
    params: list[Any] = [workspace_id]
    if project_id is not None:
        conditions.append("project_id = %s")
        params.append(project_id)

    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE embedding IS NOT NULL) AS vector_indexed,
                   count(*) FILTER (WHERE pending_sync) AS pending_sync,
                   count(*) FILTER (WHERE regista_note_id IS NOT NULL) AS signed,
                   max(updated_at) AS latest_update
            FROM memories
            WHERE {" AND ".join(conditions)}
            """,
            params,
        )
        return dict(cur.fetchone())


def search_knowledge_semantic(
    workspace_id: int,
    query_vec: Any,
    project_id: int | None = None,
    memory_type: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Semantic search shape for the public provider, including authority posture."""
    conditions = ["active = true", "workspace_id = %s", "embedding IS NOT NULL"]
    params: list[Any] = [workspace_id]
    if project_id is not None:
        conditions.append("project_id = %s")
        params.append(project_id)
    if memory_type:
        conditions.append("memory_type = %s")
        params.append(memory_type)

    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT id, workspace_id, project_id, name, memory_type, body,
                   attributes, regista_note_id, pending_sync,
                   1 - (embedding <=> %s::vector) AS score,
                   created_at, updated_at
            FROM memories
            WHERE {" AND ".join(conditions)}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            [query_vec, *params, query_vec, min(max(limit, 0), 50)],
        )
        return [dict(r) for r in cur.fetchall()]


def search_memory(
    workspace_id: int,
    query_vec: Any,
    project_id: int | None = None,
    memory_type: str | None = None,
    limit: int = 10,
) -> list[dict]:
    conditions = ["active = true", "workspace_id = %s", "embedding IS NOT NULL"]
    params: list[Any] = [workspace_id]

    if project_id is not None:
        conditions.append("project_id = %s")
        params.append(project_id)
    if memory_type:
        conditions.append("memory_type = %s")
        params.append(memory_type)

    where = " AND ".join(conditions)

    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT id, name, memory_type, LEFT(body, 0) AS body,
                   1 - (embedding <=> %s::vector) AS score,
                   created_at, updated_at
            FROM memories
            WHERE {where}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            [query_vec] + params + [query_vec, min(limit, 50)],
        )
        return [dict(r) for r in cur.fetchall()]


def search_memory_with_body(
    workspace_id: int,
    query_vec: Any,
    project_id: int | None = None,
    memory_type: str | None = None,
    limit: int = 10,
    reflections: str = "mixed",
) -> list[dict]:
    """Same as `search_memory` but returns full body text.

    ``reflections`` (WI-1.3): ``mixed`` ranks by similarity alone, ``demote``
    ranks reflections ``REFLECTION_RANK_PENALTY`` below facts, ``exclude``
    drops them. ``score`` is always the raw similarity.
    """
    conditions = ["active = true", "workspace_id = %s", "embedding IS NOT NULL"]
    params: list[Any] = [workspace_id]

    if project_id is not None:
        conditions.append("project_id = %s")
        params.append(project_id)
    if memory_type:
        conditions.append("memory_type = %s")
        params.append(memory_type)
    extra, penalty = _reflection_terms(reflections)
    if extra:
        conditions.append(extra)

    where = " AND ".join(conditions)

    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT id, project_id, name, memory_type, body,
                   1 - (embedding <=> %s::vector) AS score,
                   created_at, updated_at
            FROM memories
            WHERE {where}
            ORDER BY (embedding <=> %s::vector)
                     + CASE WHEN memory_type = 'reflection' THEN %s ELSE 0 END
            LIMIT %s
            """,
            [query_vec] + params + [query_vec, penalty, min(limit, 50)],
        )
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Composed scopes (Plan 025 section 4): repo > vm > global
# ---------------------------------------------------------------------------

_PREVIEW_CHARS = 120


@dataclass(frozen=True)
class ScopeRef:
    """One memory tier backing a composed query. ``project_id`` is its project row."""

    scope: str  # 'repo' | 'vm' | 'global'
    scope_key: str  # repo project slug, VM ID, or 'global'
    project_id: int


def _shadows_for(
    cur: Any,
    workspace_id: int,
    scopes: list[ScopeRef],
    winners: list[dict],
    skip_reflections: bool = False,
) -> dict[tuple[str, int], list[dict]]:
    """Same-name rows hidden behind each winner, keyed by (name, winner id)."""
    by_project = {s.project_id: s for s in scopes}
    names = sorted({w["name"] for w in winners})
    if not names:
        return {}
    cur.execute(
        """
        SELECT id, project_id, name, LEFT(body, %s) AS body_preview
        FROM memories
        WHERE active = true AND workspace_id = %s
          AND project_id = ANY(%s) AND name = ANY(%s)
          AND NOT (%s AND memory_type = 'reflection')
        ORDER BY array_position(%s::int[], project_id)
        """,
        (
            _PREVIEW_CHARS,
            workspace_id,
            [s.project_id for s in scopes],
            names,
            skip_reflections,
            [s.project_id for s in scopes],
        ),
    )
    winner_ids = {w["id"] for w in winners}
    out: dict[tuple[str, int], list[dict]] = {}
    rows = [r for r in cur.fetchall() if r["id"] not in winner_ids]
    for w in winners:
        out[(w["name"], w["id"])] = [
            {
                "scope": by_project[r["project_id"]].scope,
                "scope_key": by_project[r["project_id"]].scope_key,
                "body_preview": r["body_preview"],
            }
            for r in rows
            if r["name"] == w["name"]
        ]
    return out


def active_by_name(workspace_id: int, scopes: list[ScopeRef], names: list[str]) -> list[dict]:
    """Active memories named ``names`` in ``scopes``, most specific scope first.

    Exact-name lookup for composed recall: a tier's retrieval cutoff must not
    decide which tier wins a name (repo > vm > global).
    """
    if not scopes or not names:
        return []
    by_project = {s.project_id: s for s in scopes}
    project_ids = [s.project_id for s in scopes]
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT project_id, name, memory_type, body
            FROM memories
            WHERE active = true AND workspace_id = %s
              AND project_id = ANY(%s) AND name = ANY(%s)
            ORDER BY array_position(%s::int[], project_id)
            """,
            (workspace_id, project_ids, sorted(set(names)), project_ids),
        )
        return [
            {
                **r,
                "scope": by_project[r["project_id"]].scope,
                "scope_key": by_project[r["project_id"]].scope_key,
            }
            for r in cur.fetchall()
        ]


def _tag_scoped(
    cur: Any,
    workspace_id: int,
    scopes: list[ScopeRef],
    rows: list[dict],
    skip_reflections: bool = False,
) -> list[dict]:
    by_project = {s.project_id: s for s in scopes}
    shadows = _shadows_for(cur, workspace_id, scopes, rows, skip_reflections)
    tagged = []
    for r in rows:
        ref = by_project[r["project_id"]]
        r = dict(r)
        r["scope"] = ref.scope
        r["scope_key"] = ref.scope_key
        r["shadows"] = shadows.get((r["name"], r["id"]), [])
        tagged.append(r)
    return tagged


def search_memory_composed(
    workspace_id: int,
    query_vec: Any,
    scopes: list[ScopeRef],
    memory_type: str | None = None,
    limit: int = 10,
    reflections: str = "mixed",
) -> list[dict]:
    """One similarity query over several scopes, ranked with no tier boost.

    ``scopes`` is ordered most specific first. When a name exists in more than
    one scope only the most specific row is returned; the others ride along
    in ``shadows``. ``reflections`` is the WI-1.3 policy (see
    :func:`search_memory_with_body`).
    """
    if not scopes:
        return []
    project_ids = [s.project_id for s in scopes]
    conditions = [
        "active = true",
        "workspace_id = %s",
        "embedding IS NOT NULL",
        "project_id = ANY(%s)",
    ]
    params: list[Any] = [workspace_id, project_ids]
    if memory_type:
        conditions.append("memory_type = %s")
        params.append(memory_type)
    extra, penalty = _reflection_terms(reflections)
    if extra:
        conditions.append(extra)
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT * FROM (
                SELECT id, project_id, name, memory_type, body,
                       1 - (embedding <=> %s::vector) AS score,
                       created_at, updated_at,
                       row_number() OVER (
                           PARTITION BY name ORDER BY array_position(%s::int[], project_id)
                       ) AS rn
                FROM memories
                WHERE {" AND ".join(conditions)}
            ) ranked
            WHERE rn = 1
            ORDER BY score - CASE WHEN memory_type = 'reflection' THEN %s ELSE 0 END DESC
            LIMIT %s
            """,
            [query_vec, project_ids, *params, penalty, min(limit, 50)],
        )
        rows = [{k: v for k, v in r.items() if k != "rn"} for r in cur.fetchall()]
        return _tag_scoped(cur, workspace_id, scopes, rows, reflections == "exclude")


def list_memories_composed(
    workspace_id: int,
    scopes: list[ScopeRef],
    memory_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Most recently updated memories across scopes, with the same shadowing rule."""
    if not scopes:
        return []
    project_ids = [s.project_id for s in scopes]
    conditions = ["active = true", "workspace_id = %s", "project_id = ANY(%s)"]
    params: list[Any] = [workspace_id, project_ids]
    if memory_type:
        conditions.append("memory_type = %s")
        params.append(memory_type)
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT * FROM (
                SELECT id, project_id, name, memory_type,
                       LEFT(body, {_PREVIEW_CHARS}) AS body_preview,
                       created_at, updated_at,
                       row_number() OVER (
                           PARTITION BY name ORDER BY array_position(%s::int[], project_id)
                       ) AS rn
                FROM memories
                WHERE {" AND ".join(conditions)}
            ) ranked
            WHERE rn = 1
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            [project_ids, *params, min(limit, 200)],
        )
        rows = [{k: v for k, v in r.items() if k != "rn"} for r in cur.fetchall()]
        return _tag_scoped(cur, workspace_id, scopes, rows)


# ---------------------------------------------------------------------------
# Plan 027 Phase 1: write-time dedup (WI-1.1) and contradictions (WI-1.2)
# ---------------------------------------------------------------------------


def dedup_candidates(
    workspace_id: int,
    embedding: Any,
    scopes: list[ScopeRef],
    name: str,
    target_project_id: int | None,
    limit: int = 5,
) -> list[dict]:
    """Facts a new memory may duplicate, most similar first (WI-1.1).

    Composed over ``scopes`` (the target tier and its parent tiers), two
    signals: the ``limit`` nearest facts by body embedding (``match`` includes
    ``"semantic"``), plus any active memory with the *same name* in another
    tier, which the new one would shadow (``match`` includes ``"name"``).
    Reflections are never candidates: they are dated episodes, not facts.
    The same-name row in the target tier is excluded — adding over it is an
    explicit revision (it is superseded).
    """
    if not scopes:
        return []
    by_project = {s.project_id: s for s in scopes}
    project_ids = [s.project_id for s in scopes]
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        semantic: list[dict] = []
        if embedding is not None:
            cur.execute(
                """
                SELECT id, project_id, name, memory_type, LEFT(body, %s) AS body_preview,
                       length(body) AS chars, updated_at,
                       1 - (embedding <=> %s::vector) AS score
                FROM memories
                WHERE active = true AND workspace_id = %s AND project_id = ANY(%s)
                  AND embedding IS NOT NULL AND memory_type <> 'reflection'
                  AND NOT (project_id = %s AND name = %s)
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (
                    _PREVIEW_CHARS,
                    embedding,
                    workspace_id,
                    project_ids,
                    target_project_id or -1,
                    name,
                    embedding,
                    limit,
                ),
            )
            semantic = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """
            SELECT id, project_id, name, memory_type, LEFT(body, %s) AS body_preview,
                   length(body) AS chars, updated_at,
                   CASE WHEN embedding IS NULL OR %s::vector IS NULL THEN NULL
                        ELSE 1 - (embedding <=> %s::vector) END AS score
            FROM memories
            WHERE active = true AND workspace_id = %s AND project_id = ANY(%s)
              AND name = %s AND project_id <> %s
            """,
            (
                _PREVIEW_CHARS,
                embedding,
                embedding,
                workspace_id,
                project_ids,
                name,
                target_project_id or -1,
            ),
        )
        same_name = [dict(r) for r in cur.fetchall()]

    out: dict[int, dict] = {}
    for r in semantic:
        out[r["id"]] = {**r, "match": ["semantic"]}
    for r in same_name:
        if r["id"] in out:
            out[r["id"]]["match"].append("name")
        else:
            out[r["id"]] = {**r, "match": ["name"]}
    rows = sorted(out.values(), key=lambda r: -(r["score"] if r["score"] is not None else 2.0))
    result = []
    for r in rows:
        ref = by_project[r.pop("project_id")]
        # Full precision: callers compare with the gate threshold, then round
        # for display (a raw 0.84996 must not become a blocking 0.85).
        r["score"] = None if r["score"] is None else float(r["score"])
        result.append({**r, "scope": ref.scope, "scope_key": ref.scope_key})
    return result


def find_active(workspace_id: int, project_id: int, name: str) -> dict | None:
    """The active memory ``name`` in one project row, or None (id, name, type only)."""
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT id, project_id, name, memory_type FROM memories "
            "WHERE workspace_id = %s AND project_id = %s AND name = %s AND active = true",
            (workspace_id, project_id, name),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def scope_of_project(slug: str, kind: str) -> tuple[str, str]:
    """(scope, scope_key) for a project row: repo slug, VM ID, or 'global'."""
    if kind == "vm":
        return "vm", slug.split(":", 1)[1] if ":" in slug else slug
    if kind == "global":
        return "global", "global"
    return "repo", slug


def open_contradictions(
    workspace_id: int, keys: list[tuple[int, str]]
) -> dict[tuple[int, str], list[dict]]:
    """Open ``contradicts`` links touching each ``(project_id, name)`` (WI-1.2).

    A contradiction is *open* while both memories are active. It closes when
    either side is superseded under another name or deleted, or when the link
    is removed with ``memory unlink``. The link is symmetric for reporting:
    each side lists the other.
    """
    if not keys:
        return {}
    pids = [k[0] for k in keys]
    names = [k[1] for k in keys]
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            WITH k(pid, name) AS (SELECT * FROM unnest(%s::int[], %s::text[]))
            SELECT l.from_project, l.from_identifier, l.to_project, l.to_identifier,
                   l.created_at, fp.slug AS from_slug, fp.kind AS from_kind,
                   tp.slug AS to_slug, tp.kind AS to_kind
            FROM links l
            JOIN projects fp ON fp.id = l.from_project
            JOIN projects tp ON tp.id = l.to_project
            WHERE l.relationship = 'contradicts'
              AND l.from_kind = 'memory' AND l.to_kind = 'memory'
              AND l.from_workspace = %s
              AND (EXISTS (SELECT 1 FROM k WHERE k.pid = l.from_project
                                            AND k.name = l.from_identifier)
                   OR EXISTS (SELECT 1 FROM k WHERE k.pid = l.to_project
                                               AND k.name = l.to_identifier))
              AND EXISTS (SELECT 1 FROM memories m WHERE m.active
                          AND m.project_id = l.from_project AND m.name = l.from_identifier)
              AND EXISTS (SELECT 1 FROM memories m WHERE m.active
                          AND m.project_id = l.to_project AND m.name = l.to_identifier)
            ORDER BY l.created_at
            """,
            (pids, names, workspace_id),
        )
        rows = cur.fetchall()
    wanted = set(keys)
    out: dict[tuple[int, str], list[dict]] = {}
    for r in rows:
        ends = (
            ((r["from_project"], r["from_identifier"]), r["to_slug"], r["to_kind"], "to"),
            ((r["to_project"], r["to_identifier"]), r["from_slug"], r["from_kind"], "from"),
        )
        for i, (key, other_slug, other_kind, other_end) in enumerate(ends):
            if key not in wanted:
                continue
            other_name = r[f"{other_end}_identifier"]
            scope, scope_key = scope_of_project(other_slug, other_kind)
            out.setdefault(key, []).append(
                {
                    "name": other_name,
                    "scope": scope,
                    "scope_key": scope_key,
                    "since": r["created_at"],
                    "direction": "contradicts" if i == 0 else "contradicted_by",
                }
            )
    return out


def contradiction_warning(name: str, contradictions: list[dict]) -> str:
    """The one-line warning shown next to a memory with open contradictions."""
    others = ", ".join(f"{c['name']} [{c['scope']}:{c['scope_key']}]" for c in contradictions)
    return f"memory '{name}' has an open contradiction with {others}; verify before relying on it"


def delete_memory(workspace_id: int, project_id: int, name: str) -> dict | None:
    """Soft-delete a memory (sets active=false). Returns the deleted row or None."""
    reject_regista_writes()
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            UPDATE memories SET active = false
            WHERE project_id = %s AND workspace_id = %s AND name = %s AND active = true
            RETURNING id
            """,
            (project_id, workspace_id, name),
        )
        row = cur.fetchone()
        if row is None:
            return None

        write_change(
            conn,
            kind=_KIND,
            workspace_id=workspace_id,
            project_id=project_id,
            identifier=name,
            event="deleted",
            payload={"id": row["id"]},
        )
        conn.commit()
        return dict(row)


def update_memory(
    workspace_id: int,
    project_id: int,
    name: str,
    body: str | None = None,
    attributes: dict | None = None,
) -> dict:
    """Update an active memory in-place. Returns the updated row.

    Raises ValueError if the memory is not found or not active.
    """
    reject_regista_writes()
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT id, body, attributes FROM memories "
            "WHERE project_id = %s AND workspace_id = %s AND name = %s AND active = true",
            (project_id, workspace_id, name),
        )
        existing = cur.fetchone()
        if existing is None:
            raise ValueError(f"Memory '{name}' not found (or deleted)")

        sets = ["updated_at = now()"]
        params: list[Any] = []
        if body is not None:
            sets.append("body = %s")
            params.append(body)
        if attributes is not None:
            merged = dict(existing.get("attributes") or {})
            merged.update(attributes)
            sets.append("attributes = %s")
            params.append(psycopg.types.json.Jsonb(merged))

        params.extend([project_id, workspace_id, name])
        cur.execute(
            f"UPDATE memories SET {', '.join(sets)} "
            "WHERE project_id = %s AND workspace_id = %s AND name = %s AND active = true "
            "RETURNING id, workspace_id, project_id, name, memory_type, body, "
            "active, supersedes, attributes, created_at, updated_at",
            params,
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"Memory '{name}' not found (or deleted)")

        write_change(
            conn,
            kind=_KIND,
            workspace_id=workspace_id,
            project_id=project_id,
            identifier=name,
            event="updated",
            payload={"fields": [k for k in ("body", "attributes") if locals().get(k) is not None]},
        )
        conn.commit()
        return dict(row)


# ---------------------------------------------------------------------------
# Reflections spike helpers (Phase 3.6, 5)
# ---------------------------------------------------------------------------


def find_reflections(
    workspace_id: int,
    project_id: int | None = None,
    query_vec: Any | None = None,
    limit: int = 10,
    include_body: bool = False,
) -> list[dict]:
    """Find reflection-type memories.

    When `include_body=False` (default), the `body` column is returned as an
    empty string (cheap projection). When `include_body=True`, full body is
    returned. Pass `query_vec` for embedding-similarity ordering; otherwise
    rows are ordered by `updated_at DESC`.
    """
    conditions = ["active = true", "workspace_id = %s", "memory_type = 'reflection'"]
    params: list[Any] = [workspace_id]

    if project_id is not None:
        conditions.append("project_id = %s")
        params.append(project_id)

    if query_vec is not None:
        conditions.append("embedding IS NOT NULL")
        where = " AND ".join(conditions)
        order_clause = "ORDER BY embedding <=> %s::vector"
        select_score = ", 1 - (embedding <=> %s::vector) AS score"
        params_with_vec = [query_vec] + params + [query_vec, min(limit, 50)]
    else:
        where = " AND ".join(conditions)
        order_clause = "ORDER BY updated_at DESC"
        select_score = ""
        params_with_vec = params + [min(limit, 50)]

    body_expr = "body" if include_body else "LEFT(body, 0) AS body"

    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT id, name, {body_expr}, attributes,
                   created_at, updated_at{select_score}
            FROM memories
            WHERE {where}
            {order_clause}
            LIMIT %s
            """,
            params_with_vec,
        )
        return [dict(r) for r in cur.fetchall()]


def find_reflections_with_body(
    workspace_id: int,
    project_id: int | None = None,
    query_vec: Any | None = None,
    limit: int = 10,
) -> list[dict]:
    """Deprecated thin wrapper. Use `find_reflections(..., include_body=True)`."""
    return find_reflections(
        workspace_id=workspace_id,
        project_id=project_id,
        query_vec=query_vec,
        limit=limit,
        include_body=True,
    )


def extract_gaps(workspace_id: int, project_id: int, name: str) -> dict:
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT id, name, body, attributes FROM memories "
            "WHERE project_id = %s AND workspace_id = %s AND name = %s "
            "AND active = true AND memory_type = 'reflection'",
            (project_id, workspace_id, name),
        )
        row = cur.fetchone()

    if row is None:
        return {"error": f"Reflection '{name}' not found"}

    body = row["body"]
    section_match = _GAP_SECTION_RE.search(body)
    if section_match is None:
        return {"error": f"No 'Gaps to flag' section found in '{name}'"}

    section_text = section_match.group(1)
    gaps = _GAP_LINE_RE.findall(section_text)
    if not gaps:
        return {"error": f"No structured gap entries found in '{name}'"}

    already_filed = set((row.get("attributes") or {}).get("gaps_filed_as", []))
    return {
        "name": name,
        "gaps": [
            {
                "identifier": identifier,
                "description": description.strip(),
                "already_filed": identifier in already_filed,
            }
            for identifier, description in gaps
        ],
    }


def mark_gaps_filed(
    workspace_id: int, project_id: int, name: str, filed_identifiers: list[str]
) -> dict:
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT id, attributes FROM memories "
            "WHERE project_id = %s AND workspace_id = %s AND name = %s "
            "AND active = true AND memory_type = 'reflection'",
            (project_id, workspace_id, name),
        )
        row = cur.fetchone()
        if row is None:
            return {"error": f"Reflection '{name}' not found"}

        attrs = dict(row.get("attributes") or {})
        existing = set(attrs.get("gaps_filed_as", []))
        existing.update(filed_identifiers)
        attrs["gaps_filed_as"] = sorted(existing)

        cur.execute(
            "UPDATE memories SET attributes = %s WHERE id = %s",
            (psycopg.types.json.Jsonb(attrs), row["id"]),
        )
        write_change(
            conn,
            kind=_KIND,
            workspace_id=workspace_id,
            project_id=project_id,
            identifier=name,
            event="updated",
            payload={"gaps_filed_as": attrs["gaps_filed_as"]},
        )
        conn.commit()

    return {"name": name, "gaps_filed_as": sorted(existing)}
