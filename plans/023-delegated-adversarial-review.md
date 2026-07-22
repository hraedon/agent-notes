# Plan 023 — Delegated adversarial review with attributable evidence

**Status:** in progress — foundation slice implemented under WI-043
**Primary owner:** agent-notes
**Integration owners:** agent-provenance (evidence), regista (gate contract),
agent-suite (qualification), harness adapters (execution only)

**External review:** Claude Opus, three passes on 2026-07-22 — `revise`,
`revise`, then `accept` with no blockers. The first pass corrected identity
assurance and introduced durable attempt binding; the second caught an
incorrect assumption about the agent-notes Regista face. The accepted revision
resolves that against the actual Regista event/CAS interfaces and makes their
conformance spike an explicit gate.

## 1. Problem

Agent-notes already has the durable review workflow:

```text
in_review --adversarial_pass--> in_human_review --accept--> done
```

It also ships prompt-configured OpenCode adversarial-review agents and exposes
every gate transition through `agent-notes work-item review`. Those agents ask
the model to stay read-only, but they are not yet permission-locked or
hash-verified. What is missing is a single supported way to delegate a review
to another model. Today the acting
agent must assemble an ad hoc prompt, point the reviewer at the right checkout
and revisions, wait through unbounded output, interpret prose, copy the result
into a review note, and declare the reviewer's identity manually.

That manual bridge has four concrete failure modes:

1. The reviewer may inspect the wrong checkout or lack the requested commit.
2. Large diff dumps consume time and context without improving the verdict.
3. The model, provider, harness session, and resulting gate actor can disagree.
4. A valid adversarial pass can be followed accidentally by an invalid
   self-acceptance attempt, even though regista correctly rejects it.

The product should make delegated review reproducible and attributable without
allowing an external harness to become a second work-item control plane.

## 2. Decisions

### D1. Agent-notes owns the workflow

Add this surface:

```text
agent-notes work-item review delegate <id> \
  --harness opencode \
  --model <provider/model> \
  --base <git-revision> \
  --head <git-revision> \
  [--path <registered-repository>]
```

`delegate` resolves the work item, validates repository state, launches one
reviewer, validates its result, stores the review artifact, and performs at
most the `adversarial_pass` transition. It never performs `accept`.

The implementation belongs under `core/work_item/`; the existing
`WorkItemModel` facade remains a thin dispatcher. CLI parsing and rendering
remain in `cli/work_items.py`.

### D2. Harness adapters execute; they do not decide workflow state

Define a narrow internal `ReviewRunner` protocol. Initial adapters:

- `opencode`: `opencode run --format json --model <provider/model>`
- `claude`: `claude -p --output-format json --model <model>`

An adapter receives a prepared prompt and an immutable Git target. Agent-notes
materializes that target in a disposable detached worktree (or equivalently
isolated checkout), never in the operator's working tree. The subprocess may
damage its disposable copy, but it cannot mutate the developer checkout or
make that mutation part of the reviewed result. The copy is discarded after
the result and integrity checks are recorded.

Each qualified adapter also has a concrete tool policy:

- Claude uses print mode with only repository-reading tools (`Read`, `Grep`,
  and `Glob`) and native JSON-schema output. It is not given Bash, network, or
  agent-notes mutation tools.
- OpenCode uses a hash-verified, installed read-only reviewer definition. Its
  command permissions are allowlisted to bounded Git reads such as `diff`,
  `show`, `log`, and `status`; agent-notes mutations, ref changes, network,
  and general shell execution are denied. The adapter refuses an unknown or
  locally modified reviewer definition.

An adapter returns structured process output plus process metadata. It cannot
call agent-notes, change review state, push, fetch, or accept a work item.
Agent-notes compares the disposable checkout's HEAD and target identity after
execution before trusting the result.

Commands are constructed as argument tuples from a closed harness enum. There
is no `--command`, shell-template, or arbitrary executable escape hatch.
Environment inheritance is explicit and secret-bearing values are never copied
into the review artifact.

