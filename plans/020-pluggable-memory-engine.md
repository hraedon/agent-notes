# Plan 020 — Pluggable memory engine: preserve signed knowledge, delegate learned recall

**Status:** Proposed 2026-07-11.
**Author:** GPT-5.6 Sol, after review of Hindsight, Mem0, Letta, Cognee,
Zep/Graphiti, MemPalace, and the existing agent-notes implementation.
**Depends:** Plan 018 (signed knowledge entities), Plan 017 (suite config and
doctor), Plan 019 (Codex lifecycle wiring), agent-suite Plan 009 GJ-3.
**Supersedes:** Plan 018's assumption that agent-notes pgvector is the fixed
recall projection; it does not supersede signed notes or human legibility.
**Strategic role:** Keep agent-notes as the stable agent face and exact-knowledge
workflow while allowing a dedicated memory system to own extraction, indexing,
retrieval, temporal reasoning, and synthesis.
**Scope extension:** Plan 022 defines shared organization/project and personal
agent/session memory, authenticated access context, opaque provider bank IDs,
composed recall, and publication. Its scope and isolation requirements supersede
this plan's project-only Hindsight mapping.

## Outcome and default decision

The suite needs two different things that have both been called "memory":

1. **Exact knowledge** — authored decisions, project memories, feedback, and
   reflections whose exact body, author, links, history, and deletion semantics
   matter. The signed regista note is the authority for this material.
2. **Learned memory** — derived facts, observations, entity relationships,
   temporal retrieval, ranking, and synthesized answers. This is a rebuildable,
   probabilistic context service and is not authoritative truth.

Self-hosted **Hindsight is the provisional recommended learned-memory engine**.
It has the best fit among the reviewed systems: a service boundary, PostgreSQL
deployment, project-like banks, explicit asynchronous operations, exact source
document retrieval, cascading document deletion, hybrid semantic/BM25/graph/
temporal recall, and a separate reflection operation. Promotion is gated on the
Phase 0 bake-off and live conformance proof; a marketing benchmark is not proof
for this corpus.

The existing agent-notes implementation remains as `native` for four reasons:

- zero-extra-service and offline/degraded operation;
- the behavior-preserving migration source and rollback path;
- the conformance oracle for the stable CLI and skills;
- a complete minimal installation for operators who do not authorize an
  ingestion-time model or another stateful service.

Do not keep expanding native retrieval to chase dedicated memory products. The
default policy is:

| Deployment | Exact knowledge repository | Learned memory engine |
|------------|----------------------------|-----------------------|
| Minimal/offline | regista plus native projection | native baseline |
| Recommended learned-memory | regista | self-hosted Hindsight |
| Managed | regista | explicitly selected qualified provider |

"Recommended" does not mean silently installed or contacted. The operator must
select the learned-memory deployment and its model/privacy policy. A configured
external engine that fails never falls back silently to native results.

## Ground truth

- `cli/memory.py` embeds documents and queries before calling the model layer.
  That makes the CLI part of the current retrieval implementation.
- `core/memory_model.py` owns SQL CRUD, supersession, wikilinks, deletion, and
  pgvector queries. With a regista face attached, some writes go through
  `core/note_model.py`, but reads still use the local projection.
- `core/search.py`, the web views, orientation, export/import, links, and
  reconciliation assume agent-notes tables and identifiers.
- Plan 018 and `docs/note-entity-contract.md` correctly make signed notes the
  canonical exact record. Embeddings are already described as projections; this
  plan generalizes the projection into a replaceable learned-memory engine.
- Plan 006's native hybrid-search work remains useful as the native adapter. It
  is not the universal provider contract.
- Session transcripts are governed by agent-suite Plan 011's sealed-evidence
  policy. This plan does not turn ordinary memory ingestion into a bypass around
  that policy.

## Provider evaluation

### Hindsight — provisional default

Hindsight exposes retain/recall/reflect over HTTP clients, can run self-hosted,
uses isolated banks, and records asynchronous operation state. Its document API
can return the original text and delete a document together with extracted
memory units and temporal/semantic/entity links. Those properties fit a
projection sourced from signed regista notes.

References:

- https://github.com/vectorize-io/hindsight
- https://hindsight.vectorize.io/best-practices
- https://docs.hindsight.vectorize.io/api-reference/get-document/
- https://docs.hindsight.vectorize.io/api-reference/delete-document/
- https://docs.hindsight.vectorize.io/api-reference/get-operation-status/

Important constraints:

- derived facts, observations, and mental models are not signed truth;
- ingestion and reflection can incur model cost and send content to the selected
  model provider;
- the project is moving quickly, so the tested provider and adapter versions
  must be pinned;
- cloud-only controls must not be assumed to exist in self-hosted mode;
- use one bank per authority scope by default. Tags are retrieval filters, not a
  substitute for a tenant boundary.

### Alternatives

- **Mem0** is the best simple reference adapter: good SDK/REST ergonomics,
  user/agent/run scopes, metadata filters, CRUD, and hosted/self-hosted forms.
  Its incremental advantage over native pgvector is smaller than Hindsight's
  learned observations and reflect path, and hosted/OSS capability parity must
  be tested explicitly. https://github.com/mem0ai/mem0
