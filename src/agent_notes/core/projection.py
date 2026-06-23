"""Local projection mirror for the regista face (Plan 009 D4).

regista is the AUTHORITY for lifecycle + signed events. The local ``work_items``
table is a search/read projection (pgvector embeddings, legacy reads/search). This
module is the single place that translates a regista work-item snapshot into a
local projection row, and that flips the ``pending_sync`` flag the outbox uses.

It is intentionally a leaf module with a narrow, stable interface so the
write-path switch (work_item_model.py) and the outbox/reconcile layer can both
depend on it without editing each other.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

import psycopg
import psycopg.types.json

from agent_notes.core import kernel

_STATUS_FROM_STATE = {
    "open": "open",
    "claimed": "claimed",
    "deferred": "deferred",
    "closed": "closed",
}

EmbedFn = Callable[[str], list[float]]


class _ConnLike(Protocol):
    def cursor(self, *args: Any, **kwargs: Any) -> Any: ...


def state_to_status(state: str) -> str:
    if state not in _STATUS_FROM_STATE:
        raise ValueError(f"unknown regista breadcrumb state: {state!r}")
    return _STATUS_FROM_STATE[state]


def mirror_from_regista(
    conn: _ConnLike,
    *,
    project_id: int,
    identifier: str,
    entity_id: str,
    regista_work_item_id: Any,
    state: str,
    custom_fields: dict,
    embed: EmbedFn | None = None,
    pending_sync: bool = False,
    actor_id: str | None = None,
) -> dict:
    """Upsert the local work_items projection row from a regista work-item snapshot.

    ``custom_fields`` is the regista work-item's custom_fields dict
    (title/description/severity/kind/external_refs/diagnostic_keys/source_identifier).
    The description becomes the local body (content-addressed via content_blobs).
    Returns the mirrored row as a dict.
    """
    title = str(custom_fields.get("title") or identifier)
    description = str(custom_fields.get("description") or "")
    severity = str(custom_fields.get("severity") or "medium")
    kind = str(custom_fields.get("kind") or "todo")
    external_refs = dict(custom_fields.get("external_refs") or {})
    diagnostic_keys = dict(custom_fields.get("diagnostic_keys") or {})
    status = state_to_status(state)
    frontmatter_version = 1

    body_hash = kernel.store_blob(conn, description)
    embedding: list[float] | None = None
    if embed is not None:
        embedding = embed(f"{title}\n\n{description}")

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO work_items
            (entity_id, project_id, identifier, title, body_hash, kind, status,
             severity, external_refs, diagnostic_keys, embedding,
             frontmatter_version, regista_work_item_id, pending_sync)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (project_id, identifier) DO UPDATE SET
            title = EXCLUDED.title,
            body_hash = EXCLUDED.body_hash,
            kind = EXCLUDED.kind,
            status = EXCLUDED.status,
            severity = EXCLUDED.severity,
            external_refs = EXCLUDED.external_refs,
            diagnostic_keys = EXCLUDED.diagnostic_keys,
            embedding = COALESCE(EXCLUDED.embedding, work_items.embedding),
            frontmatter_version = EXCLUDED.frontmatter_version,
            regista_work_item_id = COALESCE(EXCLUDED.regista_work_item_id,
                                            work_items.regista_work_item_id),
            pending_sync = EXCLUDED.pending_sync,
            updated_at = now()
        RETURNING id, entity_id, project_id, identifier, title, body_hash, kind,
                  status, severity, external_refs, diagnostic_keys,
                  regista_work_item_id, pending_sync, created_at, updated_at,
                  closed_at
        """,
        (
            entity_id,
            project_id,
            identifier,
            title,
            body_hash,
            kind,
            status,
            severity,
            psycopg.types.json.Jsonb(external_refs),
            psycopg.types.json.Jsonb(diagnostic_keys),
            _embedding_literal(embedding),
            frontmatter_version,
            str(regista_work_item_id),
            pending_sync,
        ),
    )
    row = cur.fetchone()
    return row


def set_pending_sync(conn: _ConnLike, regista_work_item_id: Any, pending: bool) -> int:
    cur = conn.cursor()
    cur.execute(
        "UPDATE work_items SET pending_sync = %s, updated_at = now() "
        "WHERE regista_work_item_id = %s",
        (pending, str(regista_work_item_id)),
    )
    return cur.rowcount or 0


def count_pending(conn: _ConnLike, project_id: int | None = None) -> int:
    cur = conn.cursor()
    if project_id is None:
        cur.execute("SELECT COUNT(*) AS n FROM work_items WHERE pending_sync = TRUE")
    else:
        cur.execute(
            "SELECT COUNT(*) AS n FROM work_items WHERE pending_sync = TRUE AND project_id = %s",
            (project_id,),
        )
    row = cur.fetchone()
    return int(row["n"]) if row else 0


def find_local_for_regista(conn: _ConnLike, regista_work_item_id: Any) -> dict | None:
    cur = conn.cursor()
    cur.execute(
        "SELECT id, entity_id, project_id, identifier, status, regista_work_item_id, "
        "pending_sync FROM work_items WHERE regista_work_item_id = %s",
        (str(regista_work_item_id),),
    )
    return cur.fetchone()


def _embedding_literal(vec: list[float] | None) -> str | None:
    if vec is None:
        return None
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"