The currently installed OpenCode reviewer agents remain useful for interactive
delegation, but this command becomes the qualified non-interactive path. An
adapter that cannot enforce its tool and result contracts remains
`experimental` and cannot perform a lifecycle transition.

### D3. The reviewed Git state is explicit and immutable

Delegation is valid only while the item is `in_review`. Before launch,
agent-notes resolves `--base` and `--head` with local Git and records their
full object IDs. Both must already exist locally; delegation does not fetch
implicitly. The plan records:

- repository identity and resolved root;
- full base and head object IDs;
- merge-base;
- dirty-state summary;
- worktree path;
- changed-path summary.

The reviewer prompt names those full IDs and instructs the reviewer to compare
that range. A dirty operator worktree is refused by default because it makes
the requested source ambiguous even though execution is isolated. A future
explicit mode may review a content-addressed patch; Plan 023 does not add one.

### D4. Reviewer output is a bounded, versioned contract

The reviewer must return one JSON object conforming to a packaged schema:

```json
{
  "schema_version": 1,
  "verdict": "accept",
  "summary": "...",
  "blocking_findings": [],
  "non_blocking_risks": [
    {
      "title": "...",
      "detail": "...",
      "paths": ["src/example.py"]
    }
  ],
  "reviewed_paths": ["src/example.py", "tests/test_example.py"]
}
```

Closed verdicts are `accept` and `request_changes`. Findings and summaries
have count and length limits. Paths must be repository-relative and must not
escape the repository. Unknown fields are rejected in v1. The complete
canonical artifact is capped at 64 KiB. Full diffs, transcripts,
chain-of-thought, tool logs, environment dumps, and credentials are not
accepted fields.

Adapters must use a structured result channel, not strip prose or Markdown
fences heuristically. Claude uses `--json-schema` with machine output.
OpenCode must submit the final object through a small schema-validating result
sink (or a future equivalent native schema facility); its event stream alone
is transport, not proof that the model result matches the contract. Exactly
one validated result is accepted.

Malformed output, a non-zero runner exit, timeout, missing identity metadata,
or a verdict inconsistent with blocking findings produces a named failed
delegation and no lifecycle transition.

### D5. Identity and assurance come from the adapter and evidence

The adapter records, where the harness exposes them:

- harness and harness version;
- provider/model requested;
- provider/model reported;
- harness session identifier;
- start/end timestamps and exit status.

The gate actor ID is derived from the harness session and normalized model
identity; the reviewer cannot choose it inside its JSON response. Model lineage
is normalized from the actual provider/model configuration using a versioned
mapping owned by agent-notes. An operator override is explicit, recorded, and
never silently inferred from prose.

Identity has three non-interchangeable assurance states:

- `attested`: an independent Cairn receipt binds the session to the reported
  harness/provider/model. Agent-notes may automatically attempt
  `adversarial_pass` if the canonical author/reviewer lineage gate also passes.
- `asserted`: the qualified adapter reports identity but no independent
  evidence provider is available. Agent-notes stores the result but performs
  no lifecycle transition unless the operator supplies
  `--acknowledge-unattested-reviewer`; that acknowledgment and reduced
  assurance are recorded in the transition payload. The acknowledgment text
  states explicitly that both reviewer identity and reviewer distinctness are
  adapter-asserted rather than independently proven.
- `degraded`: expected evidence is incomplete, mismatched, or unverifiable.
  The attempt is retained, but no lifecycle transition is allowed.

An asserted identity is never described as proof of distinctness. The explicit
acknowledgment permits a reduced-assurance review attempt; regista remains free
to reject it under the independent canonical lineage rules.

Delegation must not automatically set `same_lineage_acknowledged`. Missing
author lineage and unattested reviewer identity are separate conditions with
separate acknowledgments. Either can stop the transition, and acknowledging
one never implies the other.

### D6. Store a content-addressed review artifact and a bounded gate note

The canonical artifact contains:

