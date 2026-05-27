"""Web frontend for agent-notes (Plan 003, Phase 8a).

Read-only viewer. FastAPI + Jinja2, localhost-only (decision 43).
"""

from agent_notes.web.app import create_app

__all__ = ["create_app"]
