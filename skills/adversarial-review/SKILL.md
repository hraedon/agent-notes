---
name: adversarial-review
description: Perform a cross-lineage adversarial review of work items awaiting review. Invoke when a work item is in "in_review" status, when the user says "review WI-X", "adversarial pass on X", or when the orient output shows items awaiting review.
---

# /adversarial-review — Cross-Lineage Review Gate

Review is opt-in: an item enters review when someone moves it to
`in_review`. From there it can't be passed, accepted or closed by anyone who
shares a **model lineage** (vendor family: anthropic, openai, google, ...)
with the people who worked on it (Plan 026 D2). A reviewer from another
family passes it to the human gate, requests changes, or (after a pass)
accepts it.

This skill is how an adversarial-review subagent discovers, reviews, and
advances work items through the gate.

## Prerequisites

You need `agent-notes` on PATH and `AGENT_NOTES_DSN` set. Verify:

```
agent-notes doctor --json
```

## Identity

Every work-item write records who made it: the actor
(`AGENT_NOTES_ACTOR_ID`) and the model (`producer` in the op). The model
comes from `AGENT_NOTES_MODEL` if set, otherwise from your session, which the
harness hooks register. Codex, agy and Hermes report it on every hook event;
Claude Code reports it at session start.

Positive verdicts (`pass`, `accept`), and closing an item that's in review,
are refused when:

1. your model lineage is unknown (set `AGENT_NOTES_MODEL`, for example
   `claude-opus-5-5`, if the refusal says so);
2. you share a lineage with anyone who worked on the item since it last
   bounced back from review; or
3. the implementer's model is unknown (work filed before this gate existed)
   and your `actor_id` is theirs.

`human:*` reviewers skip the lineage check but not the actor check.
`request-changes` and `reject` are findings, not approvals, and anyone may
record them.

This is cooperative enforcement: identity is self-reported. It stops
accidental self-approval, and every override is recorded on the op.

**Delegated reviews.** If you are the implementer and requested a review
from another model (`agent-notes review request`), apply its verdict with
`--attempt <attempt-id>`. The gate then treats the delegated reviewer as the
reviewer. Each attempt can be applied once, and only an `accept` verdict can
pass or accept.

**Overrides.** `--same-lineage-acknowledged "<reason>"` passes the gate
anyway and records the reason. Use it only when a same-family review is
genuinely acceptable (a trivial change, or no other model available), and
say why.

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
agent-notes work-item review pass <identifier> --path <repo-path> --note "<your findings: what you checked, what you found, why it's sound>" --json
```

The note must be substantive — the gate validator rejects empty notes.
Document what you reviewed and your assessment.

### Request changes (in_review → in_progress)

The work needs revision:

```
agent-notes work-item review request-changes <identifier> --path <repo-path> --note "<specific issues to address>" --json
```

Be specific about what needs to change. The author will see the note in
the work item's `diagnostic_keys.review_notes`.

### Accept (in_human_review → done)

After a prior adversarial pass, finalize the work:

```
agent-notes work-item review accept <identifier> --path <repo-path> --note "<acceptance rationale>" --json
```

The same independence check applies to `accept`. The adversarial pass
itself doesn't count as implementation work, so the passer may also accept.
A different accepter is still preferred.

### Reject (in_human_review → in_progress)

The work is fundamentally flawed and should not proceed:

```
agent-notes work-item review reject <identifier> --path <repo-path> --note "<rejection rationale>" --json
```

## Error handling

If the gate refuses your transition, the error says why:

- **"review_note is required"**: you forgot `--note`.
- **"the reviewer's model lineage is unknown"**: set `AGENT_NOTES_MODEL` in
  the reviewing process, or use `--attempt`.
- **"... shares the reviewer's model lineage"**: you're the same model family
  as an implementer. Hand the review to another family (or request a
  delegated review), or pass `--same-lineage-acknowledged "<reason>"`.
- **"... worked on this item (model unknown)"**: legacy work by your own
  actor. Use a different reviewer.

Parse the JSON response and confirm the new status to the user.

---

If `agent-notes` exits non-zero or its JSON shape doesn't match what
this skill expects, the CLI contract has drifted — file a breadcrumb
under project agent-notes.
