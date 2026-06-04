"""Unit tests for git-history reconciliation (DB-free).

These drive a throwaway git repo, so they need git on PATH but no Postgres.
"""

from __future__ import annotations

import subprocess

import pytest

from agent_notes.core.git_reconcile import scan_git_for_resolutions


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.test")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "commit", "--allow-empty", "-q", "-m", "initial commit")
    return tmp_path


def _commit(repo, message):
    _git(repo, "commit", "--allow-empty", "-q", "-m", message)


def test_detects_resolve_verb_before_identifier(repo):
    _commit(repo, "resolve BC-094: decompose queries.py")
    hits = scan_git_for_resolutions(repo, ["BC-094"])
    assert "BC-094" in hits
    assert hits["BC-094"]["subject"] == "resolve BC-094: decompose queries.py"
    assert len(hits["BC-094"]["commit"]) == 12


def test_detects_multiple_ids_in_one_commit(repo):
    _commit(repo, "resolve BC-094, BC-124, BC-125: regression tests + rename")
    hits = scan_git_for_resolutions(repo, ["BC-094", "BC-124", "BC-125"])
    assert set(hits) == {"BC-094", "BC-124", "BC-125"}


def test_detects_past_tense_after_identifier(repo):
    _commit(repo, "BC-077 fixed: SSRF guard on CT lookup")
    assert "BC-077" in scan_git_for_resolutions(repo, ["BC-077"])


def test_detects_intent_in_body_not_just_subject(repo):
    _commit(repo, "harden CT module\n\nThis also closes BC-200 for good.")
    assert "BC-200" in scan_git_for_resolutions(repo, ["BC-200"])


def test_ignores_mere_mention_without_resolution_intent(repo):
    _commit(repo, "start work on BC-300 (still in progress)")
    assert scan_git_for_resolutions(repo, ["BC-300"]) == {}


def test_ignores_unrelated_identifiers(repo):
    _commit(repo, "resolve BC-094")
    assert scan_git_for_resolutions(repo, ["BC-999"]) == {}


@pytest.mark.parametrize(
    "message",
    [
        "working on BC-300 (not done)",
        "not yet fixed BC-300",
        "todo: resolve BC-300 later",
        "WIP: closing BC-300",
        "BC-300 partially fixed",
        "revert resolve BC-300",
    ],
)
def test_negation_and_intent_words_are_not_resolutions(repo, message):
    _commit(repo, message)
    assert scan_git_for_resolutions(repo, ["BC-300"]) == {}


def test_most_recent_resolution_wins(repo):
    _commit(repo, "resolve BC-400 (first attempt)")
    _commit(repo, "resolve BC-400 again after reopen")
    hits = scan_git_for_resolutions(repo, ["BC-400"])
    assert hits["BC-400"]["subject"] == "resolve BC-400 again after reopen"


def test_word_boundary_avoids_substring_false_positive(repo):
    # "prefix" must not satisfy the \bfix\b verb.
    _commit(repo, "refactor prefix handling near BC-500")
    assert scan_git_for_resolutions(repo, ["BC-500"]) == {}


def test_missing_repo_root_is_safe():
    assert scan_git_for_resolutions(None, ["BC-1"]) == {}
    assert scan_git_for_resolutions("/nonexistent/path/xyz", ["BC-1"]) == {}


def test_non_git_directory_is_safe(tmp_path):
    assert scan_git_for_resolutions(tmp_path, ["BC-1"]) == {}


def test_empty_identifiers_short_circuits(repo):
    assert scan_git_for_resolutions(repo, []) == {}
