"""Markdown projection primitives (decision 4, 8, 24).

Generic helpers used by breadcrumbs (Phase 2b) and potentially reflections
(Phase 5). Only kinds that carry `projection_sha256` / `projection_dirty`
columns use `safe_write`; memories do not (decision 24).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import frontmatter
import yaml

# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter from body.

    Returns (metadata_dict, body_string). If no frontmatter is present,
    metadata is empty and body is the full text.
    """
    post = frontmatter.loads(text)
    return dict(post.metadata), post.content


def render_frontmatter(fm: dict) -> str:
    """Emit canonical YAML frontmatter block (sorted keys for deterministic output)."""
    if not fm:
        return ""
    body = yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=True)
    return f"---\n{body}---\n"


# ---------------------------------------------------------------------------
# Slugify
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(title: str) -> str:
    """Convert a title to a filesystem-safe slug for file naming."""
    lowered = title.lower()
    slugged = _SLUG_RE.sub("-", lowered)
    return slugged.strip("-")


# ---------------------------------------------------------------------------
# render_index
# ---------------------------------------------------------------------------

# Template registry: add new kinds here as Phase 2+ implements them.
_INDEX_TEMPLATES: dict[str, Any] = {}


def _default_template(rows: list[dict]) -> str:
    if not rows:
        return "| Identifier | Title | Status |\n|---|---|---|\n"
    lines = ["| Identifier | Title | Status |", "|---|---|---|"]
    for row in rows:
        ident = row.get("identifier", "")
        title = row.get("title", "")
        status = row.get("status", "")
        lines.append(f"| {ident} | {title} | {status} |")
    return "\n".join(lines) + "\n"


def render_index(rows: list[dict], template_kind: str) -> str:
    """Generate a README index table.

    Kind-aware: breadcrumbs renders like the substrate README today; other
    kinds fall back to a generic identifier/title/status table. New kinds
    register a callable in `_INDEX_TEMPLATES` at import time.
    """
    template_fn = _INDEX_TEMPLATES.get(template_kind, _default_template)
    return template_fn(rows)


def register_index_template(kind: str, fn: Any) -> None:
    """Register a kind-specific index renderer callable."""
    _INDEX_TEMPLATES[kind] = fn


# ---------------------------------------------------------------------------
# safe_write (decision 8)
# ---------------------------------------------------------------------------


class SafeWriteResult(Enum):
    WRITTEN = "written"
    UNCHANGED = "unchanged"
    DRIFT = "drift"
    FS_ERROR = "fs_error"


@dataclass
class SafeWriteOutcome:
    result: SafeWriteResult
    new_sha256: bytes | None = None
    exception: Exception | None = None


def safe_write(
    absolute_path: Path,
    content: str,
    expected_sha256: bytes | None,
) -> SafeWriteOutcome:
    """Hash-check writer (decisions 8, 24).

    Reads the existing file, compares SHA-256, then decides:

    | Disk state                              | Action                                |
    |-----------------------------------------|---------------------------------------|
    | Matches `expected_sha256`               | Overwrite; return WRITTEN             |
    | Bytes-equal to new `content`            | Skip; return UNCHANGED (idempotent)   |
    | `expected_sha256` is None (first write) | Overwrite/create; return WRITTEN      |
    | SHA-256 mismatch vs `expected_sha256`   | Refuse; return DRIFT                  |
    | File write fails                        | Return FS_ERROR with exception        |

    Caller should:
    - On WRITTEN: update `projection_sha256 = outcome.new_sha256`.
    - On DRIFT:   set `projection_dirty = true`; surface `reconcile_projection`.
    - On FS_ERROR: set `projection_dirty = true`.
    """
    content_bytes = content.encode("utf-8")
    content_sha = hashlib.sha256(content_bytes).digest()

    # Read existing file if present.
    existing_bytes: bytes | None = None
    if absolute_path.exists():
        try:
            existing_bytes = absolute_path.read_bytes()
        except OSError as exc:
            return SafeWriteOutcome(result=SafeWriteResult.FS_ERROR, exception=exc)

    # Idempotent: disk already has the exact content we want to write.
    if existing_bytes is not None and existing_bytes == content_bytes:
        return SafeWriteOutcome(result=SafeWriteResult.UNCHANGED, new_sha256=content_sha)

    # Drift detection: only when we have a previously-recorded hash.
    if expected_sha256 is not None and existing_bytes is not None:
        disk_sha = hashlib.sha256(existing_bytes).digest()
        if disk_sha != expected_sha256:
            return SafeWriteOutcome(result=SafeWriteResult.DRIFT)

    # Write (or create).
    try:
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        absolute_path.write_bytes(content_bytes)
    except OSError as exc:
        return SafeWriteOutcome(result=SafeWriteResult.FS_ERROR, exception=exc)

    return SafeWriteOutcome(result=SafeWriteResult.WRITTEN, new_sha256=content_sha)
