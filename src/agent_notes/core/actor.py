"""Actor resolution for the regista face (Plan 009, D3).

agent-notes has no auth (decision 43). The agent face constructs an ``Actor``
whose identity is trust-rooted in environment / git config — never in prompt
input. This is the agent-notes analog of dossier's session-resolved Actor
(``dossier/src/dossier/auth/resolver.py``): provenance guarantee G1 (attribution)
is only as good as this binding, so the binding is structural — callers cannot
pass an ``actor_id`` string into the face; they pass an ``Actor`` resolved here.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

_ACTOR_ID_ENV = "AGENT_NOTES_ACTOR_ID"
_PRINCIPAL_ID_ENV = "AGENT_NOTES_PRINCIPAL_ID"
_PRINCIPAL_KIND_ENV = "AGENT_NOTES_PRINCIPAL_KIND"
_PRINCIPAL_DISPLAY_ENV = "AGENT_NOTES_PRINCIPAL_DISPLAY_NAME"
_MODEL_LINEAGE_ENV = "AGENT_NOTES_MODEL_LINEAGE"
_DEFAULT_ACTOR_ID = "agent-notes"


def _git_config(key: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "config", "--get", key],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    val = out.stdout.strip()
    return val or None


@dataclass(frozen=True, slots=True)
class Actor:
    actor_id: str
    actor_kind: str = "agent"
    display_name: str = ""
    on_behalf_of: dict | None = None
    role: str = "agent"
    model_lineage: str | None = None

    def actor_metadata(self) -> dict:
        meta = {"display_name": self.display_name or self.actor_id, "role": self.role}
        if self.model_lineage:
            meta["model_lineage"] = self.model_lineage
        return meta


@dataclass(frozen=True, slots=True)
class ActorConfig:
    actor_id: str
    principal_id: str | None = None
    principal_kind: str = "human"
    principal_display_name: str | None = None
    model_lineage: str | None = None
    signer_overrides: dict = field(default_factory=dict)


def load_actor_config() -> ActorConfig:
    cfg = ActorConfig(
        actor_id=os.environ.get(_ACTOR_ID_ENV) or _DEFAULT_ACTOR_ID,
        principal_id=os.environ.get(_PRINCIPAL_ID_ENV),
        principal_kind=os.environ.get(_PRINCIPAL_KIND_ENV) or "human",
        principal_display_name=os.environ.get(_PRINCIPAL_DISPLAY_ENV),
        model_lineage=os.environ.get(_MODEL_LINEAGE_ENV) or None,
    )
    if cfg.principal_id is None:
        email = _git_config("user.email")
        if email:
            object.__setattr__(cfg, "principal_id", email)
    if cfg.principal_display_name is None:
        name = _git_config("user.name")
        if name:
            object.__setattr__(cfg, "principal_display_name", name)
    return cfg


def resolve_actor(config: ActorConfig | None = None) -> Actor:
    cfg = config or load_actor_config()
    on_behalf_of: dict | None = None
    if cfg.principal_id:
        on_behalf_of = {
            "principal_id": cfg.principal_id,
            "principal_kind": cfg.principal_kind,
            "principal_display_name": cfg.principal_display_name or cfg.principal_id,
        }
    return Actor(
        actor_id=cfg.actor_id,
        actor_kind="agent",
        display_name=cfg.principal_display_name or cfg.actor_id,
        on_behalf_of=on_behalf_of,
        model_lineage=cfg.model_lineage,
    )


def migration_actor() -> Actor:
    return Actor(
        actor_id="agent-notes-migration",
        actor_kind="system",
        display_name="agent-notes migration",
        role="system",
    )
