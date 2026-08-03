from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_notes.core.work_item._delegated_review import (
    DelegatedReviewError,
    IdentityAssurance,
    ReviewErrorCode,
    ReviewRequest,
    evaluate_review,
)
from agent_notes.core.work_item._review_runners import (
    ClaudePrintRunner,
    RunnerOutputLimitError,
    RunnerProtocolError,
    review_result_json_schema,
    run_bounded_process,
)

# The bounded-process runner is POSIX-only by construction (selectors over
# pipes, killpg for process-group timeouts) and now refuses on Windows rather
# than degrading. These tests drive that runner, so they are POSIX-only too.
# The Windows gap itself is covered by test_runner_refuses_on_windows.
pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="delegated review runners are POSIX-only; see RunnerUnsupportedPlatformError",
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def review_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "review@example.invalid")
    _git(repo, "config", "user.name", "Review Test")
    (repo / "reviewed.txt").write_text("base\n")
    _git(repo, "add", "reviewed.txt")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "reviewed.txt").write_text("head\n")
    _git(repo, "commit", "-qam", "head")
    return repo, base, _git(repo, "rev-parse", "HEAD")


def _result() -> dict[str, object]:
    return {
        "schema_version": 1,
        "verdict": "accept",
        "summary": "The exact range is sound.",
        "blocking_findings": [],
        "non_blocking_risks": [],
        "reviewed_paths": ["reviewed.txt"],
    }


def _request(repo: Path, base: str, head: str, **overrides: object) -> ReviewRequest:
    values: dict[str, object] = {
        "work_item_entity_id": "entity-1",
        "work_item_identifier": "WI-TEST",
        "work_item_title": "Test adapter",
        "work_item_body": "Review it",
        "work_item_status": "in_review",
        "repository_identity": "example/agent-notes",
        "repository_root": repo,
        "base_revision": base,
        "head_revision": head,
        "harness": "claude",
        "requested_model": "opus",
        "accepted_reported_models": ("claude-opus-4-6",),
        "identity_assurance": IdentityAssurance.ASSERTED,
        "acknowledge_asserted_reviewer": True,
    }
    values.update(overrides)
    return ReviewRequest(**values)  # type: ignore[arg-type]


def _fake_claude(tmp_path: Path, envelope: dict[str, object], *, exit_code: int = 0) -> Path:
    script = tmp_path / "fake-claude"
    payload = json.dumps(envelope, separators=(",", ":"))
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('9.9.9 (Claude Code)')\n"
        "    raise SystemExit(0)\n"
        "prompt = sys.stdin.read()\n"
        "assert 'Perform an adversarial code review' in prompt\n"
        "assert '--output-format' in sys.argv and 'json' in sys.argv\n"
        "assert '--safe-mode' in sys.argv and '--bare' not in sys.argv\n"
        "assert '--strict-mcp-config' in sys.argv\n"
        "assert '--json-schema' in sys.argv\n"
        "assert sys.argv[sys.argv.index('--tools') + 1] == 'Read,Grep,Glob'\n"
        f"print({payload!r})\n"
        f"raise SystemExit({exit_code})\n"
    )
    script.chmod(0o755)
    return script


def _envelope(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": "session-123",
        "modelUsage": {"claude-opus-4-6": {"inputTokens": 10}},
        "permission_denials": [],
        "structured_output": _result(),
    }
    value.update(overrides)
    return value


def test_claude_runner_conforms_and_preserves_machine_identity(
    review_repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    repo, base, head = review_repo
    runner = ClaudePrintRunner("opus", executable=str(_fake_claude(tmp_path, _envelope())))
    evaluation = evaluate_review(_request(repo, base, head), runner)

    process = evaluation.artifact.process
    assert process.harness == "claude"
    assert process.harness_version == "9.9.9 (Claude Code)"
    assert process.requested_model == "opus"
    assert process.reported_model == "claude-opus-4-6"
    assert process.session_id == "session-123"
    assert evaluation.artifact.result.summary == "The exact range is sound."


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"session_id": ""}, "session_id"),
        ({"structured_output": None}, "structured_output"),
        ({"modelUsage": {}}, "one model identity"),
        ({"modelUsage": {"one": {}, "two": {}}}, "one model identity"),
        ({"permission_denials": [{"tool": "Read"}]}, "permission denial"),
        ({"subtype": "error"}, "successful result"),
        ({"is_error": True}, "marked as an error"),
    ],
)
def test_claude_runner_rejects_invalid_envelope(
    tmp_path: Path, overrides: dict[str, object], message: str
) -> None:
    runner = ClaudePrintRunner(
        "opus", executable=str(_fake_claude(tmp_path, _envelope(**overrides)))
    )
    with pytest.raises(RunnerProtocolError, match=message):
        runner.run(prompt="Perform an adversarial code review", cwd=tmp_path, timeout_seconds=5)


