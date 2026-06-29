"""Import breadcrumbs from on-disk markdown files into the DB (Plan 007).

This is the files->DB direction needed to migrate file-based projects (sf2,
substrate, v1) onto the DB-canonical store and retire md-as-source-of-truth.
The DB is the source of truth after import; this is a one-time/again-on-demand
ingest, not a maintained two-way sync.

Embedding is injected (``embed_fn``) so the core logic is testable without the
sentence-transformers model on the hot path.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

import yaml

from agent_notes.core.lifecycle import ALL_VALID_STATES, map_legacy_to_canonical

# Sensible open/terminal flags for statuses commonly seen in file corpora.
# Unknown statuses default to open / non-terminal; the operator can refine vocab.
_STATUS_FLAGS: dict[str, tuple[bool, bool]] = {
    # name: (is_open, is_terminal)
    "new": (True, False),
    "proposed": (True, False),
    "active": (True, False),
    "in_progress": (True, False),
    "open": (True, False),
    "deferred": (False, False),
    "stabilized": (False, False),
    "resolved": (False, True),
    "implemented": (False, True),
    "obsolete": (False, True),
    "closed": (False, True),
}


def parse_breadcrumb_file(path: Path) -> dict | None:
    """Parse a breadcrumb markdown file with YAML frontmatter.

    Returns a normalized dict, or ``None`` if the file is not a well-formed
    breadcrumb (no frontmatter, or missing identifier/title) — e.g. a README.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.lstrip().startswith("---"):
        return None
    # Frontmatter is the block between the first two '---' fences.
    body_text = text.lstrip()
    parts = body_text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        fm = yaml.safe_load(parts[1])
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(fm, dict):
        return None

    identifier = str(fm.get("identifier") or fm.get("number") or "").strip()
    title = str(fm.get("title") or "").strip()
    if not identifier or not title:
        return None

    return {
        "identifier": identifier,
        "title": title,
        "body": parts[2].lstrip("\n"),
        "kind": str(fm.get("kind") or "todo").strip(),
        "status": str(fm.get("status") or "new").strip(),
        "severity": str(fm.get("severity") or "medium").strip(),
        "tags": fm.get("tags") or [],
        "related": fm.get("related") or [],
    }


def discover_breadcrumb_files(directory: Path, include_resolved: bool = True) -> list[Path]:
    """All *.md under ``directory`` (and ``directory/resolved`` if present)."""
    files = sorted(p for p in directory.glob("*.md"))
    resolved = directory / "resolved"
    if include_resolved and resolved.is_dir():
        files += sorted(p for p in resolved.glob("*.md"))
    return files


def _map_bc_status_to_wi(bc_status: str) -> str:
    # Plan 013: delegates to ``lifecycle.map_legacy_to_canonical`` (single
    # source). Canonical states pass through; legacy bc_status synonyms map
    # to their canonical equivalent. ``claimed`` is a liveness axis, not a
    # workflow state — never emitted here.
    #
    # Unknown statuses (not canonical, not a known legacy synonym) default to
    # ``open`` — preserving the v1 ingest semantics. Without this fallback,
    # an on-disk status like ``stabilized`` would pass through unchanged and
    # then fail ``wi_status`` vocab validation (a different namespace from
    # ``bc_status`` which ``create_missing_vocab`` reconciles).
    mapped = map_legacy_to_canonical(bc_status)
    if mapped not in ALL_VALID_STATES:
        return "open"
    return mapped


def sync_breadcrumbs_from_dir(
    project_id: int,
    directory: str | Path,
    embed_fn: Callable[[str], Any],
    *,
    create_missing_vocab: bool = False,
    prune: bool = False,
    include_resolved: bool = True,
) -> dict:
    """Upsert every breadcrumb file in ``directory`` into ``project_id``.

    - ``create_missing_vocab``: add any kind/status/severity values encountered
      but absent from the project's workspace vocab (else those files are
      reported under ``errors`` and skipped).
    - ``prune``: delete DB breadcrumbs in the project whose identifier is not
      present in the files (reconciles a stale prior import). Hard delete,
      destructive — off by default.

    Returns a summary dict: imported, skipped, errors, pruned, missing_vocab.
    """
    from agent_notes.core import db
    from agent_notes.core.work_item_model import WorkItemModel

    directory = Path(directory)
    proj = next((p for p in db.list_projects() if p.id == project_id), None)
    if proj is None:
        raise ValueError(f"project id {project_id} not found")
    workspace_id = proj.workspace_id

    parsed: list[tuple[Path, dict]] = []
    skipped: list[dict] = []
    for f in discover_breadcrumb_files(directory, include_resolved=include_resolved):
        try:
            bc = parse_breadcrumb_file(f)
        except ValueError as exc:
            skipped.append({"file": str(f), "reason": str(exc)})
            print(f"WARNING: skipping {f}: {exc}", file=sys.stderr)
            continue
        if bc is None:
            skipped.append({"file": str(f), "reason": "not a breadcrumb (no frontmatter/id/title)"})
        else:
            parsed.append((f, bc))

    # Reconcile vocab. Collect what the files need vs what exists.
    needed = {"bc_kind": set(), "bc_status": set(), "bc_severity": set()}
    for _f, bc in parsed:
        needed["bc_kind"].add(bc["kind"])
        needed["bc_status"].add(bc["status"])
        needed["bc_severity"].add(bc["severity"])
    missing_vocab: dict[str, list[str]] = {}
    for namespace, values in needed.items():
        existing = {v.name for v in db.list_vocabulary(workspace_id, kind_namespace=namespace)}
        missing = sorted(values - existing)
        if not missing:
            continue
        if create_missing_vocab:
            for name in missing:
                is_open, is_terminal = (
                    _STATUS_FLAGS.get(name, (True, False))
                    if namespace == "bc_status"
                    else (True, False)
                )
                db.add_vocabulary(
                    workspace_id, namespace, name, is_terminal=is_terminal, is_open=is_open
                )
        else:
            missing_vocab[namespace] = missing

    imported: list[str] = []
    errors: list[dict] = []
    seen: set[str] = set()
    for f, bc in parsed:
        try:
            vec = embed_fn((bc["title"] + " " + bc["body"]).strip())
            fields = {
                "title": bc["title"],
                "body": bc["body"],
                "kind": bc["kind"],
                "status": _map_bc_status_to_wi(bc["status"]),
                "severity": bc["severity"],
                "external_refs": {"tags": bc["tags"], "related": bc["related"]},
                "embedding": vec,
            }
            existing = WorkItemModel.get_work_item(project_id, bc["identifier"])
            if existing is None:
                WorkItemModel.file_work_item(
                    project_id=project_id, identifier=bc["identifier"], **fields
                )
            else:
                update_fields = dict(fields)
                if existing["status"] in ("closed", "deferred"):
                    update_fields.pop("status", None)
                WorkItemModel.update_work_item(
                    project_id=project_id, identifier=bc["identifier"], **update_fields
                )
            imported.append(bc["identifier"])
            seen.add(bc["identifier"])
        except ValueError as exc:
            errors.append({"file": str(f), "identifier": bc["identifier"], "error": str(exc)})

    pruned: list[str] = []
    if prune:
        existing_rows = WorkItemModel.query_work_items(project_id=project_id, limit=100000)
        for row in existing_rows:
            if row["identifier"] not in seen:
                if WorkItemModel.delete_work_item(project_id, row["identifier"]):
                    pruned.append(row["identifier"])

    return {
        "imported": imported,
        "skipped": skipped,
        "errors": errors,
        "pruned": pruned,
        "missing_vocab": missing_vocab,
    }
