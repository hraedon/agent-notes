---
name: adversarial-review
description: Perform a cross-lineage adversarial review of work items awaiting review. Invoke when a work item is in "in_review" status, when the user says "review WI-X", "adversarial pass on X", or when the orient output shows items awaiting review.
---

# /adversarial-review — Cross-Lineage Review Gate

Work items reach `done` only after a cross-lineage adversarial review pass
(Plan 010/014, Invariant G). When an author closes their work, the item
moves to `in_review` — it sits there until a **different actor** (ideally
a different model lineage) reviews it and either passes it to the human
gate, requests changes, or (after a prior adversarial pass) accepts it.

This skill is how an adversarial-review subagent discovers, reviews, and
advances work items through the gate.

## Prerequisites

You need `agent-notes` on PATH and `AGENT_NOTES_DSN` set. Verify:

```
agent-notes doctor --json
```

If `coordinator-absent / local-lease` mode is active, review transitions
other than `accept` work but the gate does not actually run (the review
note is stored in `diagnostic_keys` for provenance). `accept` is blocked
in degrade mode — completion requires regista's cross-lineage gate.

## Identity

The gate enforces two identity checks:

1. **Separation of duties** — your `actor_id` must differ from every
   actor who worked the item (no self-review).
2. **Cross-lineage** — if you are an agent, your `model_lineage` must be
   declared and distinct from the authors' lineages. Set it via:

   ```
   --actor-id <your-distinct-id> --model-lineage <your-lineage>
   ```

   Common lineage values: `kimi`, `glm`, `nemotron`, `minimax`, `opus`,
   `human`. If you share a lineage with an author, the gate allows the
   review only with `--same-lineage-acknowledged` (the note must justify
   why same-lineage review is acceptable here).

## Step 1: Discover what's awaiting review

```
agent-notes work-item review list --path <repo-path> --json
```

Returns `review_queue` with items in `in_review` (needs adversarial pass)
or `in_human_review` (needs accept/reject). Pick one to review.

## Step 2: Read the work item

```
agent-notes work-item get <identifier> --path <repo-path> --with-body --json
```

Read the body, title, and diagnostic keys. Understand what was done and
why. If the item references git commits, review those too:

```
git log --oneline -10
git diff <base>..<head>
```

## Step 3: Perform the review

Evaluate honestly. Look for:

- **Correctness** — does the change do what the work item claims?
- **Completeness** — are edge cases covered? Are tests adequate?
- **Provenance** — does the op-chain reflect the actual work?
- **Security** — any secrets exposed, injection risks, or unsafe patterns?
- **Convention drift** — does it break existing patterns without justification?

## Step 4: Drive the gate transition

Based on your findings, choose one:

### Pass (in_review → in_human_review)

The work is sound; advance it to the final accept gate:

```
agent-notes work-item review pass <identifier> \
  --path <repo-path> \
  --note "<your findings: what you checked, what you found, why it's sound>" \
  --actor-id <your-id> \
  --model-lineage <your-lineage> \
  --json
```

The note must be substantive — the gate validator rejects empty notes.
Document what you reviewed and your assessment.

### Request changes (in_review → in_progress)

The work needs revision:

```
agent-notes work-item review request-changes <identifier> \
  --path <repo-path> \
  --note "<specific issues to address>" \
  --actor-id <your-id> \
  --model-lineage <your-lineage> \
  --json
```

Be specific about what needs to change. The author will see the note in
the work item's `diagnostic_keys.review_notes`.

### Accept (in_human_review → done)

After a prior adversarial pass, finalize the work (requires regista):

```
agent-notes work-item review accept <identifier> \
  --path <repo-path> \
  --note "<acceptance rationale>" \
  --actor-id <your-id> \
  --model-lineage <your-lineage> \
  --json
```

Your `actor_id` must differ from whoever ran the `adversarial_pass`
(two-stage independence). In relaxed mode (homelab default) the original
author may accept, but a different reviewer is still preferred.

### Reject (in_human_review → in_progress)

The work is fundamentally flawed and should not proceed:

```
agent-notes work-item review reject <identifier> \
  --path <repo-path> \
  --note "<rejection rationale>" \
  --actor-id <your-id> \
  --model-lineage <your-lineage> \
  --json
```

## Error handling

If the gate rejects your transition, the error explains why:

- **"review_note is required"** — you forgot `--note`.
- **"reviewer must differ from every actor"** — your `--actor-id`
  matches an author. Use a distinct ID.
- **"model lineage is not confirmed distinct"** — your lineage collides
  with an author. Either use a different lineage or pass
  `--same-lineage-acknowledged` with a justification in the note.
- **"Cannot accept in degrade mode"** — regista is not connected. The
  item stays in `in_human_review` until regista is available to run the
  final gate.

Parse the JSON response and confirm the new status to the user.

---

If `agent-notes` exits non-zero or its JSON shape doesn't match what
this skill expects, the CLI contract has drifted — file a breadcrumb
under project agent-notes.
