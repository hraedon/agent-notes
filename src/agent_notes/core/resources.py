"""MCP resources scaffolding (Phase 1b.5).

Provides URI parsing/building and a helper for kind servers to register
consistent `note://` URI handlers via the `Server` base class's
`register_resource_handler`.

No actual kind exposes resources yet — breadcrumbs and memories get real
resource handlers in Phase 2b / Phase 3 respectively. This module is
scaffolding so kind servers have a uniform registration pattern from day one.

URI scheme: note://kind/workspace/project/identifier
Shorter prefixes like note://kind/workspace/ are used for listing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

# ---------------------------------------------------------------------------
# URI model
# ---------------------------------------------------------------------------


@dataclass
class ResourceURI:
    kind: str
    workspace: str
    project: str | None = None
    identifier: str | None = None

    def is_listing(self) -> bool:
        """True when identifier is absent (the URI is a collection prefix)."""
        return self.identifier is None

    def __str__(self) -> str:
        return build_uri(self.kind, self.workspace, self.project, self.identifier)


def parse_uri(uri: str) -> ResourceURI:
    """Parse `note://kind/workspace[/project[/identifier]]`.

    Raises ValueError for malformed or unsupported URIs.
    """
    if not uri.startswith("note://"):
        raise ValueError(f"Unsupported URI scheme: {uri!r}")

    path = uri[len("note://"):]
    parts = [p for p in path.split("/") if p]

    if not parts:
        raise ValueError(f"URI missing kind segment: {uri!r}")
    if len(parts) < 2:
        raise ValueError(f"URI missing workspace segment: {uri!r}")

    kind = parts[0]
    workspace = parts[1]
    project = parts[2] if len(parts) >= 3 else None
    identifier = parts[3] if len(parts) >= 4 else None

    return ResourceURI(kind=kind, workspace=workspace, project=project, identifier=identifier)


def build_uri(
    kind: str,
    workspace: str,
    project: str | None = None,
    identifier: str | None = None,
) -> str:
    """Build a `note://` URI from components."""
    uri = f"note://{kind}/{workspace}"
    if project is not None:
        uri += f"/{project}"
        if identifier is not None:
            uri += f"/{identifier}"
    return uri


# ---------------------------------------------------------------------------
# Kind server registration helper
# ---------------------------------------------------------------------------


def make_resource_handler(
    kind: str,
    list_fn: Callable[[str, str], list[dict]],
    read_fn: Callable[[str, str, str], str],
) -> Callable[[str, str], Any]:
    """Return a handler function suitable for `Server.register_resource_handler`.

    The returned handler is called by `Server._handle_resources_list` and
    `Server._handle_resources_read` with `(action, uri_or_prefix)`.

    `list_fn(workspace, project) -> list[{uri, name, description, mimeType}]`
    `read_fn(workspace, project, identifier) -> str content`

    Kind servers that expose resources call::

        server.register_resource_handler(
            f"note://{kind}/",
            make_resource_handler(kind, list_fn, read_fn),
        )
    """

    def handler(action: str, uri_or_prefix: str) -> Any:
        if action == "list":
            # `uri_or_prefix` is the registered prefix, e.g. "note://breadcrumb/"
            # Return an empty list; real listing requires workspace/project context
            # which isn't available at the prefix level. Kind servers override
            # this with a workspace-aware listing strategy in Phase 2b+.
            return []

        if action == "read":
            parsed = parse_uri(uri_or_prefix)
            if parsed.kind != kind:
                raise ValueError(f"Handler for {kind!r} received URI for {parsed.kind!r}")
            if parsed.project is None or parsed.identifier is None:
                raise ValueError(
                    f"URI must include project and identifier for read: {uri_or_prefix!r}"
                )
            return read_fn(parsed.workspace, parsed.project, parsed.identifier)

        raise ValueError(f"Unknown action: {action!r}")

    return handler
