"""Discover which project the caller is standing in.

``db.resolve_project`` has always been able to map a filesystem path to a
registered project (longest-prefix match on ``repo_root``), but nothing ever
handed it the current working directory: ``--path`` defaults to ``None``, and
a command invoked with no ``--path`` / ``--workspace`` / ``--project`` simply
exited ``NOT_CONFIGURED``. So an agent working inside a registered repo still
had to name the project it was obviously in.

This module closes that. It is deliberately a **fallback**, tried only after
every explicit selector has been exhausted, so it can only turn a hard failure
into a resolution — it never overrides something the caller asked for, and it
cannot change the answer for any invocation that already worked.

Unregistered directories stay unresolved: discovery returns ``None`` and the
caller's existing error (with its "run `agent-notes init`" hint) still fires.
Guessing a project from an unregistered cwd is exactly how a breadcrumb gets
filed against the wrong project.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DiscoveredProject:
    """A project resolved from a filesystem path."""

    workspace: str
    project: str
    repo_root: str
    #: ``"exact"`` when the path *is* the project root, ``"ancestor"`` when it
    #: is somewhere inside one. Callers surface this so an unregistered project
    #: cannot masquerade as an exact match.
    resolved_via: str

    @classmethod
    def from_resolution(cls, data: dict[str, Any]) -> DiscoveredProject:
        return cls(
            workspace=str(data["workspace"]),
            project=str(data["project"]),
            repo_root=str(data["repo_root"]),
            resolved_via=str(data.get("resolved_via", "ancestor")),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "workspace": self.workspace,
            "project": self.project,
            "repo_root": self.repo_root,
            "resolved_via": self.resolved_via,
        }


def discover_project(start: str | None = None) -> DiscoveredProject | None:
    """Resolve the project containing ``start`` (default: the cwd).

    Returns ``None`` when the path belongs to no registered project, or when
    the registry cannot be reached — discovery is a convenience, so it must
    never be the thing that turns a working command into a crash. The caller's
    own error path reports the failure.
    """
    path = start if start is not None else os.getcwd()
    try:
        from agent_notes.core.db import resolve_project

        return DiscoveredProject.from_resolution(resolve_project(path))
    except ValueError:
        # PROJECT_NOT_REGISTERED — the caller reports this, with its hint.
        return None
    except Exception:
        # Registry unreachable / misconfigured. Same reasoning: fall through
        # to the caller's existing diagnosis rather than raising a new one.
        return None
