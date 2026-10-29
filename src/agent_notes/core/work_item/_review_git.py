"""Immutable Git targeting for delegated adversarial review (Plan 023)."""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

DIFF_BUDGET_ENV = "AGENT_NOTES_REVIEW_DIFF_BUDGET"
DEFAULT_DIFF_BUDGET_BYTES = 48 * 1024
MAX_DIFF_BUDGET_BYTES = 1024 * 1024
MAX_STAT_BYTES = 16 * 1024
MAX_NUMSTAT_BYTES = 4 * 1024 * 1024
MAX_DIFF_FILES = 1000
_DIFF_FLAGS = (
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--no-renames",
    "--diff-algorithm=myers",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)


class ReviewGitError(ValueError):
    """A requested review range is missing, ambiguous, or unsafe."""


@dataclass(frozen=True, slots=True)
class OmittedDiffFile:
    """A changed file whose diff did not fit the byte budget (None counts: binary)."""

    path: str
    added: int | None
    deleted: int | None


@dataclass(frozen=True, slots=True)
class ReviewDiff:
    """A bounded, deterministic rendering of the base-to-head diff (Plan 025 6a).

    ``patch`` holds only whole per-file diffs, in path order. Binary files are
    a one-line summary, never content. ``omitted`` lists every file left out.
    """

    budget_bytes: int
    stat: str
    patch: str
    included_paths: tuple[str, ...]
    omitted: tuple[OmittedDiffFile, ...]

    @property
    def patch_bytes(self) -> int:
        return len(self.patch.encode("utf-8"))

    @property
    def stat_digest(self) -> str:
        return hashlib.sha256(self.stat.encode("utf-8")).hexdigest()

    @property
    def patch_digest(self) -> str:
        return hashlib.sha256(self.patch.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ResolvedGitTarget:
    repository_root: Path
    base_sha: str
    head_sha: str
    merge_base_sha: str
    changed_paths: tuple[str, ...]
    diff: ReviewDiff | None = None


def diff_budget_from_env(environ: Mapping[str, str] | None = None) -> int:
    """Read AGENT_NOTES_REVIEW_DIFF_BUDGET; invalid values fall back to the default."""
    raw = (os.environ if environ is None else environ).get(DIFF_BUDGET_ENV, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_DIFF_BUDGET_BYTES
    if value < 0:
        return DEFAULT_DIFF_BUDGET_BYTES
    return min(value, MAX_DIFF_BUDGET_BYTES)


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


def _git_capped(root: Path, *args: str, limit: int, timeout: float = 30.0) -> tuple[bytes, bool]:
    """Run git, reading at most ``limit`` bytes of stdout; returns (data, truncated)."""
    argv = (
        "git",
        "-C",
        str(root),
        "--literal-pathspecs",
        "-c",
        "core.quotePath=false",
        *args,
    )
    with tempfile.TemporaryFile() as stderr:
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=stderr)
        except OSError as exc:
            raise ReviewGitError(f"git command failed: {exc}") from exc
        timer = threading.Timer(timeout, proc.kill)
        timer.start()
        try:
            assert proc.stdout is not None
            data = proc.stdout.read(limit + 1)
            truncated = len(data) > limit
            if truncated:
                proc.kill()
                data = data[:limit]
            proc.stdout.close()
            code = proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if not truncated and code != 0:
            stderr.seek(0)
            detail = stderr.read().decode("utf-8", errors="replace").strip()
            raise ReviewGitError(detail or f"git {args[0]} exited {code}")
    return data, truncated


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
    *,
    require_clean: bool = True,
    diff_budget_bytes: int = DEFAULT_DIFF_BUDGET_BYTES,
) -> ResolvedGitTarget:
    """Resolve a clean, local, ancestor-based review range to immutable IDs.

    ``require_clean=False`` skips the operator-worktree check for callers that
    review fetched commits by SHA in a shared checkout (the Plan 025
    dispatcher); the review still runs in a detached worktree at ``head``.
    ``diff_budget_bytes`` bounds the unified diff carried on the target.
    """
    requested = repository.resolve()
    root_text = _decode_line(_git(requested, "rev-parse", "--show-toplevel"), "repository root")
    root = Path(root_text).resolve()
    if requested != root:
        raise ReviewGitError(f"repository path must be the Git root: {root}")

    if require_clean:
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
        diff=_collect_diff(root, base_sha, head_sha, diff_budget_bytes),
    )


def _decode_text(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


def _parse_numstat(raw: bytes) -> list[tuple[str, int | None, int | None]]:
    entries: list[tuple[str, int | None, int | None]] = []
    for record in raw.split(b"\x00"):
        if not record:
            continue
        parts = record.split(b"\t", 2)
        if len(parts) != 3:
            raise ReviewGitError("unexpected git diff --numstat output")
        added, deleted, path = parts
        try:
            counts = (
                None if added == b"-" else int(added),
                None if deleted == b"-" else int(deleted),
            )
        except ValueError as exc:
            raise ReviewGitError("unexpected git diff --numstat output") from exc
        entries.append((path.decode("utf-8", errors="replace"), *counts))
    entries.sort(key=lambda entry: entry[0])
    return entries


def _collect_diff(root: Path, base_sha: str, head_sha: str, budget: int) -> ReviewDiff:
    """Build the bounded diff: whole per-file patches in path order, then an omitted list.

    Files that do not fit are skipped (a later, smaller file may still fit), so
    a huge generated file cannot crowd out the rest of the change. The result
    depends only on the two commits, the budget and the fixed flags above.
    """
    budget = max(0, min(budget, MAX_DIFF_BUDGET_BYTES))
    stat_raw, stat_cut = _git_capped(
        root,
        "diff",
        "--stat=200,160",
        *_DIFF_FLAGS,
        base_sha,
        head_sha,
        limit=MAX_STAT_BYTES,
    )
    stat = _decode_text(stat_raw)
    if stat_cut:
        stat = stat[: stat.rfind("\n") + 1] + "... (stat truncated)\n"

    numstat_raw, numstat_cut = _git_capped(
        root,
        "diff",
        "--numstat",
        "-z",
        *_DIFF_FLAGS,
        base_sha,
        head_sha,
        limit=MAX_NUMSTAT_BYTES,
    )
    if numstat_cut:
        raise ReviewGitError("too many changed files to summarize")

    chunks: list[str] = []
    included: list[str] = []
    omitted: list[OmittedDiffFile] = []
    used = 0
    for index, (path, added, deleted) in enumerate(_parse_numstat(numstat_raw)):
        patch: str | None = None
        if added is None or deleted is None:
            patch = f"Binary file changed (content not shown): {path}\n"
        elif index < MAX_DIFF_FILES and used < budget:
            data, cut = _git_capped(
                root,
                "diff",
                "-U3",
                *_DIFF_FLAGS,
                base_sha,
                head_sha,
                "--",
                path,
                limit=budget - used,
            )
            if not cut:
                patch = _decode_text(data)
        if patch is not None and not patch.endswith("\n"):
            patch += "\n"
        if patch is not None and used + len(patch.encode("utf-8")) <= budget:
            used += len(patch.encode("utf-8"))
            chunks.append(patch)
            included.append(path)
        else:
            omitted.append(OmittedDiffFile(path=path, added=added, deleted=deleted))
    return ReviewDiff(
        budget_bytes=budget,
        stat=stat,
        patch="".join(chunks),
        included_paths=tuple(included),
        omitted=tuple(omitted),
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
