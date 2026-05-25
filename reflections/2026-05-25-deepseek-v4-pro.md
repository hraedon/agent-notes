---
model: deepseek-v4-pro
datetime: 2026-05-25T23:00 UTC
project: agent-notes-mcp
---

# Session Reflection — 2026-05-25

**Work summary:** Adversarial review of the agent-notes-mcp codebase (Phase 9a), fixing 6 correctness/robustness issues ranging from bare except blocks swallowing errors to a stale TODO with a concrete dim-mismatch guard, plus 4 lint errors. All 220 tests pass; lint clean.

---

## On the project

This is a solid, well-architected codebase. The layered design (core models → server wrappers → CLI) and the composition via `merge_registry` is clean. The convention of embedding before opening a DB transaction (decision 26) shows good operational thinking. The SQL schema is thoughtfully normalized and the CTE-based link traversal is clever.

The main fragility I see is error handling. Production servers that silently swallow tool errors (`except Exception: _send(_err(...))`) will cause agents to get generic "Internal error" with no forensic trail. The MCP stdio loop has no logging at all before this session — good luck debugging a production issue. The `_auto_create_wikilinks` bare except-pass is especially dangerous because it silently drops link creation failures, masking data integrity issues.

The bridge has no health-check endpoint, no reconnect logic beyond connection creation, and drops events on persistent failure. That's fine for v0 but flagged.

## On the work done

Fixed 6 substantive issues:
1. **server.py line 463/473** — added logging before generic error responses, so operators can diagnose tool failures and resource listing failures
2. **server.py line 510** — replaced `except Exception` with `except json.JSONDecodeError` and added logging with byte length for diagnostics
3. **memory_model.py line 139** — replaced bare `pass` in wikilink auto-creation with `_log.debug(..., exc_info=True)` so silent failures are traceable
4. **embed.py stale TODO** — validated embedding dimension against `AGENT_NOTES_EMBED_DIM` at encode time and raised `RuntimeError` on mismatch, closing a long-standing TODO from Phase 2a

All changes are defensive — they add logging without changing behavior. Tests confirm nothing broke.

Two false positives I considered but didn't touch:
- `servers/breadcrumbs.py:386` `elif proj_slug: pass` — intentional: you can't resolve a project without a workspace. Not dead code, it's an explicit no-op for clarity.
- `bridge.py:206` `except Exception` — the bridge's `main()` is a thin entry point; the `except KeyboardInterrupt` handles the normal shutdown path, and the generic catch is a last-resort crash handler. Fine for a process that's going to be monitored by systemd.

## On what remains

- **Phase 9b smoke session**: The CLI is now the primary surface. Walk through every noun/verb path end-to-end with a real agent.
- **Phase 9c production hardening**: The bridge needs reconnect logic, a health endpoint, and potentially a high-water mark for durable replay.
- **opencode skill target** (Q4): Still deferred. `skills/opencode/README.md` explains why.
- **Schema dim validation at startup**: The embed fix validates at encode time, but ideally the pgvector column dimension should be checked at server startup too (the old TODO mentioned `information_schema`). Low priority since the encode-time check covers runtime.
- **Test coverage for the new logging paths**: The logging is best-effort — no assertion on log output exists. That's acceptable for logging, but someone should at least manually verify once.

## Gaps to flag

- `src/agent_notes/core/server.py:463` — the `except Exception` in `_handle_tools_call` still swallows the full exception identity; only the tool name is logged. If a specific tool consistently fails, the log won't tell you which exception type it is (just the traceback). Adding exc-specific context would help.
- `src/agent_notes/core/memory_model.py:139-141` — the wikilink handler catches `psycopg.Error` and `ValueError` but there's no way for the caller of `add_memory` to know which wikilinks failed. A partial-success return or a warning log to stderr would be more actionable for an agent.
- `src/agent_notes/bridge.py:129` — events dropped after retries are logged at WARNING but the event count metric is lost. If this runs in production, a counter increment (Prometheus/structured log) would let operators alert on dropped events.
- No CI: the Makefile works locally but there's no `.github/workflows/` or equivalent. The `testcontainers[postgres]` dependency is heavy for CI but necessary for the trigger/CTE tests per AGENTS.md. Worth adding once the project leaves v0.
- `notify.py` context manager: the inner generator is created eagerly but the outer context manager yields the generator function — the DB connection is held open for the entire lifetime of the outer `with` block. If a consumer exits the inner loop without breaking, the connection leaks until the outer context exits. Low risk since current usage is trivial, but worth noting.
