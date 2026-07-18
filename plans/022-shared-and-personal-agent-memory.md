# Plan 022 — Shared and personal agent memory

**Status:** Proposed 2026-07-18.
**Owner:** agent-notes owns exact-memory authoring, learned-memory scope mapping,
authorized recall composition, publication, deletion, and provider conformance.
**Depends:** Plan 018 (signed note entities), Plan 020 (pluggable learned-memory
engine), agent-suite Plan 017 (agent workload identity and secret isolation),
regista's principal/delegation and signed-note contracts.
**Amends:** Plan 020's scope requirements and Hindsight bank mapping. Where this
plan is more specific about scope, identity, visibility, or isolation, this plan
controls.

## 1. Outcome

An authenticated agent working in a project receives a deliberately composed
memory view:

1. organization knowledge shared with authorized members;
2. knowledge shared by the current project;
3. durable memories personal to that stable agent principal; and
4. optional session-local working memory that expires by policy.

Two agents may work for different humans in the same project, see the same
authorized shared knowledge, and retain different personal memories without
being able to retrieve, overwrite, infer, or delete one another's personal
material.

“Personal” means owned by and private from other ordinary agents. It does not
mean invisible to an authorized operator, exempt from retention/deletion,
available for secrets, or able to hide authoritative project facts.

## 2. Current gap

The current exact-memory projection is project-only:

- every row requires `workspace_id` and `project_id`;
- cross-project knowledge uses a conventional `global` project;
- the active-name constraint is `(project_id, name)`;
- actor identity is signed provenance, not memory ownership or read authority;
- native recall resolves only workspace and project.

Plan 020 introduced `workspace`, `project`, `user`, `agent`, and `session` fields,
but the current provider adapters do not implement those as independent
authority boundaries. In particular, the Hindsight adapter:

- derives a bank from a sanitized project slug without the workspace identity;
- uses user and agent identifiers as tags rather than bank boundaries;
- ignores session identity;
- cannot compose organization, project, and personal banks in one recall;
- deletes the project bank for a scope-wide forget request; and
- does not return the effective source scope on each recalled result.

Those behaviors are sufficient for the existing project-only compatibility
path. They are not a personal-memory security model.

## 3. Identity vocabulary

Memory authorization uses the canonical suite identities; it never trusts an
`agent_id` supplied by a prompt, note body, tool argument, or arbitrary CLI flag.

| Identifier | Meaning | Lifecycle |
|---|---|---|
| `human_principal_id` | Stable workplace identity of the delegating human | Identity-source managed |
| `agent_principal_id` | Stable logical identity of one agent | Explicit enrollment, transfer, revocation |
| `agent_instance_id` | One installed workload allowed to act as that agent | Rotatable/revocable independently |
| `actor_id` | Event author identity recorded on a signed operation | Bound to authenticated principal |
| `session_id` | One execution/session | Short-lived; never a durable agent identity |
| `model_lineage` | Model/provider/version evidence | Descriptive; never an owner or credential subject |

An agent principal is independent of its delegating human. User A's agent does
not write as User A and does not use User A's signing or backend credential. The
signed delegation records that the agent acted on behalf of User A.

A model or harness upgrade does not silently create or merge agent identity.
Continuity is an explicit operator decision. Cloning an agent installation
creates a new `agent_instance_id`; sharing the same logical principal requires
an explicit enrollment and separately revocable credential.

## 4. Scope, ownership, and visibility

Scope, owner, author, and visibility are separate dimensions.

### 4.1 Closed scope kinds

| Scope kind | Stable scope ID | Default audience | Intended content |
|---|---|---|---|
| `organization` | workspace/organization UUID | Authorized organization members | Standards and cross-project lessons |
| `project` | project UUID | Authorized project members | Decisions, conventions, architecture, known issues |
| `agent` | agent-principal UUID | Owning agent plus authorized administration | Personal preferences, recurring lessons, working heuristics |
| `session` | session UUID | Current authorized agent/session | Ephemeral working context |

`user` is not a durable memory-bank kind in the first version. Human preferences
belong to an exact human-owned profile contract if needed later; using a human's
bank as an agent's personal bank would blur delegation and ownership.

### 4.2 Exact memory fields

Every durable exact memory source must carry or resolve:

- `scope_kind` and opaque `scope_id`;
- `owner_principal_id` where the scope is personal;
- `author_actor_id` and signed delegation/provenance;
- `visibility` (`shared`, `owner_only`, or an explicit policy reference);
- classification, retention policy, creation/update time, and source identity;
- authority class (`exact`) and current lifecycle state.

Display names and slugs are metadata. They are never isolation keys.

