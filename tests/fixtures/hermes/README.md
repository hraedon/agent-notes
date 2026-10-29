Payloads for the Hermes `pre_llm_call` shell hook.

`pre_llm_call_first_turn.json` and `pre_llm_call_multimodal.json` were captured
on 2026-10-29 from hermes-agent 0.15.2 (PyPI): a capture script registered as a
shell hook, fired through the real `hermes_cli.plugins.invoke_hook` with the
kwargs `agent/conversation_loop.py` passes. Only `cwd` and the session id were
edited.

`pre_llm_call_main.json` follows the `main` branch (docs and
`agent/shell_hooks.py`, not captured): it adds `profile` and the `task_id` /
`turn_id` kwargs.
