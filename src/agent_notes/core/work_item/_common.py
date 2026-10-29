"""Shared helpers for the work-item write/query paths.

Extracted from the original monolithic ``work_item_model.py`` (Plan 008 P0).
These are pure helpers used by the native op-log path — workspace/vocabulary
lookup, work-item row loading, and embedding comparison.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg
from psycopg.rows import dict_row


def resolve_workspace_for_project(conn: psycopg.Connection, project_id: int) -> int:
    cur = conn.cursor()
    cur.execute("SELECT workspace_id FROM projects WHERE id = %s", (project_id,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"Project {project_id} not found")
    return row["workspace_id"]


def require_repo_project(conn: psycopg.Connection, project_id: int) -> None:
    """Work items live only in repo projects, never a ``vm``/``global`` memory tier."""
    cur = conn.cursor()
    cur.execute("SELECT slug, kind FROM projects WHERE id = %s", (project_id,))
    row = cur.fetchone()
    if row is not None and row["kind"] != "repo":
        raise ValueError(
            f"Project {row['slug']!r} is a {row['kind']} memory tier, not a repo project; "
            "work items live only in repo projects."
        )


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