- **Graphiti** is the best second forcing adapter for source episodes,
  bi-temporal facts, graph provenance, and hybrid retrieval. It has a larger
  operational footprint (graph database plus model/embedding services) and a
  lower-level API. **Zep** is the managed context service built over that model
  and stays an explicit managed choice. https://github.com/getzep/graphiti
- **Cognee** is a broad document/data knowledge pipeline with graph, vector,
  relational, session, improve, and feedback flows. That breadth is useful for
  knowledge-base products but too much semantic and operational surface for the
  suite default. https://github.com/topoteretes/cognee
- **Letta** is an `AgentRuntime`, not a `MemoryEngine`: its blocks are
  agent-managed and always present in the executing agent's context. It may be a
  future alternate runtime or context source; adapting it to `recall()` would
  discard the reason to use it. https://github.com/letta-ai/letta

## Decisions and invariants

1. **One authority per content class.** Exact authored knowledge has exactly one
   repository. Learned engines receive source documents or events and produce
   derived context; they never become an accidental second authority.
2. **Separate ports.** `KnowledgeRepository` owns exact records.
   `MemoryEngine` owns learned ingestion and recall. A future `AgentRuntime`
   port, if justified, is separate again.
3. **Provider-owned retrieval.** An external engine owns its embeddings,
   extraction, deduplication, graph construction, temporal logic, ranking, and
   synthesis. Agent-notes does not pre-embed its requests.
4. **Stable face.** Existing `agent-notes memory`, `search`, `orient`, and skill
   workflows remain the user-facing compatibility surface.
5. **Service boundary.** Core code speaks a versioned HTTP/subprocess protocol.
   Provider SDKs and their dependency trees do not enter the core package.
6. **Capability negotiation.** Unsupported update, exact-source, synthesis,
   graph, export, or deletion behavior is named. No adapter returns a successful
   no-op for an unsupported operation.
7. **Honest consistency.** Ingestion returns an operation reference. Pending,
   indexed, failed, cancelled, and stale are distinct states.
8. **Source-aware results.** Results identify provider, source reference,
   scope, source time, and origin class: `raw`, `extracted`, `derived`, or
   `synthesized`. Provider-native fields survive under a namespaced extension.
9. **Untrusted context.** Recalled and synthesized text is token-budgeted,
   delimited, source-labelled, and treated as untrusted input to the agent.
10. **No transcript shortcut.** Only signed notes are ingested initially.
    Conversation/session capture requires the content and authorization contract
    in agent-suite Plan 011.

## Phase 0 — Contract and evidence before selection

### WI-0.1 — Memory engine protocol and conformance fixture

Define typed provider-neutral requests/results for:

- `describe` and `health`;
- `ingest(batch, idempotency_key)` and `operation_status`;
- `recall(query, scope, budget)`;
- `forget(selector)` with a deletion receipt;
- optional `synthesize`, exact-source `get`, `update`, and `export`.

The protocol must represent one input producing many extracted or derived
results without pretending that every provider exposes CRUD records.

**AC:**

- A fake asynchronous one-to-many provider passes the fixture.
- Capabilities, origin class, provider-native extensions, and all consistency
  states round-trip through JSON.
- Project, workspace, user, agent, and session scopes have explicit mappings;
  isolation negatives fail closed.
- Idempotent re-ingestion, outage, timeout, malformed response, prompt-injection
  delimiting, deletion receipt, and unsupported-capability cases are covered.
- The native engine passes the same mandatory contract.

### WI-0.2 — Reproducible leader bake-off

Build a committed, sanitized corpus from exact suite knowledge patterns:

- a decision and later reversal;
- changing temporal facts;
- user feedback and correction;
- repeated similar incidents;
- cross-project near-duplicates;
- linked work, files, sessions, and notes;
- insufficient-evidence/abstention questions;
- deletion and derived-artifact cascade;
- adversarial cross-scope and memory-injection cases.

Run native, Hindsight, Mem0, and at least one graph-oriented engine through the
same ingest/recall harness. Letta is reported separately as a runtime.

**AC:**

- The corpus, expected evidence, scoring rules, provider versions, model
  configuration, and commands are committed and reproducible.
- The report separates retrieval recall from synthesized-answer quality and
  records p50/p95 latency, model calls/tokens, storage, operational services,
  license, exact-source lifecycle, and deletion behavior.
- Promotion thresholds are declared before the final run. Zero scope leaks,
  honest indexing state, exact-source/hash agreement, and deletion correctness
  are hard gates regardless of quality score.
- Hindsight is promoted only if it beats native by the predeclared material
  quality margin without violating cost/latency budgets. Otherwise native stays
  default and Hindsight remains experimental.

## Phase 1 — Extract the native implementation without behavior change

### WI-1.1 — Repository and engine ports

Introduce `KnowledgeRepository`, `MemoryEngine`, capability/result types, and a
provider factory. Wrap current signed-note/local-projection behavior in
`RegistaKnowledgeRepository` and current search behavior in
`NativeMemoryEngine`.

