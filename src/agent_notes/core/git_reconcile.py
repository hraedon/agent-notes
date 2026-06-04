"""Reconcile open breadcrumbs against git history.

The recurring failure mode of the breadcrumb store is *silent resolution*: an
agent finishes the work and records it in a commit message ("resolve BC-094"),
but never tells the DB. The status stays ``new`` for weeks while the work is
done — the DB and reality diverge with no signal.

The fix is to treat the commit message as what it already is: the authoritative
record of what work happened. Rather than enforce a manual step at commit time
(a git hook — undistributed and bound to the wrong moment), we *read* git
history when reconciling and surface the divergence. The agent's existing habit
becomes the oracle instead of a lossy side channel.

This module is deliberately DB-free and read-only: it shells out to ``git log``
and returns suggestions. Applying them is the caller's decision.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# Resolution-intent verbs agents actually write in commit subjects/bodies.
_VERB = (
    r"(?:resolv\w*|close[sd]?|closing|fix(?:e[sd])?|fixing|"
    r"complet\w*|implement\w*|address(?:e[sd])?|done)"
)

# Record/field separators (ASCII unit/record separators) keep subjects and
# bodies unambiguous even when they contain newlines or commas.
_FS = "\x1f"
_RS = "\x1e"

# Negation / not-yet tokens near a match flip its meaning ("not done", "not yet
# fixed", "todo: BC-1", "WIP BC-1", "revert resolve BC-1"). A match within reach
# of one of these is rejected.
_NEGATION = re.compile(
    r"\b(?:not|yet|todo|wip|without|unfinished|unresolved|pending|partial(?:ly)?|revert)\b",
    re.IGNORECASE,
)


def _build_pattern(identifier: str) -> re.Pattern[str]:
    """A commit references *identifier* with resolution intent when a resolution
    verb sits next to it (``resolve BC-094``, ``closes BC-094 and BC-095``) or
    the identifier is followed closely by a past-tense resolution word
    (``BC-094: resolved``, ``BC-094 done``)."""
    idre = re.escape(identifier)
    return re.compile(
        rf"\b{_VERB}\b[^.\n]{{0,40}}\b{idre}\b"
        rf"|\b{idre}\b[^.\n]{{0,40}}\b"
        rf"(?:resolved|fixed|closed|done|complete[d]?|implemented|addressed)\b",
        re.IGNORECASE,
    )


def _has_unnegated_match(pattern: re.Pattern[str], message: str) -> bool:
    """True if *message* contains a resolution match for the pattern that isn't
    negated. A short window before each match (plus the match itself) is checked
    for negation tokens, so "not yet fixed BC-1" and "BC-1 (not done)" are
    rejected while "resolve BC-1" is kept."""
    for m in pattern.finditer(message):
        window = message[max(0, m.start() - 12) : m.end()]
        if _NEGATION.search(window) or "n't" in window.lower():
            continue
        return True
    return False


def scan_git_for_resolutions(
    repo_root: str | Path | None,
    identifiers: list[str],
    *,
    lookback: int = 400,
) -> dict[str, dict[str, str]]:
    """Find open breadcrumbs that a recent commit resolves.

    Returns ``{identifier: {"commit": <short sha>, "subject": <subject>}}`` for
    each identifier referenced with resolution intent, picking the most recent
    such commit. Read-only and fail-safe: returns ``{}`` when ``repo_root`` is
    missing, is not a git work tree, or git is unavailable — reconciliation is
    advisory and must never break the caller.
    """
    if not identifiers or not repo_root:
        return {}
    root = Path(repo_root)
    if not root.is_dir():
        return {}

    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "log",
                "-n",
                str(lookback),
                "--no-merges",
                f"--format=%H{_FS}%s{_FS}%b{_RS}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}

    patterns = {ident: _build_pattern(ident) for ident in identifiers}
    found: dict[str, dict[str, str]] = {}

    # Commits arrive newest-first; the first match for an identifier wins, so a
    # later re-open isn't masked by an older resolution.
    for record in proc.stdout.split(_RS):
        record = record.strip()
        if not record:
            continue
        parts = record.split(_FS)
        if len(parts) < 2:
            continue
        sha, subject = parts[0].strip(), parts[1].strip()
        body = parts[2] if len(parts) > 2 else ""
        message = f"{subject}\n{body}"
        for ident, pat in patterns.items():
            if ident not in found and _has_unnegated_match(pat, message):
                found[ident] = {"commit": sha[:12], "subject": subject}
        if len(found) == len(identifiers):
            break
    return found