def test_claude_runner_rejects_model_mismatch_at_evaluation(
    review_repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    repo, base, head = review_repo
    runner = ClaudePrintRunner(
        "opus",
        executable=str(
            _fake_claude(
                tmp_path,
                _envelope(modelUsage={"claude-sonnet-4-6": {"inputTokens": 10}}),
            )
        ),
    )
    with pytest.raises(DelegatedReviewError) as raised:
        evaluate_review(_request(repo, base, head), runner)
    assert raised.value.code is ReviewErrorCode.IDENTITY_MISMATCH


def test_claude_nonzero_exit_is_not_parsed_as_a_result(tmp_path: Path) -> None:
    runner = ClaudePrintRunner(
        "opus", executable=str(_fake_claude(tmp_path, _envelope(), exit_code=7))
    )
    process = runner.run(
        prompt="Perform an adversarial code review", cwd=tmp_path, timeout_seconds=5
    )
    assert process.exit_code == 7
    assert process.structured_outputs == ()


def test_claude_runner_does_not_inherit_operator_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer subprocess must run with a minimal, explicit environment.

    Delegated review is a trust boundary (WI-054): the harness must not inherit
    the operator's environment, or host config, DSNs, and cloud credentials leak
    into the review path. A fake harness records the environment it actually
    receives; we assert the operator's variables are absent and only the
    documented minimal set (PATH, isolated HOME) is present.
    """
    monkeypatch.setenv("AGENT_NOTES_LEAK_SENTINEL", "do-not-leak")
    monkeypatch.setenv("REGISTA_DSN", "postgresql://operator@host/prod")
    marker = tmp_path / "child-env.json"
    script = tmp_path / "env-claude"
    payload = json.dumps(_envelope(), separators=(",", ":"))
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "if '--version' in sys.argv:\n"
        "    print('9.9.9 (Claude Code)')\n"
        "    raise SystemExit(0)\n"
        f"open({str(marker)!r}, 'w').write(json.dumps(dict(os.environ)))\n"
        f"print({payload!r})\n"
        "raise SystemExit(0)\n"
    )
    script.chmod(0o755)

    runner = ClaudePrintRunner("opus", executable=str(script))
    process = runner.run(
        prompt="Perform an adversarial code review", cwd=tmp_path, timeout_seconds=10
    )
    assert process.exit_code == 0

    child_env = json.loads(marker.read_text())
    assert "AGENT_NOTES_LEAK_SENTINEL" not in child_env
    assert "REGISTA_DSN" not in child_env
    # Only the documented minimal set is passed explicitly. CPython's PEP 538
    # locale coercion may inject LC_CTYPE into the child; that is not operator
    # state, so tolerate it while proving nothing else survives.
    assert set(child_env) - {"LC_CTYPE"} == {"PATH", "HOME"}
    # HOME is isolated: not the operator's real home directory.
    assert child_env["HOME"] != os.path.expanduser("~")


def _recording_claude(tmp_path: Path, marker: Path) -> Path:
    """A fake claude that records the env it receives and echoes a valid result."""
    payload = json.dumps(_envelope(), separators=(",", ":"))
    script = tmp_path / "recording-claude"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "if '--version' in sys.argv:\n"
        "    print('9.9.9 (Claude Code)')\n"
        "    raise SystemExit(0)\n"
        "sys.stdin.read()\n"
        f"open({str(marker)!r}, 'w').write(json.dumps(dict(os.environ)))\n"
        f"print({payload!r})\n"
        "raise SystemExit(0)\n"
    )
    script.chmod(0o755)
    return script


def test_claude_runner_injects_credential_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer subprocess receives the declared credential channel (WI-056).

    WI-054's minimal env left a real reviewer unable to authenticate. The
    credential channel injects only the operator-declared auth env vars into the
    child's environment (inject-don't-surface). A fake harness records the env it
    receives and echoes the structured envelope; we assert the credential reaches
    the child while no operator state leaks and the secret never surfaces in the
    result.
    """
    monkeypatch.setenv("AGENT_NOTES_LEAK_SENTINEL", "do-not-leak")
    marker = tmp_path / "child-env.json"
    runner = ClaudePrintRunner(
        "opus",
        executable=str(_recording_claude(tmp_path, marker)),
        credentials={"ANTHROPIC_API_KEY": "sk-test-secret-do-not-log"},
    )
    process = runner.run(
        prompt="Perform an adversarial code review", cwd=tmp_path, timeout_seconds=10
    )

    child_env = json.loads(marker.read_text())
    assert child_env["ANTHROPIC_API_KEY"] == "sk-test-secret-do-not-log"
    # Inject-don't-surface: the operator's env never leaks even with a channel.
    assert "AGENT_NOTES_LEAK_SENTINEL" not in child_env
    # The child sees only the sandbox base plus the declared credential.
    assert set(child_env) - {"LC_CTYPE"} == {"PATH", "HOME", "ANTHROPIC_API_KEY"}
    # The surfaced result carries the structured output, never the secret.
    assert process.exit_code == 0
    assert b"sk-test-secret-do-not-log" not in process.structured_outputs[0]


def test_credential_channel_never_surfaces_secret_in_artifact(
    review_repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    """The credential reaches the child but never the persisted artifact (WI-056).

    Inject-don't-surface end-to-end: a full evaluate_review with a credential
    channel produces an artifact whose canonical serialization does not contain
    the secret, even though the child subprocess received it.
    """
    repo, base, head = review_repo
    marker = tmp_path / "child-env.json"
    runner = ClaudePrintRunner(
        "opus",
        executable=str(_recording_claude(tmp_path, marker)),
        credentials={"ANTHROPIC_API_KEY": "sk-artifact-secret"},
    )
    evaluation = evaluate_review(_request(repo, base, head), runner)

    secret = "sk-artifact-secret".encode()
    assert secret not in evaluation.artifact.canonical_bytes()
    assert secret not in json.dumps(evaluation.artifact.to_dict()).encode()
    # And the credential did reach the child.
    assert json.loads(marker.read_text())["ANTHROPIC_API_KEY"] == "sk-artifact-secret"


@pytest.mark.parametrize(
    ("credentials", "match"),
    [
        ({"": "v"}, "name must be a non-empty"),
        ({"   ": "v"}, "name must be a non-empty"),
        ({"GOOD_NAME": ""}, "must be a non-empty string"),
        ({"PATH": "/x"}, "reserved for the reviewer sandbox"),
        ({"HOME": "/x"}, "reserved for the reviewer sandbox"),
    ],
)
def test_runner_rejects_invalid_credentials(credentials: dict[str, str], match: str) -> None:
    """Misconfigured channels fail at construction, not silently in the child."""
    with pytest.raises(ValueError, match=match):
        ClaudePrintRunner("opus", credentials=credentials)


def test_credential_error_never_surfaces_another_secret() -> None:
    """A validation failure surfaces only the env-var name, never another entry's
    secret value (inject-don't-surface)."""
    secret = "sk-super-secret-never-log-me"
    with pytest.raises(ValueError) as raised:
        ClaudePrintRunner("opus", credentials={"ANTHROPIC_API_KEY": secret, "BAD_NAME": ""})
    assert secret not in str(raised.value)


def test_bounded_process_separates_streams_and_enforces_caps(tmp_path: Path) -> None:
    command = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.write('out'); sys.stderr.write('err')",
    ]
    result = run_bounded_process(command, cwd=tmp_path, timeout_seconds=5)
    assert result.stdout == b"out"
    assert result.stderr == b"err"

    with pytest.raises(RunnerOutputLimitError, match="stdout"):
        run_bounded_process(
            [sys.executable, "-c", "print('x' * 10000)"],
            cwd=tmp_path,
            timeout_seconds=5,
            max_stdout_bytes=100,
        )


def test_bounded_process_timeout_kills_descendant(tmp_path: Path) -> None:
    marker = tmp_path / "descendant-finished"
    child = f"import pathlib,time; time.sleep(1); pathlib.Path({str(marker)!r}).write_text('bad')"
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(30)"
    )
    with pytest.raises(TimeoutError):
        run_bounded_process([sys.executable, "-c", parent], cwd=tmp_path, timeout_seconds=0.2)
    time.sleep(1.1)
    assert not marker.exists()


