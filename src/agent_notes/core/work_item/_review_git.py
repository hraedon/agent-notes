"""Immutable Git targeting for delegated adversarial review (Plan 023)."""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


class ReviewGitError(ValueError):
    """A requested review range is missing, ambiguous, or unsafe."""


@dataclass(frozen=True, slots=True)
class ResolvedGitTarget:
    repository_root: Path
    base_sha: str
    head_sha: str
    merge_base_sha: str
    changed_paths: tuple[str, ...]


def _git(root: Path, *args: str, timeout: float = 15.0) -> bytes:
    try:
        result = subprocess.run(
            ("git", "-C", str(root), *args),
            check=False,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReviewGitError(f"git command failed: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReviewGitError(detail or f"git {' '.join(args)} exited {result.returncode}")
    return result.stdout


def _decode_line(value: bytes, field: str) -> str:
    try:
        return value.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise ReviewGitError(f"{field} is not valid UTF-8") from exc


def _resolve_commit(root: Path, revision: str, field: str) -> str:
    if not revision or "\x00" in revision:
        raise ReviewGitError(f"{field} revision is empty or invalid")
    output = _git(root, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}")
    sha = _decode_line(output, field)
    if len(sha) not in (40, 64) or any(c not in "0123456789abcdef" for c in sha):
        raise ReviewGitError(f"{field} did not resolve to a full commit object ID")
    return sha


def resolve_git_target(
    repository: Path,
    base_revision: str,
    head_revision: str,
) -> ResolvedGitTarget:
    """Resolve a clean, local, ancestor-based review range to immutable IDs."""
    requested = repository.resolve()
    root_text = _decode_line(_git(requested, "rev-parse", "--show-toplevel"), "repository root")
    root = Path(root_text).resolve()
    if requested != root:
        raise ReviewGitError(f"repository path must be the Git root: {root}")

    dirty = _git(root, "status", "--porcelain=v1", "--untracked-files=normal")
    if dirty:
        raise ReviewGitError("operator worktree is dirty")

    base_sha = _resolve_commit(root, base_revision, "base")
    head_sha = _resolve_commit(root, head_revision, "head")

    ancestor = subprocess.run(
        ("git", "-C", str(root), "merge-base", "--is-ancestor", base_sha, head_sha),
        check=False,
        capture_output=True,
        timeout=15.0,
    )
    if ancestor.returncode == 1:
        raise ReviewGitError("base commit is not an ancestor of head commit")
    if ancestor.returncode != 0:
        detail = ancestor.stderr.decode("utf-8", errors="replace").strip()
        raise ReviewGitError(detail or "could not compare base and head")

    merge_base = _decode_line(_git(root, "merge-base", base_sha, head_sha), "merge base")
    raw_paths = _git(root, "diff", "--name-only", "-z", base_sha, head_sha)
    try:
        paths = tuple(
            sorted(
                part.decode("utf-8", errors="strict") for part in raw_paths.split(b"\x00") if part
            )
        )
    except UnicodeDecodeError as exc:
        raise ReviewGitError("changed path is not valid UTF-8") from exc

    return ResolvedGitTarget(
        repository_root=root,
        base_sha=base_sha,
        head_sha=head_sha,
        merge_base_sha=merge_base,
        changed_paths=paths,
    )


def assert_disposable_worktree_integrity(path: Path, expected_head: str) -> None:
    actual = _decode_line(_git(path, "rev-parse", "HEAD"), "review worktree HEAD")
    if actual != expected_head:
        raise ReviewGitError("reviewer changed the disposable worktree HEAD")
    if _git(path, "status", "--porcelain=v1", "--untracked-files=normal"):
        raise ReviewGitError("reviewer modified the disposable worktree")


@contextlib.contextmanager
def disposable_review_worktree(target: ResolvedGitTarget) -> Iterator[Path]:
    """Materialize ``target.head_sha`` without exposing the operator checkout."""
    parent = Path(tempfile.mkdtemp(prefix="agent-notes-review-"))
    checkout = parent / "checkout"
    added = False
    remove_failed = False
    try:
        _git(
            target.repository_root,
            "worktree",
            "add",
            "--detach",
            str(checkout),
            target.head_sha,
            timeout=60.0,
        )
        added = True
        yield checkout
    finally:
        if added:
            try:
                _git(
                    target.repository_root,
                    "worktree",
                    "remove",
                    "--force",
                    str(checkout),
                    timeout=30.0,
                )
            except ReviewGitError:
                remove_failed = True
        shutil.rmtree(parent, ignore_errors=True)
        if remove_failed:
            try:
                _git(
                    target.repository_root,
                    "worktree",
                    "prune",
                    "--expire=now",
                    timeout=30.0,
                )
            except ReviewGitError:
                pass