- the validated reviewer result;
- reviewed Git identity from D3;
- adapter identity and timing from D5;
- SHA-256 of the exact prompt;
- optional evidence reference from D7;
- artifact creation version.

Store it through agent-notes' existing content-addressed blob path. The
`adversarial_pass` review note is a bounded human-readable summary plus the
artifact digest, not the complete model transcript. A successful regista
transition stores the complete bounded canonical artifact, its digest, and
assurance metadata in the authoritative transition event payload; the native
degrade path stores the same vocabulary in the op-log operation. Existing
review list/get/diagnose output renders the artifact identity and assurance
state.

Add a local `review_delegation_attempts` journal for both backends. It records
the deterministic idempotency key, work item, Git IDs, prompt digest, adapter
identity, assurance, artifact digest, intended transition/event ID, timestamps,
status, and a bounded error code. It deliberately retains failed and
pre-transition attempts that cannot appear in lifecycle history.

The write protocol is explicit:

1. Insert the unique `planned` attempt and commit it locally.
2. Mark it `running`, execute the subprocess, validate the result, store the
   blob, and mark it `result_validated`.
3. Snapshot the authoritative state/version and persist a backend-specific
   idempotency identity, then mark `transition_pending`:
   - Regista: generate one UUIDv4 `event_id`, record the current
     `next_event_seq`, and use it as `expected_event_seq`.
   - Native: construct the canonical transition-op payload whose
     content-addressed `op_id` excludes volatile timestamps and retry/session
     noise.
4. Submit the regista transition with the persisted `event_id`, CAS sequence,
   and bounded artifact payload, or commit the native transition op and journal
   update in one local database transaction.
5. For Regista, read events since the recorded prior sequence. Mark
   `transition_applied` only when the exact `event_id`, transition, artifact
   digest, actor, and target state match. If no event exists and the source
   state/sequence is unchanged, retry the same submission; if state advanced
   differently, stop with a reconciliation conflict. For native, reconcile the
   exact content-addressed op and folded state.

On retry, agent-notes performs that reconciliation before running another
review. Lifecycle legality already prevents a second pass transition from the
advanced state, but it is not used as a substitute for positive artifact/event
binding. Regista event append and native op-log apply remain the idempotency
authority; the local journal is the recovery index, not a second lifecycle.

### D7. Cairn evidence raises assurance but does not own the verdict

Agent-provenance adds a narrow query or receipt surface that can bind a harness
session ID to its captured session/tool attestations. Agent-notes consumes only
a stable evidence reference and summarized capture state; it does not copy
transcripts or tool payloads into the work-item store.

Evidence lookup produces one of:

- `attested`: the runner session is bound to verifiable Cairn evidence;
- `asserted`: the harness ran successfully but no evidence provider exists;
- `degraded`: evidence was expected but incomplete or unverifiable.

The initial policy permits storing an `asserted` result but requires the D5
operator acknowledgment before attempting a transition. A configurable strict
policy may require `attested`. Evidence failure never changes an `accept`
reviewer result into a lifecycle pass silently.

### D8. Regista remains the policy authority

Regista continues to enforce author/reviewer separation, lineage rules, review
notes, and the independent final accepter. No runner or provider logic moves
into regista.

Direct inspection during plan review confirmed that Regista core:

- accepts and persists arbitrary additive transition payload keys while its
  review validators inspect only their known gate fields;
- accepts a caller-supplied UUIDv4 `event_id` and `expected_event_seq`; and
- exposes per-work-item `read_events_since` for positive reconciliation.

The current agent-notes `RegistaFace.transition_breadcrumb` passes only the
CAS sequence. Its `history()` exposes a bounded full-history read, but not a
since-sequence or event-ID-focused reconciliation contract. Plan 023 therefore
adds `event_id` and the narrow event-read passthrough to the agent-notes face
and outbox contract, then extends `review_transition` to pass this artifact
vocabulary:

```text
review_artifact
review_artifact_digest
review_evidence_ref
review_identity_assurance
review_session_id
unattested_reviewer_acknowledged
```

