"""The canonical principal grammar, vendored from regista 0.7.2 (Plan 026 D3).

Cases ported from regista v0.7.2 ``tests/test_p23_principal_grammar.py``. The
grammar is a wire contract: an id that validates today must keep validating,
and a refused spelling must stay refused with the same code and reason.
"""

from __future__ import annotations

import unicodedata

import pytest

from agent_notes.core.actor import (
    PRINCIPAL_KINDS,
    principal_id_problem,
    validate_principal_id,
)
from agent_notes.core.errors import AgentNotesError, ErrorCode


def _reason(value):
    problem = principal_id_problem(value)
    return None if problem is None else problem[1]


def test_kind_set_is_exactly_the_three_ratified_kinds():
    assert PRINCIPAL_KINDS == frozenset({"human", "agent", "service"})


def test_witness_is_not_a_kind():
    assert "witness" not in PRINCIPAL_KINDS
    assert principal_id_problem("witness:0f6c1b2e-1111-4222-8333-444455556666") == (
        "ungrammatical",
        "kind_not_canonical",
    )
    assert principal_id_problem("service:witness.0f6c1b2e-1111-4222-8333-444455556666") is None


def test_key_is_never_a_principal():
    assert _reason("key:pk_4f70570b481745a8") == "key_is_never_a_principal"
    with pytest.raises(AgentNotesError) as exc:
        validate_principal_id("key:pk_4f70570b481745a8")
    assert exc.value.code == ErrorCode.PRINCIPAL_ID_UNGRAMMATICAL
    assert exc.value.detail["reason"] == "key_is_never_a_principal"


@pytest.mark.parametrize(
    "value",
    [
        "human:itadmin",
        "agent:mvmcc03",
        "agent:mvmcc02",
        "agent:mvmhermes01",
        "agent:mvmcc03-claude-code",
        "service:witness.0f6c1b2e-1111-4222-8333-444455556666",
        # An IdP subject containing colons is legal and unambiguous.
        "service:idp:tenant-a/svc-7",
        # Every subject-char class in one subject.
        "agent:a.b_c-d~e:f/g9",
        "human:0f6c1b2e-1111-4222-8333-444455556666",
    ],
)
def test_canonical_examples_validate(value):
    assert principal_id_problem(value) is None
    assert validate_principal_id(value) == value


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("", "empty"),
        ("Human:itadmin", "kind_not_canonical"),
        ("HUMAN:itadmin", "kind_not_canonical"),
        ("robot:x", "kind_not_canonical"),
        ("unknown:x", "kind_not_canonical"),
        ("human:", "subject_empty"),
        ("human::x", "subject_edge_char"),
        ("human:.x", "subject_edge_char"),
        ("human:-x", "subject_edge_char"),
        ("human:_x", "subject_edge_char"),
        ("human:/x", "subject_edge_char"),
        ("human:x:", "subject_edge_char"),
        ("human:x.", "subject_edge_char"),
        ("human:x-", "subject_edge_char"),
        ("human:x_", "subject_edge_char"),
        ("human:x/", "subject_edge_char"),
        ("human:a b", "subject_char_not_allowed"),
        ("human:a+b", "subject_char_not_allowed"),
        ("human:a@b", "subject_char_not_allowed"),
        ("human:a%b", "subject_char_not_allowed"),
        ("human:ítadmin", "non_ascii"),
        ("someone@example.com", "not_kind_colon_subject"),
        ("a b", "not_kind_colon_subject"),
    ],
)
def test_ungrammatical_examples_are_refused_with_a_named_reason(value, reason):
    assert principal_id_problem(value) == ("ungrammatical", reason)
    with pytest.raises(AgentNotesError) as exc:
        validate_principal_id(value)
    assert exc.value.code == ErrorCode.PRINCIPAL_ID_UNGRAMMATICAL
    assert exc.value.detail["reason"] == reason


def test_subject_length_bounds_are_1_to_247():
    assert principal_id_problem("agent:" + "a" * 247) is None
    assert _reason("agent:" + "a" * 248) == "subject_too_long"


def test_total_length_ceiling_is_256_utf8_bytes():
    longest = "service:" + "a" * 247
    assert len(longest) == 255
    assert principal_id_problem(longest) is None
    # Over the byte ceiling is refused before any subject reasoning.
    assert _reason("agent:" + "a" * 300) == "too_long"


def test_nfc_is_a_no_op_on_accepted_values():
    for value in ("human:itadmin", "service:idp:tenant-a/svc-7", "agent:a~b_c-d.e"):
        assert unicodedata.normalize("NFC", value) == value
    # A decomposed non-ASCII value is refused as non-ASCII, which is checked first.
    assert _reason("human:éx") == "non_ascii"


def test_non_string_input_is_classified_not_crashed():
    for value in (None, 42, b"human:itadmin", ["human:itadmin"]):
        assert _reason(value) == "not_a_string"


@pytest.mark.parametrize(
    "value",
    [
        "human-1",
        "suite-service",
        "mvmcc03-agent",
        "agent-notes",
        "claude-fable-5",
        "adversarial-reviewer-nemotron-3-ultra",
        "plm",
    ],
)
def test_legacy_bare_names_classify_as_bare_name_not_junk(value):
    assert principal_id_problem(value) == ("bare_name", "bare_name_not_canonical")


def test_bare_name_refusal_is_its_own_code_and_says_what_to_do():
    with pytest.raises(AgentNotesError) as exc:
        validate_principal_id("mvmcc03-agent")
    assert exc.value.code == ErrorCode.PRINCIPAL_ID_NOT_CANONICAL
    assert exc.value.detail["remedy"] == "principal_alias_bound"
    assert "§2.5" in exc.value.message
    assert "never satisfies signature binding" in exc.value.message
    assert ErrorCode.PRINCIPAL_ID_NOT_CANONICAL != ErrorCode.PRINCIPAL_ID_UNGRAMMATICAL


def test_path_names_the_offending_setting():
    with pytest.raises(AgentNotesError) as exc:
        validate_principal_id("robot:x", path="actor_id")
    assert str(exc.value) == (
        "[PRINCIPAL_ID_UNGRAMMATICAL] actor_id 'robot:x' does not satisfy the canonical "
        "principal grammar (TRUST-DOMAIN.md §2.1): kind_not_canonical"
    )
