"""FastAPI application for the agent-notes web viewer (Plan 003, Phase 8a).

Read-only routes for browsing work items, memories, and workspaces/projects.
Server-rendered HTML via Jinja2 templates (decision 44).
Bound to 127.0.0.1 only (decision 43).
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path

import jinja2
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from agent_notes.core.db import list_projects, list_workspaces

_WEB_TOKEN = os.environ.get("AGENT_NOTES_WEB_TOKEN", "").strip() or None

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

app = FastAPI(title="agent-notes-web", docs_url=None, redoc_url=None)

_UNAUTHORIZED_HTML = """<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>401 Unauthorized</title>
</head>
<body>
    <h1>401 Unauthorized</h1>
    <p>The agent-notes web viewer requires a bearer token. Set it via the
    <code>AGENT_NOTES_WEB_TOKEN</code> environment variable and send
    <code>Authorization: Bearer &lt;token&gt;</code> with each request.</p>
</body>
</html>
"""


@app.middleware("http")
async def bearer_token_middleware(request: Request, call_next):
    """Optional bearer-token gate for the read-only web viewer.

    If ``AGENT_NOTES_WEB_TOKEN`` is unset, all requests pass through so the
    localhost-only default remains backward compatible. When the token is set,
    every request must include ``Authorization: Bearer <token>``. The token and
    scheme comparison uses ``hmac.compare_digest`` to avoid timing side channels.
    Browser requests without an Authorization header receive an HTML 401 page;
    API requests receive a JSON 401.
    """
    if _WEB_TOKEN is None:
        return await call_next(request)

    auth_header = request.headers.get("authorization", "")
    scheme, _, provided = auth_header.partition(" ")
    valid = hmac.compare_digest(scheme.lower(), "bearer") and hmac.compare_digest(
        provided, _WEB_TOKEN
    )
    if valid:
        return await call_next(request)

    accepts = request.headers.get("accept", "")
    wants_html = "text/html" in accepts
    if wants_html:
        return HTMLResponse(content=_UNAUTHORIZED_HTML, status_code=401)
    return JSONResponse({"detail": "Unauthorized"}, status_code=401)


_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=jinja2.select_autoescape(["html"]),
    enable_async=False,
)


def _render(template_name: str, status_code: int = 200, **context) -> HTMLResponse:
    tmpl = _jinja_env.get_template(template_name)
    html = tmpl.render(**context)
    return HTMLResponse(content=html, status_code=status_code)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    workspaces = list_workspaces()
    return _render("index.html", workspaces=workspaces)


@app.get("/workspaces/{workspace_slug}", response_class=HTMLResponse)
def workspace_detail(request: Request, workspace_slug: str):
    ws = _find_workspace(workspace_slug)
    if ws is None:
        return _render("404.html", status_code=404, thing="workspace")
    projects = list_projects(workspace_id=ws.id)
    return _render("workspace.html", workspace=ws, projects=projects)


@app.get("/workspaces/{workspace_slug}/{project_slug}", response_class=HTMLResponse)
def project_detail(request: Request, workspace_slug: str, project_slug: str):
    ws = _find_workspace(workspace_slug)
    if ws is None:
        return _render("404.html", status_code=404, thing="project")
    proj = _find_project(ws.id, project_slug)
    if proj is None:
        return _render("404.html", status_code=404, thing="project")

    breadcrumbs = _query_breadcrumbs(proj.id)
    memories = _query_memories(proj.id, ws.id)
    return _render(
        "project.html",
        workspace=ws,
        project=proj,
        breadcrumbs=breadcrumbs,
        memories=memories,
    )


@app.get(
    "/workspaces/{workspace_slug}/{project_slug}/breadcrumbs/{identifier}",
    response_class=HTMLResponse,
)
def breadcrumb_detail(
    request: Request,
    workspace_slug: str,
    project_slug: str,
    identifier: str,
):
    ws = _find_workspace(workspace_slug)
    proj = _find_project(ws.id, project_slug) if ws else None
    if ws is None or proj is None:
        return _render("404.html", status_code=404, thing="breadcrumb")

    bc = _get_breadcrumb(proj.id, identifier)
    if bc is None:
        return _render("404.html", status_code=404, thing="breadcrumb")
    return _render(
        "breadcrumb.html",
        workspace=ws,
        project=proj,
        breadcrumb=bc,
    )


@app.get(
    "/workspaces/{workspace_slug}/{project_slug}/memories/{name}",
    response_class=HTMLResponse,
)
def memory_detail(
    request: Request,
    workspace_slug: str,
    project_slug: str,
    name: str,
):
    ws = _find_workspace(workspace_slug)
    proj = _find_project(ws.id, project_slug) if ws else None
    if ws is None or proj is None:
        return _render("404.html", status_code=404, thing="memory")

    mem = _get_memory(proj.id, ws.id, name)
    if mem is None:
        return _render("404.html", status_code=404, thing="memory")
    return _render(
        "memory.html",
        workspace=ws,
        project=proj,
        memory=mem,
    )


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = ""):
    results: list[dict] = []
    if q:
        from agent_notes.core.embed import embed

        vec = embed(q, task="query").tolist()
        results = _search_all(vec, limit=20)
    return _render("search.html", query=q, results=results)


# ---------------------------------------------------------------------------
# Helpers — delegate to model layer, post-process for web display
# ---------------------------------------------------------------------------


def _find_workspace(slug: str):
    workspaces = list_workspaces()
    return next((w for w in workspaces if w.slug == slug), None)


def _find_project(workspace_id: int, slug: str):
    projects = list_projects(workspace_id=workspace_id)
    return next((p for p in projects if p.slug == slug), None)


def _query_breadcrumbs(project_id: int) -> list[dict]:
    from agent_notes.core.work_item_model import WorkItemModel

    rows = WorkItemModel.query_work_items(project_id=project_id, limit=200)
    return [_strip_embedding(r) for r in rows]


def _query_memories(project_id: int, workspace_id: int) -> list[dict]:
    from agent_notes.core import memory_model

    return memory_model.list_memories(workspace_id=workspace_id, project_id=project_id, limit=200)


def _get_breadcrumb(project_id: int, identifier: str) -> dict | None:
    from agent_notes.core.work_item_model import WorkItemModel

    row = WorkItemModel.get_work_item(project_id, identifier)
    if row is None:
        return None
    body = WorkItemModel.get_work_item_body(project_id, identifier) or ""
    row = dict(row)
    row["body"] = body
    return _strip_embedding(row)


def _get_memory(project_id: int, workspace_id: int, name: str) -> dict | None:
    from agent_notes.core import memory_model

    return memory_model.get_memory(workspace_id, project_id, name)


def _strip_embedding(row: dict) -> dict:
    """Remove the embedding vector from a work item dict for display."""
    return {k: v for k, v in row.items() if k not in ("embedding", "body_hash")}


def _search_all(query_vec: list[float], limit: int = 20) -> list[dict]:
    from psycopg.rows import dict_row

    from agent_notes.core.db import _conn

    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT kind, identifier, title,
                   embedding <=> %s::vector AS distance,
                   p.slug AS project_slug, w.slug AS workspace_slug
            FROM all_notes_search_v v
            JOIN projects p ON p.id = v.project_id
            JOIN workspaces w ON w.id = v.workspace_id
            WHERE v.embedding IS NOT NULL
            ORDER BY v.embedding <=> %s::vector
            LIMIT %s
            """,
            (query_vec, query_vec, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def create_app() -> FastAPI:
    return app


def main() -> None:
    import uvicorn

    port = int(os.environ.get("AGENT_NOTES_WEB_PORT", "8765"))
    uvicorn.run(app, host="127.0.0.1", port=port)
