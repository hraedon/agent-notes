"""Shared helpers for the work-item write/query paths.

Extracted from the original monolithic ``work_item_model.py`` (Plan 008 P0).
These are pure helpers used by both the regista write path and the native
op-log path — workspace/vocabulary lookup, work-item row loading, embedding
comparison, regista-snapshot mirroring, and change-log payload building.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_notes.core import kernel, projection


def resolve_workspace_for_project(conn: psycopg.Connection, project_id: int) -> int:
    cur = conn.cursor()
    cur.execute("SELECT workspace_id FROM projects WHERE id = %s", (project_id,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"Project {project_id} not found")
    return row["workspace_id"]


def validate_vocab(
    conn: psycopg.Connection,
    workspace_id: int,
    kind_namespace: str,
    name: str,
) -> None:
    cur = conn.cursor()
    cur.execute(
        (
            "SELECT 1 FROM vocabularies WHERE workspace_id = %s "
            "AND kind_namespace = %s AND name = %s AND archived = false"
        ),
        (workspace_id, kind_namespace, name),
    )
    if cur.fetchone() is None:
        cur.execute(
            "SELECT name FROM vocabularies "
            "WHERE workspace_id = %s AND kind_namespace = %s AND archived = false "
            "ORDER BY sort_order",
            (workspace_id, kind_namespace),
        )
        valid = [r["name"] for r in cur.fetchall()]
        if valid:
            raise ValueError(
                f"Unknown {kind_namespace} value: {name!r}. Valid values: {', '.join(valid)}"
            )
        else:
            raise ValueError(
                f"Unknown {kind_namespace} value: {name!r}. "
                f"No {kind_namespace} entries found for this workspace."
            )


def load_work_item_row(conn: psycopg.Connection, project_id: int, identifier: str) -> dict:
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        "SELECT * FROM work_items WHERE project_id = %s AND identifier = %s",
        (project_id, identifier),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"Work item not found: {identifier!r} in project {project_id}")
    return dict(row)


def embedding_equal(new_emb: Any, old_emb: Any) -> bool:
    """Compare an incoming embedding (list of floats) against the cached
    value read from ``work_items.embedding``.

    The cached value is a pgvector text string (``"[0.1,0.2,...]"``) when
    read without the pgvector psycopg adapter; the incoming value is a
    plain list of floats. Normalize both to lists and compare with a
    tolerance so float representation drift doesn't cause spurious ops
    (WI-008/WI-009).
    """
    if new_emb is None or old_emb is None:
        return new_emb is old_emb

    def _to_float_list(v: Any) -> list[float]:
        if isinstance(v, str):
            return [float(x) for x in json.loads(v)]
        return [float(x) for x in v]

    try:
        a = _to_float_list(new_emb)
        b = _to_float_list(old_emb)
    except (TypeError, ValueError):
        return False
    if len(a) != len(b):
        return False
    return all(abs(x - y) < 1e-6 for x, y in zip(a, b))


def update_change_log_payload(
    old: dict,
    old_body: str,
    mirrored: dict,
    new_body: str,
    external_refs: dict | None,
    diagnostic_keys: dict | None,
) -> dict:
    payload: dict = {}
    for field in ("title", "kind", "status", "severity"):
        old_val = old.get(field)
        new_val = mirrored.get(field)
        if old_val != new_val:
            payload[field] = {"from": old_val, "to": new_val}
    if old_body != new_body:
        payload["body"] = {"from": old_body, "to": new_body}
    if external_refs is not None and old.get("external_refs") != external_refs:
        payload["external_refs"] = external_refs
    if diagnostic_keys is not None and old.get("diagnostic_keys") != diagnostic_keys:
        payload["diagnostic_keys"] = diagnostic_keys
    return payload


def mirror_regista_snapshot(
    conn: psycopg.Connection,
    local: dict,
    regista_work_item: Any,
    embed: Any | None = None,
    pending_sync: bool = False,
    actor_id: str | None = None,
) -> dict:
    def embed_fn(_text: str) -> list[float]:
        return embed

    return projection.mirror_from_regista(
        conn,
        project_id=local["project_id"],
        identifier=local["identifier"],
        entity_id=local["entity_id"],
        regista_work_item_id=regista_work_item.work_item_id,
        state=regista_work_item.current_state,
        custom_fields=dict(regista_work_item.custom_fields),
        embed=embed_fn if embed is not None else None,
        pending_sync=pending_sync,
        actor_id=actor_id,
    )


def entity_id_for_regista_create(identifier: str, regista_work_item_id: Any) -> str:
    return kernel._make_op_id(
        "work_item",
        "create",
        {"identifier": identifier, "regista_work_item_id": str(regista_work_item_id)},
        [],
    )