The exact-note repository remains authoritative. Learned banks are rebuildable
projections with deletion receipts and visible indexing state.

### 4.3 Naming and supersession

The active exact-memory identity is `(scope_kind, scope_id, name)`, not
`(project_id, name)`. Two agents may use the same personal memory name without
collision. Organization and project records retain shared-name conflict rules.

Publishing a personal memory does not change its scope in place. It creates a
new signed shared record with a link to the personal source when disclosure is
permitted, preserving both histories and the explicit act of publication.

## 5. Authorization contract

The memory API receives an `AccessContext` resolved from authenticated workload
identity. At minimum it contains:

- `agent_principal_id` and `agent_instance_id`;
- delegating `human_principal_id`, when acting on behalf of a human;
- authorized organization and project IDs;
- current session ID;
- roles/capabilities and authentication assurance;
- policy/configuration version and correlation ID.

Callers request a purpose and target scope. Agent-notes resolves whether that
scope is authorized. A caller-provided owner or tag cannot broaden access.

Rules:

- organization reads require organization membership;
- project reads/writes require project authorization;
- agent-scope reads/writes require the authenticated agent principal to equal
  the scope owner, unless an explicit administrative operation is authorized;
- session scope requires both the authenticated agent and current session;
- administrative inspection/export/deletion is a named, audited capability;
- learned-provider access occurs only after authorization and cannot replace it;
- an unavailable identity/policy service fails closed for personal or shared
  cross-principal access.

The database connection, provider token, or possession of a DSN is not itself
memory authorization.

## 6. Provider bank mapping

Every authority scope maps to an independent provider-native isolation boundary
by default. Tags remain retrieval metadata, never the tenant boundary.

Provider bank keys are opaque, versioned, and injective. A conceptual input is:

```text
bank_key_v1 = encode(
  deployment_id,
  organization_uuid,
  scope_kind,
  scope_uuid
)
```

The encoded key may be a provider-safe hash plus a protected mapping record. It
must not be produced by lossy slug sanitization. Workspace and deployment
identity prevent equal project slugs in different workspaces or deployments from
colliding.

Required banks for an active project session are normally:

```text
organization:<organization_uuid>
project:<project_uuid>
agent:<agent_principal_uuid>
session:<session_uuid>        # only when durable session memory is enabled
```

If a provider cannot isolate these banks or authorize mediated access safely, it
cannot qualify for personal memory. It may remain qualified for a narrower
project-only profile whose limitation is explicit.

## 7. Composed recall

Agent-notes, not the harness and not agent-suite, composes recall across
authorized banks.

The request declares:

- authenticated `AccessContext`;
- current organization/project/session;
- query and purpose;
- selected scopes, defaulting to organization + project + personal;
- deterministic token/result budgets per scope;
- permitted classifications and freshness requirements.

The response identifies for every result:

- effective `scope_kind` and opaque `scope_id`;
- owner where disclosure is authorized;
- exact source reference and source time, when one exists;
- provider, provider bank reference, origin class, and indexing freshness;
- classification and authority (`exact` or `learned`);
- ranking contribution and any deduplication/supersession decision.

Provider scores from different banks are not assumed comparable. Composition
uses explicit per-scope quotas and a documented merge policy. Personal memory
does not automatically outrank project truth. On conflict, exact current shared
records remain authoritative and the conflict is visible.

Recalled text stays untrusted content. Its bank or ownership does not make it an
instruction or capability grant.

## 8. Capture and publication policy

Durable capture is intentional:

- project decisions, commitments, defects, risks, and shared operating facts
  default to project or organization exact records;
- agent-specific preferences and reusable working heuristics may be saved to the
  personal exact store and projected into the personal learned bank;
- session observations remain ephemeral unless an authorized explicit save
  selects personal or shared scope;
- raw transcripts are not captured under this plan;
- secrets and raw credentials are prohibited from every memory scope;
- sensitive incident or personal data uses its governed source store and may be
  referenced only according to policy.

Promotion from personal to shared is a `publish` workflow:

1. identify the personal source and intended shared scope;
2. show the material that will be disclosed;
3. apply classification/redaction and any required human review;
4. create a new signed shared exact record;
5. ingest that shared source into the shared learned bank;
6. preserve a permitted provenance link and independent deletion semantics.

Recall, ranking, repeated use, or another agent's agreement never publishes a
personal memory implicitly.

## 9. Deletion, transfer, and decommissioning

- Forgetting one exact source removes all provider-derived artifacts for that
  source and returns a receipt.
- Scope-wide deletion targets exactly one opaque bank ID. A personal deletion
  cannot name or delete a project/organization bank.