Unknown keys remain additive metadata, not gate bypasses. A future regista
contract change is warranted only if another consumer needs standardized
fields or a validator must enforce the new acknowledgment itself. Before that
is assumed, WI-0.3 must prove direct, in-memory, and configured sidecar/outbox
conformance against the pinned Regista version. Any missing transport support
becomes an explicit Regista work item and blocks WI-1.3; the plan does not
silently downgrade to state-only reconciliation.

### D9. Final acceptance stays deliberately separate

Successful delegation ends at `in_human_review`. Output must say that the
adversarial pass is complete and name the remaining independent acceptance
step. The delegating actor, reviewer actor, and any identities in their
delegation chains cannot perform final acceptance where the canonical gate
forbids it.

Agent-notes does not offer `delegate --accept`, `--auto-accept`, or an adapter
hook that calls `work-item review accept`.

## 3. Work breakdown

### Phase 0 — Contract and fixtures

#### WI-0.1 — Review result schema

- Package the v1 JSON schema and typed Python representation.
- Add exhaustive verdict dispatch and bounded field validation.
- Add malicious/malformed fixtures: extra fields, absolute/traversal paths,
  oversized output, blocking findings with `accept`, invalid UTF-8, and
  transcript-like payloads.
- **AC:** every fixture has one deterministic validation result and malformed
  output cannot reach a lifecycle transition.

#### WI-0.2 — Runner and artifact contracts

- Define `ReviewRequest`, `ReviewProcessResult`, `ReviewArtifact`, and
  `ReviewRunner` under an internal delegated-review module.
- Define stable error codes for unavailable runner, unknown revision, dirty
  target, invalid work-item state, timeout, invalid output, identity mismatch,
  lineage uncertainty, evidence degradation, and transition rejection.
- **AC:** contracts are harness-neutral and contain no shell command strings,
  secrets, transcript body, or provider credentials.

#### WI-0.3 — Regista reconciliation conformance spike

- Jointly inspect and test the pinned Regista direct, in-memory, and configured
  sidecar transports for caller-supplied UUIDv4 event IDs,
  `expected_event_seq`, and `read_events_since`.
- Prototype the minimal agent-notes `RegistaFace`/outbox passthrough and prove
  the D6 lost-response recovery case by event ID plus artifact digest.
- If any deployed transport lacks those semantics, file and sequence a bounded
  Regista compatibility work item before WI-1.3; do not fall back to inference
  from current state alone.
- **AC:** a transition response may be discarded deliberately, after which a
  fresh agent-notes process positively identifies the one committed event and
  neither re-runs the reviewer nor appends another lifecycle event.

### Phase 1 — Agent-notes orchestration

#### WI-1.1 — Git target resolver

- Resolve registered project root, base/head full IDs, merge-base, clean state,
  and changed paths using argument-vector Git calls with timeouts; materialize
  the exact head in a disposable detached worktree.
- Refuse missing objects, ambiguous revisions, dirty worktrees, and a base that
  is not an ancestor unless a future explicit comparison mode is introduced.
- **AC:** tests cover linked worktrees, SHA/tag/branch inputs, missing commits,
  dirty state, unrelated histories, disposable-worktree cleanup, post-run HEAD
  integrity, and paths containing spaces.

#### WI-1.2 — Prompt renderer

- Render a bounded prompt containing work-item title/body, exact Git range,
  relevant repository instructions, output schema, read-only constraints, and
  a review checklist.
- Do not include environment values, credentials, unrelated note/memory bodies,
  or prior model transcripts.
- **AC:** golden prompt fixtures are stable, size-bounded, and identifier-gate
  clean.

#### WI-1.3 — Delegation transaction

- Add the internal orchestration function under `core/work_item/`.
- Require the canonical item state to be `in_review`; otherwise return the
  named `REVIEW_DELEGATION_STATE_INVALID` failure before launching a runner.
