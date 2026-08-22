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
both together. Run `make check-suite-lock` to confirm the sibling checkout
matches the pin (informational; `make check-suite-lock-strict` fails on drift).

## 2. Secrets — choose a backend (Plan 017 WI-4.1)

agent-notes resolves the regista DSN and signing key-set through regista's
secret-backend resolver (blueprint §2.5), so neither needs to sit in plaintext
config. A value may be:

- **a literal** — `REGISTA_DSN=postgresql://user:pass@host/db` (today's default;
  zero regression, no backend contacted).
- **`env:VAR`** — read the secret from a process env var (useful for a secret
  injected by the launcher / a `.env` loaded by systemd).
- **`file:/path`** — read the secret from a file (DSN: the file *contains* the
  DSN string; key-set: the file *is* the key-set manifest regista reads directly).
- **`vault:mount/path/key`** — HashiCorp Vault KV v2 (requires `pip install regista[vault]`
  + `VAULT_ADDR`/`VAULT_TOKEN`).
- **`azure:name`** — Azure Key Vault (requires `pip install regista[azure]` +
  `AZURE_KEY_VAULT_NAME`).

The two suite secrets and the recommended pattern:

| Secret | Config var | Backend pattern |
|--------|-----------|-----------------|
| regista DSN (incl. password) | `REGISTA_DSN` | `env:REGISTA_DSN_VALUE` or `vault:secret/agent-suite/regista#dsn` |
| signing key-set manifest | `REGISTA_KEY_PATH` | a file path (default), or `vault:secret/agent-suite/regista#keys` |

If a launcher cannot provide environment variables, the optional
`~/.config/agent-notes/config.json` fallback uses the following `regista`
shape (the write gate is still tool-specific):

```json
{
  "regista": {
    "dsn": "postgresql://user:pass@host/db",
    "key_path": "/path/to/keys.json",
    "require_ssl": true,
    "writes_enabled": true
  }
}
```

The configuration surface is canonical and breaking; unknown names are ignored.

**Key-set manifest vs key material.** regista's key-set JSON is a *manifest*
(key ids, roles, statuses); each entry may carry its secret inline **or** point
at the backend via a per-key `secret_ref`. The custody best practice is to keep
only `secret_ref` pointers in the manifest and the raw key material in the
backend — then the manifest itself is not sensitive. When `REGISTA_KEY_PATH` is
a *remote* ref (`env:`/`vault:`/`azure:`), agent-notes resolves the manifest
bytes and materializes them to a 0600 temp file for regista to read; the file is
scrubbed at clean process exit. mtime-poll hot-reload does not apply on this
path — rotate by restarting the face. A bare path or `file:` ref is read directly
by regista (hot-reload works; `~` is expanded by agent-notes).

**Caveat — unclean shutdown.** `atexit` does not run on `SIGKILL`, OOM-kill, or
a hard crash, so a 0600 owner-only temp file (`an-keys-*.json`) may survive in
`$TMPDIR`/`%TEMP%` after an unclean shutdown. It is owner-only, but for a
regulated host a startup sweep or `tmpwatch` rule for `an-keys-*.json` is
recommended. Keeping only `secret_ref` pointers in the manifest (not inline
secrets) means a leaked temp file exposes only pointers, not raw key material.

**Windows.** `install-harness` and the resolver run unchanged on Windows. The
temp manifest honors `%TEMP%` and inherits its (typically user-scoped) ACL; the
explicit 0600 chmod is POSIX-only (a no-op on Windows, where the file is already
owner-scoped by mkstemp).

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
reverses it. The env block carries whatever you configured — a backend ref is
preserved verbatim (e.g. `REGISTA_KEY_PATH=vault:...`) and resolved at the
consumer's runtime, not expanded at install time.

## 5. Health check

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

The `secrets_backend` check resolves any configured backend ref once (it
*contacts the backend* — for Vault/AKV that is one API call per ref per doctor
run; a monitoring loop may want to throttle). It is `skip` when no ref is
configured (plaintext/file deployment) — not a failure.
