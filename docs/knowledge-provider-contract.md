# Knowledge provider contract

**Status:** Foundation implemented 2026-07-12 for dossier Plan 024 Phase 1.
**Public Python surface:** `agent_notes.knowledge_provider`.
**Schema:** `agent-notes.knowledge.v1`.

## Boundary

Suite consumers use `get_knowledge_provider()` and the `KnowledgeProvider`
protocol for read-only browse, detail, search, link traversal, and index posture.
They do not import `agent_notes.core`, query agent-notes tables, initialize the
embedding model, or infer authority and freshness from missing fields.

The provider resolves a requested workspace/project scope, but it does **not**
authenticate a caller or enforce project ACLs. Every in-process caller or future
transport must authenticate and authorize the complete requested scope before
invoking it, and must apply the same authorization to every returned link target.
Scope resolution is not an authorization decision.

Every result is JSON-safe and carries one of the closed states `current`,
`stale`, `partial`, `unavailable`, `unsupported`, or `unknown`. Provider errors
are sanitized; connection strings and exception text are not returned.

## Exact knowledge and search

Browse and detail return exact note text from the current agent-notes read
model. Native/offline notes name `agent-notes-native` as their authority. A row
that references a signed regista note names regista as its authority, but the
foundation deliberately reports its authority state as `unknown`: reading the
local projection does not replay or verify the signed event chain. Pending
outbox synchronization is `stale`; mixed results are `partial`.

This is an honest intermediate boundary, not the final signed-read proof. A
future `RegistaKnowledgeRepository` must read and verify the canonical note
events behind this provider before dossier may present those records as verified
signed knowledge. Dossier must render the returned state and must not substitute
its own proof inference.

Two search modes are explicit:

- `lexical` is deterministic PostgreSQL case-insensitive substring matching
  over exact name/body text. It does not load an embedding model and is not a
  full-text relevance engine.
- `semantic` lazily loads the configured native embedding model and searches
  the pgvector projection. It can be slow on the first request and depends on
  the model/vector stack. Missing vectors make the result `partial`, even when
  the returned matches themselves are valid.

No learned-memory synthesis or external engine is exposed by this foundation.
Those remain governed by Plan 020 and must preserve the distinction between
exact records and derived context.

## Pagination and compatibility

Browse cursors are opaque strings to consumers. Version 1 currently encodes a
non-negative offset, caps pages at 100 records, and returns `next_cursor` when
another page exists. Consumers must not parse the cursor or depend on database
IDs. Additive fields may be introduced within v1; renaming fields or changing
their meaning requires a schema-version bump.

The provider is currently an in-process Python contract. A future HTTP or
subprocess transport should serialize the same versioned result shapes and add
authentication and authorization at the transport boundary. Dossier remains
responsible for checking actor access before every call and before exposing a
cross-scope link; the provider must never be treated as an ACL oracle.