- Launch the runner, validate identity and JSON, store the artifact, and then
  request `adversarial_pass` only for a valid `accept` result.
- A `request_changes` result stores the artifact and performs the existing
  `request_changes` transition back to `in_progress`.
- Make retries idempotent by `(work_item_entity_id, head_sha, model_identity,
  prompt_digest)`. Add the attempt journal and pending-event reconciliation
  protocol from D6. Regista calls carry the persisted UUIDv4 event ID and CAS
  sequence; native calls keep volatile values out of the identity-bearing op
  payload. A retry may reuse a completed artifact but cannot duplicate a gate
  event.
- **AC:** failure at every boundary leaves an inspectable attempt record and
  never produces a partial or duplicate lifecycle transition.

#### WI-1.4 — CLI and human output

- Add `work-item review delegate` with `--harness`, `--model`, `--base`,
  `--head`, `--timeout`, `--json`,
  `--acknowledge-unattested-reviewer`, and the standard project-resolution
  flags.
- JSON output uses the component CLI envelope and never mixes logs on stdout.
- Text output is concise: target range, reviewer identity, verdict, artifact,
  assurance, transition performed, and remaining final-acceptance action.
- **AC:** help, success, request-changes, timeout, malformed response, lineage
  uncertainty, and dry failure paths have stable exit codes and no traceback.

### Phase 2 — Harness adapters

#### WI-2.1 — OpenCode adapter

- Invoke non-interactive OpenCode in the disposable exact-head worktree with
  JSON event output and the hash-verified read-only reviewer definition.
- Extract the reported session and model identity; reject disagreement with the
  requested provider/model unless an explicit alias mapping permits it.
- Require the schema-validating result sink from D4; do not treat arbitrary
  assistant event text as the result object.
- Restrict the launched reviewer to the D2 Git-read allowlist. Network, general
  shell, ref mutation, and agent-notes mutation are denied.
- **AC:** fake-runner tests cover event ordering, duplicate/missing result
  submissions, model mismatch, reviewer-definition hash mismatch, missing
  session ID, non-zero exit, timeout, and cancellation; one live opt-in test
  reviews a synthetic repository. Until this AC passes, OpenCode is displayed
  as experimental and cannot perform a lifecycle transition.

#### WI-2.2 — Claude print-mode adapter

- Invoke `claude -p` with `--json-schema`, machine output, and only `Read`,
  `Grep`, and `Glob` against the disposable exact-head worktree.
- Extract session/model identity from Claude's machine output rather than
  parsing display text.
- **AC:** the same conformance suite used by OpenCode passes; one live opt-in
  Opus test reviews the synthetic repository.

#### WI-2.3 — Adapter conformance kit

- Publish a shared adapter contract test parametrized over every runner.
- Test path safety, output caps, timeout/process cleanup, stdout/stderr
  separation, model identity, and deterministic artifact production.
- **AC:** a new harness cannot be advertised as supported without passing the
  complete kit.

### Phase 3 — Artifact attribution and evidence

#### WI-3.1 — Review attempt journal, artifact storage, and rendering

- Add the D6 attempt journal and recovery state machine.
- Store content-addressed artifacts, bind them to authoritative
  events/operations, and render them through get/diagnose/export without
  exposing transcripts.
- Include artifact digests in cross-project export/ingest and verifier checks.
- **AC:** tests crash at every write-protocol boundary and prove recovery
  without duplicate transitions; tampering, missing blobs, duplicate
  artifacts, and mismatched Git IDs are named verifier findings.

#### WI-3.2 — Cairn session evidence binding

- Add the minimal agent-provenance query/receipt required by D7.
- Bind the harness session to an evidence reference without coupling
  agent-notes to Cairn storage internals.
- **AC:** attested, asserted, and degraded cases are deterministic; a forged
  or mismatched session reference cannot claim `attested`.

### Companion track L — Author-lineage completeness

This is an existing platform hygiene concern, not part of runner execution or
artifact durability, so it should not expand Plan 023's critical path. It is a
prerequisite only for advertising automatic strong cross-lineage assurance.

