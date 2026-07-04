# Deployment — agent-notes as a suite component (Plan 017 WI-2.2)

This is the operator-facing install procedure for agent-notes as the **agent
face** of the agent-suite (blueprint §2.3 Phase B). It assumes the spine
(`regista provision`) is already done.

## 1. Pin the spine

`SUITE.lock` records the regista git SHA this release was tested against. From
a fresh clone:

```bash
git clone hraedon/regista ../regista
git -C ../regista checkout "$(awk -F'\"' '/^sha =/{print $2}' SUITE.lock)"
pip install -e ../regista
pip install -e ".[test]"
```

The `[tool.uv.sources]` editable mapping in `pyproject.toml` resolves regista
from `../regista`; `SUITE.lock` is the SHA that pair is known-good at. Bump
both together.

## 2. Pre-cache the embedding model (avoid first-run egress)

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

## 3. Wire the harness

```bash
agent-notes install-harness claude      # or: opencode, all
```

This installs the skills + wires the env block + registers the opencode plugin
(Plan 017 WI-2.1). Re-runnable; `--dry-run` shows the diff; `uninstall-harness`
reverses it.

## 4. Health check

```bash
agent-notes doctor --json
```

Emits the suite-shape health object (Plan 017 WI-3.1). The suite-doctor
umbrella aggregates this across components. A green run:

- `status: "healthy"`
- `regista.reachable` true (or `null` if running in degrade / coordinator-absent
  mode — that is a named, non-failing state, not an error)
- `regista.chain_ok` true
- `checks[].status` all `pass` or `skip` (no `fail`)