def test_bounded_process_timeout_applies_while_child_ignores_stdin(tmp_path: Path) -> None:
    with pytest.raises(TimeoutError):
        run_bounded_process(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            timeout_seconds=0.2,
            stdin=b"x" * (1024 * 1024),
        )


def test_bounded_process_maps_wait_timeout_after_streams_close(tmp_path: Path) -> None:
    with pytest.raises(TimeoutError):
        run_bounded_process(
            [
                sys.executable,
                "-c",
                "import os,time; os.close(1); os.close(2); time.sleep(30)",
            ],
            cwd=tmp_path,
            timeout_seconds=0.2,
        )


def test_schema_is_strict_and_result_parser_remains_authoritative() -> None:
    schema = review_result_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert os.path.sep not in json.dumps(schema) or os.path.sep == "/"


# This one must NOT be skipped on Windows — it is the Windows behaviour.
@pytest.mark.skipif(os.name == "nt", reason="simulated on POSIX; real on Windows")
def test_runner_refuses_on_windows(monkeypatch, tmp_path):
    """Refuse up front rather than degrade into an unenforceable bound.

    A runner that cannot kill the process group or cap the output stream is
    worse than no runner: delegated review would report a timeout while the
    harness kept running. Simulated by forcing os.name, so the guard is
    covered on the platform CI actually runs.
    """
    from agent_notes.core.work_item._review_runners import (
        RunnerUnsupportedPlatformError,
    )

    monkeypatch.setattr(os, "name", "nt")
    with pytest.raises(RunnerUnsupportedPlatformError) as exc:
        run_bounded_process([sys.executable, "-c", "pass"], cwd=tmp_path, timeout_seconds=5)
    assert "Windows" in str(exc.value)