**AC:**

- Existing CLI JSON/text golden outputs, skills, web reads, export/import,
  wikilinks, reconciliation, and tests do not regress.
- No memory CLI handler imports `core.embed`; the selected engine owns document
  and query representation.
- Reads and writes no longer dispatch by checking a concrete regista face.
- Native provider conformance passes and `native` remains the default through
  this phase.

### WI-1.2 — Federated search and links

Turn cross-kind search into an aggregator over the work provider, exact
knowledge repository, and learned-memory engine. Preserve exact entity links
separately from provider-derived relationships.

**AC:**

- A result identifies whether it is an exact work/note entity or learned
  context and links back to the exact source when one exists.
- Ranking across unlike score systems is explicit and tested; provider scores
  are not assumed comparable without normalization.
- Engine outage does not make exact work or knowledge unreadable.

## Phase 2 — Hindsight adapter

### WI-2.1 — Provider-neutral transport and Hindsight mapping

Implement an adapter to the versioned Hindsight HTTP surface. Map each project
to an isolated bank; use the regista note entity ID as stable `document_id`, the
signed body as source content, and signed actor/session/time/link metadata as
context. Store no secret in provider configuration or logs.

**AC:**

- Core imports no Hindsight package.
- Retain returns an operation reference and does not claim recallability until
  indexing completes.
- Re-ingesting the same note version is idempotent; a new signed version has a
  deterministic replacement/supersession policy.
- Recall preserves source/document references and provider-native evidence.
- Forget deletes the source document and verifies the provider-reported cascade;
  exact regista deletion/supersession policy remains independently enforced.

### WI-2.2 — Live adapter qualification

Run the adapter against the pinned self-hosted container and a disposable
database.

**AC:**

- The test covers provision, retain, pending/indexed/failed state, recall,
  optional reflect, exact source retrieval, delete cascade, restart, rebuild,
  timeout, provider outage, and version mismatch.
- Concurrent projects cannot retrieve one another's material.
- A self-hosted deployment is tested rather than inferring parity from cloud
  documentation.
- Provider/model calls and their content exposure are visible in doctor and the
  operator documentation.

## Phase 3 — Lifecycle and operator surface

### WI-3.1 — Memory-provider CLI and doctor

Add `agent-notes memory-provider describe|configure|doctor|export` and, if the
adapter can own it safely, `provision`. Configuration selects repository and
engine separately.

**AC:**

- Commands have stable JSON, dry-run where mutation is possible, idempotency,
  and explicit exit semantics.
- Doctor reports provider/version/protocol, reachability, capabilities, scope
  mapping, authority, indexing freshness/backlog, model configuration posture,
  and degraded operations.
- A configured external outage is not hidden by native fallback.

### WI-3.2 — Recall and capture lifecycle

Agent-notes remains the sole composer of its lifecycle hooks. Session start may
request bounded recall; compaction/end may flush only content authorized by the
capture policy.

**AC:**

- Orientation has a deterministic token budget and identifies learned context.
- Hook installation merges with cairn and user hooks and uninstalls only owned
  entries.
- Unregistered projects, provider outage, pending indexing, and no-result cases
  degrade honestly without blocking unrelated agent work.
- No raw transcript reaches the engine under this plan.

## Phase 4 — Promotion, migration, and rollback

### WI-4.1 — Shadow evaluation and default promotion

Mirror signed-note ingestion to Hindsight while native remains the served
engine, compare recall offline, and promote only after Phase 0 and Phase 2 gates
pass.

**AC:**

- Shadow results never enter prompts or mutate exact knowledge.
- Promotion is one configuration change with a recorded provider/adapter pin.
- The support matrix calls Hindsight `experimental` until live qualification
  passes and `recommended` only afterward.

### WI-4.2 — Rebuild, restore, and rollback

Treat learned memory as rebuildable from exact signed notes. Document what is
canonical, derived, provider-only, and externally backed up.

**AC:**

- A clean provider can be rebuilt from repository export with stable document
  IDs and no cross-scope leakage.
- Exact knowledge remains readable throughout provider loss and rebuild.
- Rollback to native requires no reverse transformation of learned facts.
- Restore verification reports incomplete indexing separately from lost
  canonical knowledge.

## Sequencing

WI-0.1 and WI-0.2 precede external adapter implementation. Phase 1 then lands as
a behavior-preserving refactor. Phase 2 proves Hindsight against self-hosted
reality. Phase 3 integrates lifecycle and health. Only Phase 4 may change a
recommended default.

Coordinate the exact-note side with Plan 018 rather than reopening its entity
split. Plan 018's dossier/cross-face proof can proceed while this plan changes
only the learned projection behind agent-notes.

## Non-goals

- Making generated facts, observations, or summaries canonical signed truth.
- Building another competitive memory algorithm inside agent-notes.
- Automatic raw transcript capture or general transcript search.
- Treating Letta as a search adapter instead of an alternate agent runtime.
- Bidirectional synchronization between two authorities.
- Requiring a managed cloud or model provider for the minimal suite.
