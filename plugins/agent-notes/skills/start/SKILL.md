---
name: start
description: Orient at the start of a working session so the agent picks up with project context instead of re-deriving it from scratch. Invoke at the start of a session, when the user says "/start", "where were we", or after a long gap.
---

# Session orientation

Start with one bounded, embedding-free read:

```text
agent-notes orient --path "<repo-path>" --reconcile --json
```

The default shows up to five entries per section, recorded update ages, and
latest-reflection metadata. `has_more` flags mean these are samples, not total
backlog counts. Use `--full` for up to fifteen entries and individual recent
changes, or `--limit <1-100>` when a task needs a larger sample.

Registered paths may differ from the local checkout, especially on Windows.
If resolution returns `PROJECT_NOT_REGISTERED`, use explicit selectors:

```text
agent-notes orient --workspace default --project <slug> --reconcile --json
```

Use `agent-notes workspace list --json` only if the workspace is unknown.
A git reconciliation match is a suggestion; inspect the commit before applying
it. The scan covers the displayed work-item sample. Orientation does not close
items or change shared state.

Keep the briefing short: the few work items relevant to the task, the latest
handoff's age, and any synchronization or git-resolution findings. Preserve
`has_more` rather than presenting a clipped list as the whole backlog.

`generated_at` dates this read. `updated_at`, `age_days`, and `freshness` describe
recorded updates: `aged` defaults to at least 30 days (`--stale-after-days`
changes the threshold); `unknown` and `future` require caution. Recent edits
do not verify a record's claims. Check the checkout or other current evidence
before relying on old decisions, statuses, or validation results.

Fetch `latest_reflection.name` with `agent-notes memory get <name>` and the same
project selectors when its handoff is relevant. Read specific memory bodies
only when needed; do not dump all project history by default. Report store or
configuration errors directly, without treating an unavailable store as empty.