- Session banks expire and are verified absent after their retention window.
- Revoking an agent immediately blocks new personal-bank access without erasing
  retained records that policy requires.
- Transferring an agent to another human is a signed delegation change; personal
  memory does not move to a different organization automatically.
- Decommissioning supports export where policy permits, provider deletion,
  derived-artifact verification, exact-record retention/deletion, and closure of
  workload credentials.
- An administrator cannot silently reassign one agent's bank to another agent.

## 10. Work plan

### WI-0.1 — Freeze identity and scope contracts

Define `AccessContext`, scope/visibility/owner fields, stable opaque identifiers,
authorization decisions, publication events, and scope-bearing recall results.
Amend the memory-provider contract and note-entity contract before changing a
provider adapter.

**AC:** closed-schema tests reject prompt-supplied identity, missing owner for
personal scope, lossy/display-name bank keys, results without effective scope,
and visibility changes masquerading as publication.

### WI-0.2 — Threat model and migration decision

Threat-model cross-agent recall, overwrite, inference, enumeration, deletion,
provider compromise, confused deputy, stale delegation, cloned credentials,
operator access, and data remanence. Define migration of existing project rows
and the conventional `global` project without silently reclassifying content.

**AC:** current project memories remain project exact records; `global` content
requires an explicit reviewed migration to organization scope; unknown ownership
does not become personal by inference.

### WI-1.1 — Exact repository scope support

Add scope/owner/visibility to signed note payloads and projections. Change
uniqueness, CRUD, list/search, links, export/import, rebuild, and deletion to use
the scope identity. Enforce authorization at the public service/CLI boundary.

**AC:** agents with identical memory names do not collide; direct SQL/provider
possession is not presented as authorized API access; rebuild preserves scope,
owner, author, and publication history.

### WI-1.2 — Provider-neutral bank and recall composition

Replace project-slug bank mapping with opaque injective scope mapping. Add
multi-bank recall, per-scope budgets, source labels, conflict handling, and exact
bank deletion selectors.

**AC:** same-slug cross-workspace and sanitizer-collision fixtures remain
isolated; personal forget cannot delete shared banks; every result round-trips
effective scope and source.

### WI-2.1 — Native and Hindsight implementations

Implement the complete contract in native and Hindsight adapters. Use provider
tags only within an already authorized isolated bank. Record provider/profile
capabilities so project-only and personal-memory qualification remain distinct.

**AC:** both implementations pass the same conformance fixture; a provider that
cannot implement personal isolation reports that capability absent rather than
simulating it with tags.

### WI-2.2 — Capture, publish, and lifecycle surfaces

Add explicit scope selection to memory save, an authorized personal-to-shared
publish command, scope-aware orientation, export/delete/decommission operations,
and honest doctor output.

**AC:** ordinary orientation composes only authorized banks; publishing creates a
new shared signed source; no raw transcript or secret is captured; revoked agents
cannot access a personal bank.

### WI-3.1 — Live multi-agent qualification

Run the released service and provider with two humans, at least two agents, two
workspaces containing equal project slugs, colliding display slugs, and
concurrent sessions.

**AC:**

- Agents A and B see authorized organization/project facts.
- A sees A-personal and never B-personal; B sees B-personal and never A-personal.
- Same names across personal banks do not overwrite.
- Wrong-agent reads, writes, searches, exports, publish, and deletes fail closed.
- Same-slug workspaces and lossy-slug fixtures do not collide.
- Session expiry, source deletion, derived cascade, transfer, revocation, and
  provider restart are proven.
- Provider and service audit evidence correlates each operation to the
  authenticated agent workload identity from agent-suite Plan 017.

## 11. Release and migration gates

Personal memory remains disabled until:

- the exact and learned contracts carry scope and ownership end to end;
- unique agent workload identity is authenticated rather than asserted;
- native and selected external providers pass cross-agent negatives;
- provider bank IDs are opaque and collision-tested;
- administrative access, retention, deletion, export, and decommissioning are
  documented and exercised;
- a rollback can return to project-only memory without merging or exposing
  personal banks.

Existing installations stay project-only during migration. Adding `agent_id`
tags or creating projects named after agents is not an interim personal-memory
implementation.

## 12. Non-goals

- Treating model lineage, harness, display name, or session as agent identity.
- Giving every agent a hidden autobiographical profile or capturing all chats.
- Making learned memory authoritative over signed exact records.
- Storing secrets in memory because the bank is called personal.
- Hiding personal memories from duly authorized administration or retention
  policy.
- Reimplementing provider SDKs, identity providers, or secret backends in
  agent-notes.
