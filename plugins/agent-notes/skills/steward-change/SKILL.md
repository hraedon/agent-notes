---
name: steward-change
description: Use when a change can affect authentication, credentials, production, persistent data, deployment, policy enforcement, or multiple repositories; also use when asked to bypass isolation, evidence, or review.
---

# Steward a Change

Stewardship makes high-risk work explicit before mutation and keeps acceptance separate from implementation. This skill supplies judgment and sequencing; it is not an enforcement boundary and creates no new authority. Regista remains authoritative for workflow state, the harness or OS must provide real isolation, and the suite provenance layer records what it is configured to observe.

## When to use

- Before changing authentication, authorization, credentials, production infrastructure, deployment policy, persistent data, schemas, or agent controls.
- Before a multi-repository, irreversible, or externally visible change with material blast radius.
- When resuming another actor's work or preparing work for review.
- During incidents, deadlines, control-plane outages, or requests to waive isolation, evidence, or review.

## When not to use

- Read-only investigation or explanation with no mutation.
- A purely editorial change with no behavioral, operational, policy, or generated-output effect.
- A test-only change with no production-code, schema, policy, or fixture-contract effect.
- As a substitute for a repository's stricter workflow or an executable gate.

## Steps

1. **Confirm that this is high-risk work before writing.** Security boundaries, credentials, production, persistent data, deployment, policy enforcement, multi-repository scope, and irreversible external effects qualify. Record why. If none apply, leave this skill and use the repository's ordinary workflow.

2. **Declare intent in the authoritative work item.** Before mutation, file or update the work item through the repository's agent-facing CLI. Record the actor and model lineage, exact working-set paths, expected behavior, external acceptance commands or probes, required evidence, and known risks in the item body. A chat message, commit, or local note is not authoritative work state.

3. **Claim and isolate the work.** Claim the item using the existing claim command, then use an isolated worktree or a stronger repository-approved sandbox. A claim coordinates ownership; it does not grant filesystem authority. Verify that the isolation boundary exists before writing. Do not edit a shared checkout for high-risk work.

4. **Stay inside the declared working set.** Treat the listed paths as the maximum intended scope. Stop before touching an undeclared path and update the work item first. Use the repository's configured capability broker when privileged access is required. Never place secret values in prompts, transcripts, config patches, or evidence. This instruction does not replace an OS or harness boundary.

5. **Run external acceptance checks.** Execute the predeclared tests, linters, security checks, or reproducible probes after the change. Record each command or probe, exit status, and relevant result in the work item or review note. When Cairn or another suite provenance adapter is attached, link its evidence; when it is unavailable, say so rather than inventing a reference. Model confidence and manual inspection may supplement a check but cannot replace it.

6. **Obtain independent review.** Use a different actor and a different model lineage as required by the repository gate. If the gate permits a same-lineage acknowledgment, use it only with the required explicit justification. The reviewer is read-only, checks the declared intent, diff, acceptance evidence, risk classification, and scope, and records a substantive verdict. High-risk security work also receives a security-focused review.

7. **Use the existing completion gate.** Compare the final diff with the declared working set, confirm every declared acceptance check has a fresh result, and disclose provenance gaps and blockers. Drive the repository's existing adversarial-review and human-accept transitions; do not invent a stronger validator or claim that this skill enforced the result.

8. **Handle incidents and outages without inventing authority.** Follow an existing repository or operator emergency workflow if one is already approved. This skill does not create one. If no applicable path exists, stage nothing beyond an isolated worktree and do not perform privileged or shared-checkout writes; escalate and offer non-code containment from an existing incident playbook. Backfill can document an exception, but cannot retroactively authorize one.

## Rationalizations

| Excuse | Reality |
|---|---|
| "Active exploitation wins over ideal process." | Active harm justifies an existing emergency path, not an improvised unbounded write path. |
| "The incident commander ordered it." | Human authority must select an existing emergency workflow; a verbal order does not create declared scope, isolation, or evidence. |
| "Waiting is process-worship." | Isolation and the existing work-item and review path are operational safety controls. If they are too slow during an incident, the emergency path was not designed adequately. |
| "The fix is only six lines and easy to revert." | Small diffs can cross critical boundaries. Reversibility does not establish authorization or detect data already exposed. |
| "A commit is recoverable provenance." | Git records content history, not delegated authority, capability scope, independent evidence, or reviewer separation. |
| "The checkout is clean." | Cleanliness prevents accidental co-commit; it does not isolate concurrent work or enforce a working set. |
| "Provenance is degraded, not destroyed." | Name the gap honestly and follow the repository's existing degraded-mode policy; do not fabricate evidence or silently lower the gate. |
| "We can backfill everything afterward." | Backfill records what happened. It cannot make an unauthorized action authorized at execution time. |
| "This temporarily reorders the controls." | Controls whose value depends on happening before execution cannot be reordered after execution. |

## Red Flags - Stop

- "This emergency is different."
- "Being pragmatic, not dogmatic."
- "We will formalize it later."
- "Git is enough provenance for now."
- "The manager accepts the risk."
- "It is too small to need isolation."
- "The service is down, so the gate cannot apply."
- Any privileged write without a pre-action reservation and enforceable boundary.

## Final Report

Report:

```text
Risk: high - <reason>
Work item and claim: <identifiers>
Isolation: <worktree or sandbox>
Working set: <paths>
Acceptance checks: <commands/probes and fresh results>
Provenance: <references, unavailable, or degraded>
Independent review: <actor, lineage, verdict>
Gate: <review transition and resulting state>
Emergency path: <none or approved path and reconciliation status>
Residual risks: <none or explicit list>
```

Do not claim completion if any required field is missing. State the blocker and leave the work item in its current non-terminal state.
