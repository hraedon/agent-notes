# Deployment — agent-notes as a suite component (Plan 017 WI-2.2)

This is the operator-facing install procedure for agent-notes as the **agent
face** of the agent-suite (blueprint §2.3 Phase B). It assumes the Postgres
database exists and is migrated (`agent-notes-migrate`).

## 1. Install

agent-notes has no regista dependency since Plan 026 D3:

```bash
uv tool install --reinstall /path/to/agent-notes   # hosts
uv pip install -e ".[test]"                         # development
```

## 2. The database DSN

Set a literal Postgres DSN in `AGENT_NOTES_DSN` (process env or suite.env) or
in the `dsn` key of `~/.config/agent-notes/config.json`. Secret-backend refs
(`env:`, `file:`, `vault:`, `azure:`) were resolved by regista and were dropped
with it (Plan 026 D3); a DSN carrying one fails with `RETIRED_SETTING`.

## 3. Pre-cache the embedding model (avoid first-run egress)

agent-notes loads a ~270 MB embedding model on first use (`nomic-ai/nomic-embed-text-v1.5`,
dim 768). On an air-gapped / egress-controlled work host, pre-stage it so the
first run does no network fetch:

```bash
# On a machine with egress, populate a cache dir you can copy in:
export HF_HOME=/opt/agent-notes/hf-cache
huggingface-cli download nomic-ai/nomic-embed-text-v1.5
# Copy /opt/agent-notes/hf-cache to the work host, then:
export HF_HOME=/opt/agent-notes/hf-cache   # set in suite.env
HF_HUB_OFFLINE=1 agent-notes doctor --check-embed   # confirms offline load
```

`HF_HOME` is the canonical cache root (read by `sentence-transformers` /
`huggingface_hub`). Override the model with `AGENT_NOTES_EMBED_MODEL` /
`AGENT_NOTES_EMBED_DIM` if you standardize on a different embedder.

## 4. Wire the harness

```bash
agent-notes install-harness claude      # or: opencode, all
```

This installs the skills + wires the env block + registers the opencode plugin
(Plan 017 WI-2.1). Re-runnable; `--dry-run` shows the diff; `uninstall-harness`
reverses it.

## 5. Health check

```bash
agent-notes doctor --json
```

Emits the suite-shape health object (Plan 017 WI-3.1). The suite-doctor
umbrella aggregates this across components. A green run:

- `status: "healthy"`
- `checks[].status` all `pass` or `skip` (no `fail`)