#### L-1 — Author mutation lineage audit

- Audit all agent-notes work-item mutation paths and ensure agent actors carry
  resolved model lineage when it is available.
- Add `work-item diagnose` output for undeclared author lineage and document
  why historical events remain fail-closed.
- Do not rewrite signed history to fill missing lineage.
- **AC:** a newly created and amended agent-authored work item can pass a truly
  distinct attested reviewer without `same_lineage_acknowledged`; a legacy
  undeclared author requires that separate explicit acknowledgment.

### Phase 4 — Suite qualification

#### WI-4.1 — Cross-repo interop fixture

- Agent-suite creates a synthetic work item and repository, delegates one
  OpenCode accept review and one Claude request-changes review through fake
  adapters, injects a crash/retry around the authoritative transition, verifies
  artifact replay, and proves final acceptance remains a distinct step.
- **AC:** the fixture runs in ordinary CI without external model credentials.

#### WI-4.2 — Credentialed live qualification

- Add explicit opt-in jobs/runbooks for one OpenCode model and Claude Opus.
- Capture only review artifact, adapter metadata, Cairn evidence reference, and
  gate events as qualification evidence; never commit transcripts or secrets.
- **AC:** both live reviewers inspect the exact requested range, produce valid
  artifacts, and end at the expected lifecycle state.

#### WI-4.3 — Installer and documentation convergence

- Update the `adversarial-review` skill and installed OpenCode agents to prefer
  `work-item review delegate` for non-interactive reviews.
- Document the interactive subagent path as provisional/manual and the delegate
  command as the qualified path.
- Update agent-suite feature probes and operating guidance.
- **AC:** canonical skills, plugin copies, harness manifests, docs, and feature
  probes agree; installation remains idempotent and hash-owned.

## 4. Sequencing

1. Run WI-0.3 first. If the pinned Regista transports conform, land the small
   agent-notes face/outbox extension; otherwise land the resulting Regista
   compatibility item before continuing.
2. Land the rest of Phase 0, the attempt journal, and Phase 1 behind an internal
   fake runner. Prove lost-response and crash recovery before launching a real
   model.
3. Land the OpenCode adapter against the conformance kit and result sink; keep
   it experimental until the live read-only and exact-range tests pass.
4. Land the Claude adapter independently against the same conformance kit; its
   native JSON-schema support makes it the reference structured-output adapter.
5. Add Cairn binding without making it a hidden hard dependency. Run companion
   Track L in parallel; both are required before claiming strong automatic
   cross-lineage assurance.
6. Qualify in agent-suite and only then update installed skills to advertise
   the workflow as supported.

Agent-notes Phases 0–2 and WI-3.1 form one focused implementation stream.
WI-3.2 belongs in agent-provenance. Phase 4 belongs in agent-suite after both
component legs are released and pinned. Track L can be its own small
agent-notes change. WI-0.3 is jointly owned by agent-notes and the Regista
maintainer; whether it opens a Regista implementation stream is an explicit
output of that spike.

## 5. Security and operational invariants

- External reviewers are untrusted subprocesses and untrusted output sources.
- No implicit fetch, operator-worktree mutation, branch/ref mutation, commit,
  push, or PR operation occurs during delegation. The disposable checkout may
  be created and removed only by agent-notes.
- No arbitrary shell command or executable path is accepted from work-item
  content or reviewer output.
- Output and prompt sizes, process duration, and stored finding counts are
  bounded.
- Credentials and inherited environment values are neither prompted nor
  persisted.
- Reviewer JSON never determines its own gate identity.
- A stored reviewer result is not a gate pass until regista accepts the
  transition.
- Delegation never performs final acceptance.
- Asserted reviewer identity requires a recorded operator acknowledgment;
  degraded identity always stops the transition.
- Missing author lineage, missing reviewer identity, and missing required
  evidence reduce assurance or stop the transition; none are silently filled
  and no acknowledgment implies another.

