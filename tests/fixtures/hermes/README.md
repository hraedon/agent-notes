Payloads for the Hermes `pre_llm_call` shell hook, captured on 2026-10-29 by
registering a capture script as a shell hook and firing the real
`hermes_cli.plugins.invoke_hook` with the kwargs Hermes passes
(`agent/conversation_loop.py` in 0.15.2, `agent/turn_context.py` on `main`).
Only `cwd` and the session id were edited.

- `pre_llm_call_first_turn.json`, `pre_llm_call_multimodal.json`: hermes-agent
  0.15.2 (PyPI).
- `pre_llm_call_main.json`: `main` at ef75bc70 (2026-10-28), which adds
  `profile` and `extra.telemetry_schema_version`. `is_first_turn` was set to
  false by hand.
