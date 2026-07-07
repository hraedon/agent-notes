# Plan 018 — Knowledge legibility: agent knowledge a human can read

**Status:** Proposed 2026-07-07.
**Author:** Claude (Fable 5), from the 2026-07-07 suite v2 gaps review
**Strategic role:** agent-notes holds the suite's institutional knowledge —
breadcrumbs, memories, reflections — and only agents can read it. A human
teammate (or auditor) asking "what do the agents know about this system?" or
"what open problems have they filed?" has no surface at all: the knowledge is
in the store, but locked behind an agent-facing CLI. The convergence made
agent-notes a face over regista precisely so the *same* record could have two
faces; this plan finishes that promise for the knowledge entities, pairing with
dossier Plan 009 (knowledge-note entity) so the human face can render them.

## Ground truth at time of writing

- Breadcrumbs and work-item lifecycle write through regista (Plan 010
  canonical workflow; `breadcrumb` is a first-class type). Memories and
  reflections are agent-notes-local (Postgres + pgvector projection) and do not
  exist as regista entities.
- dossier Plan 009 (proposed 2026-06-28) sketches a `note` entity from the
  dossier side — the "Confluence seam." It has not started. The two plans are
  one feature seen from the two faces; they should land as one coordinated
  change with regista owning the entity shape.
- regista Plan 022 generalized entities (`entity(kind, id)`), so a non-workflow
  `note` entity is architecturally provided for; dossier Plan 006 already
  decided memory becomes "a referenceable non-workflow regista entity."

## Principles

- **One record, two faces — for knowledge too.** A breadcrumb filed by an agent
  is the same signed entity a human reads in dossier; no export/sync pipeline.
- **Embeddings are a projection, not the record.** pgvector search stays the
  agent face's private index; the human face reads the signed entities, never
  the projection.
- **Write-path stays agent-first.** v2 scope is human *read* (browse, search,
  link); human authoring of notes can come later via dossier once reading has
  proven the shape.

---

## Phase 1 — Knowledge entities in the store

### WI-1.1 — `note` entity shape (with regista + dossier)
- Agree the entity contract with regista (owner of the shape) and dossier
  (Plan 009): kind (`note`), subtypes (breadcrumb / memory / reflection),
  body, links (work items, files, other notes), provenance (author actor,
  session). Breadcrumbs likely stay workflow work-items (they have lifecycle);
  memories/reflections are non-workflow entities. Decide and document the split
  explicitly.
- **AC:** the contract doc exists in regista; both faces reference it; no face
  defines its own divergent copy (the Plan 010 lesson — divergence between
  faces was invisible until a cross-face test existed).

### WI-1.2 — Memories and reflections write through
- agent-notes writes memories and reflections as signed regista entities
  (behind a flag first, like `AGENT_NOTES_REGISTA_WRITES` was), with the local
  store becoming a projection. Reconcile path covers offline-append as it does
  for breadcrumbs.
- **AC:** filing a memory produces a signed entity in the store readable
  without agent-notes' code; the pgvector index rebuilds from the entities.

## Phase 2 — The human read surface (with dossier)

### WI-2.1 — Browse + render in dossier
- dossier renders notes: per-project browse by subtype, note detail with links
  resolved (work items ↔ notes), full-text search folded into dossier's search.
  This is dossier Plan 009's implementation, coordinated so it lands against
  the WI-1.1 contract.
- **AC:** a breadcrumb filed by an agent during a real session is readable in
  dossier, linked from the work item it was filed against; a reflection is
  browsable under its project.

### WI-2.2 — Cross-face integration test
- The Plan 010 discipline applied here: a CI-runnable test drives
  file-note-via-agent-face → read-via-human-face against one store, so the
  knowledge contract cannot silently diverge.
- **AC:** the test runs in at least one repo's CI (or the suite interop CI) and
  fails if either face drifts from the WI-1.1 contract.

---

## Sequencing

WI-1.1 (contract) gates everything — do it as a three-repo conversation, not an
agent-notes unilateral. WI-1.2 and WI-2.1 can then proceed in parallel per
face; WI-2.2 closes the loop. Fold the effort estimate into the suite v2
human-visibility wave (dossier 017/018 + this).