## 6. Non-goals

- General multi-agent task scheduling or coding delegation.
- A provider/model marketplace.
- Automatic remediation of review findings.
- Automatic PR approval or merge.
- Transcript or chain-of-thought collection.
- Rewriting legacy signed author events.
- Replacing regista's review validators.
- Credential brokerage; agent-capability-broker may own that later.

## 7. Completion criteria

Plan 023 is complete when:

1. `agent-notes work-item review delegate` supports qualified Claude
   print-mode and OpenCode adapters through one conformance contract; OpenCode
   cannot leave experimental mode without its schema-validating result sink.
2. Every successful delegation names an exact Git range, immutable artifact
   digest, actual runner/model/session identity, and honest evidence assurance.
3. Invalid or ambiguous review attempts cannot change lifecycle state, and a
   crash/retry at every write boundary cannot duplicate a transition.
4. A valid accept review performs only `adversarial_pass` and leaves final
   acceptance to a different actor.
5. Asserted identity, missing author lineage, and same-lineage review use
   separate recorded acknowledgments; degraded identity cannot pass.
6. Agent-suite CI proves fake-adapter interop, while credentialed live runs
   separately qualify OpenCode and Opus without making ordinary CI depend on
   external model availability.

Strong automatic cross-lineage assurance is an additional release claim gated
on companion Track L and Cairn attestation; the useful reduced-assurance
workflow may ship earlier with the explicit acknowledgments above.

## 8. Opus review resolution

The external review's blocking findings were resolved as follows, and Opus's
third pass accepted the resulting plan with no blockers:

1. An unattested model/session is now `asserted`, never "proven distinct". It
   cannot transition without a dedicated operator acknowledgment, while
   mismatched/degraded evidence cannot transition at all.
2. Regista's implementation was inspected directly. It persists additive
   payload keys and its core supports caller UUIDv4 event IDs, CAS, and event
   reads, but the current agent-notes face does not expose all three. Plan 023
   now gates implementation on a transport-conformance spike and specifies
   positive event reconciliation rather than assuming face support or inferring
   success from state alone.

The reviews' important improvements are also incorporated: disposable
read-only execution, native/schema-validating result channels, the explicit
`in_review` precondition, honest asserted-distinctness acknowledgment, stable
native idempotency payloads, and separation of author-lineage completeness into
a companion track.

## 9. Implementation log

### 2026-07-22 — WI-043 foundation slice

- WI-0.3 passed for the transports agent-notes actually uses: direct Regista,
  `InMemoryRegista`, and the signed local outbox. Caller UUIDv4 event IDs, CAS,
  model lineage, and since-sequence reads survive replay. A commit-then-lost-
  response test positively reconciles the exact event binding instead of
  retrying into an invalid transition. Regista's HTTP sidecar exposes the same
  upstream primitives, but agent-notes has no HTTP-sidecar client; such an
  adapter remains a separate compatibility scope rather than a false current
  qualification claim.
- Added the strict, private v1 result/runner/artifact contracts, immutable Git
  range resolution, disposable detached-worktree execution, bounded prompt,
  exact structured-result validation, identity checks, and non-mutating
  recommendations. No public delegate CLI or lifecycle write exists yet.
- Added the durable attempt journal and artifact blob binding. The private
  orchestrator commits `planned` and `running` before the external runner,
  records bounded failures, and stops safely at `result_validated`.
- Raised the declared Python floor to 3.12 to match the published suite
  conformance dependency and the existing Python 3.13/3.14 CI matrix; refreshed
  `uv.lock` to include that kit and pinned Regista 0.5.3.
- Opus adversarial implementation review initially found one blocker: the
  canonical artifact included an absolute checkout path. The fix replaces it
  with a stable registered repository identity and proves the digest is
  invariant across local checkout paths. The follow-up verdict was `accept`
  with no blockers.
- Verification: `make lint` clean; `make test` reports 937 passed, 3 skipped,
  and 9 deselected.
