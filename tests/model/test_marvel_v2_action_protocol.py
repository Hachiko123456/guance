from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "domain"))
from declaration_fixture_oracle import (  # noqa: E402
    NEGATIVE_VECTORS, POSITIVE_VECTORS, RULES, declaration_from_canonical,
    matrix_declaration, positive_declaration, wire_level,
)

from guance.infrastructure.model.marvel_v2 import (  # noqa: E402
    ActionCandidate, ActionDeclaration, ActionProtocolError, ActionProtocolSession, ActionRow,
    ExchangeTransferFacts, LegalActionRows, MarvelProtocolError, decode_canonical_tokens,
    decode_committed_tokens, decode_exchange_resolution, declaration_tokens,
    generate_legal_rows,
)
from guance.public_state_contract import guandan_card_id_to_wire  # noqa: E402

PARITY_CASES = tuple((level, case) for level in RULES["level_parity"]["levels"]
                     for case in level["cases"])


def _json_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, separators=(",", ":"),
                                     sort_keys=True).encode()).hexdigest()


def _candidate_from_canonical(canonical: dict, level: int) -> ActionCandidate:
    declaration = ActionDeclaration.from_domain(declaration_from_canonical(canonical))
    return ActionCandidate.play("LEAD_PLAY", declaration, level)


@pytest.mark.parametrize("vector", POSITIVE_VECTORS, ids=lambda item: item["id"])
def test_all_73_positive_vectors_match_tokens_rows_session_and_decoder(vector):
    domain_declaration = positive_declaration(vector)
    declaration = ActionDeclaration.from_domain(domain_declaration)
    level = wire_level(vector)
    candidate = ActionCandidate.play("LEAD_PLAY", declaration, level)
    expected_tokens = tuple(vector["expected"]["complete_token_sequence"])
    assert declaration_tokens(declaration, level) == candidate.tokens == expected_tokens
    independent = decode_canonical_tokens(expected_tokens, phase="LEAD_PLAY", level=level,
                                          own_hand=candidate.physical_cards)
    assert independent.declaration == declaration
    expected_rows = tuple(tuple(row) for row in vector["expected"]["marvel_v2_action_rows"])
    for index, expected in enumerate(expected_rows):
        generated = generate_legal_rows((candidate,), phase="LEAD_PLAY",
                                        prefix=expected_tokens[:index])
        assert len(generated.rows) == 1
        assert generated.rows[0].values == expected
        assert generated.weights == (1.0,)
        assert generated.is_commit == (expected_tokens[index] == 7,)
    domain = object()
    session = ActionProtocolSession("golden", 9, "LEAD_PLAY", candidate.physical_cards,
                                    (candidate,), domain)
    committed = None
    for expected_token in expected_tokens:
        row = next(row for row in session.legal_rows.rows if row.token == expected_token)
        step = session.select(row)
        assert step.session.confirmed_domain is domain
        assert session.confirmed_domain is domain
        session = step.session
        committed = step.committed_action or committed
    assert session.closed and committed is not None
    assert committed.declaration == declaration
    assert committed.storage_order == tuple(
        item for item in vector["expected"]["canonical_declaration"]["declaration_card_ids"])
    assert committed.action_order == tuple(
        item for item in vector["expected"]["canonical_declaration"]["canonical_action_card_ids"])
    assert committed.commit_identity is not None
    assert session.token_step == len(expected_tokens) and session.committed_step == 1


@pytest.mark.parametrize("level,case", PARITY_CASES,
                         ids=lambda value: value["id"] if "id" in value else value["level"]["name"])
def test_338_level_matrix_matches_token_and_action_row_hashes(level, case):
    declaration = ActionDeclaration.from_domain(matrix_declaration(level, case))
    tokens = declaration_tokens(declaration, wire_level(level))
    rows = [list(ActionRow.for_token("LEAD_PLAY", index, token).values)
            for index, token in enumerate(tokens)]
    assert _json_hash(list(tokens)) == case["complete_token_sequence_sha256"]
    assert _json_hash(rows) == case["marvel_action_rows_sha256"]


@pytest.mark.parametrize("vector", NEGATIVE_VECTORS, ids=lambda item: item["id"])
def test_all_50_negative_token_sequences_fail_closed_against_reference(vector):
    canonical = vector["expected"]["reference_canonical_declaration"]
    reference = None if canonical is None else _candidate_from_canonical(
        canonical, wire_level(vector)
    )
    candidate_tokens = vector["candidate"].get("complete_token_sequence")
    forged = (None,) if candidate_tokens is None else tuple(candidate_tokens)
    if reference is not None:
        assert forged != reference.tokens
    hand = tuple(item["card_id"] for item in vector["candidate"].get("physical_cards", ()))
    with pytest.raises(MarvelProtocolError):
        decode_canonical_tokens(forged, phase="LEAD_PLAY", level=wire_level(vector),
                                own_hand=hand)


def _simple(family: str, card_ids: tuple[int, ...], rank: int) -> ActionDeclaration:
    wires = tuple(guandan_card_id_to_wire(card_id) for card_id in card_ids)
    return ActionDeclaration(family, (rank,), rank, None, len(wires), wires, (), wires, None)


@pytest.mark.parametrize("family,cards", [
    ("SINGLE", (0,)), ("PAIR", (0, 54)), ("TRIPLE", (0, 1, 2)),
    ("RANK_BOMB", (0, 1, 2, 3)),
])
def test_simple_and_rank_bomb_families_roundtrip(family, cards):
    declaration = _simple(family, cards, 3)
    candidate = ActionCandidate.play("LEAD_PLAY", declaration, 5)
    decoded = decode_committed_tokens(candidate.tokens, (candidate,), own_hand=cards)
    assert decoded.family == family
    assert decoded.physical_cards == cards
    assert decoded.declared_ranks == (3,)
    assert decoded.comparison_rank == 3


def test_pass_is_follow_only_and_tribute_return_preserve_physical_card():
    pass_candidate = ActionCandidate.pass_action()
    assert decode_committed_tokens((6,), (pass_candidate,), own_hand=()).kind == "pass"
    with pytest.raises(ValueError):
        ActionCandidate("pass", "LEAD_PLAY").validate()
    for phase, kind in (("TRIBUTE", "tribute"), ("RETURN", "return")):
        candidate = ActionCandidate.exchange(phase, 107)
        decoded = decode_committed_tokens(candidate.tokens, (candidate,), own_hand=(107,))
        assert decoded.kind == kind and decoded.physical_cards == (107,)


def test_anti_tribute_and_exchange_resolution_validation():
    result = decode_exchange_resolution((), anti_tribute=True)
    assert result.anti_tribute and not result.transfers
    transfer = ExchangeTransferFacts("tribute", 1, 0, 5)
    with pytest.raises(ValueError, match="anti_tribute_conflict"):
        decode_exchange_resolution((transfer,), anti_tribute=True)


def test_legal_rows_are_unique_stable_complete_and_never_top_n_truncated():
    candidates = tuple(ActionCandidate.exchange("TRIBUTE", card_id) for card_id in range(108))
    rows = generate_legal_rows(candidates, phase="TRIBUTE")
    assert len(rows.rows) == 108
    assert [row.token for row in rows.rows] == list(range(64, 172))
    duplicate = candidates + (candidates[0],)
    with pytest.raises(ActionProtocolError, match="legal_candidate_nonunique"):
        generate_legal_rows(duplicate, phase="TRIBUTE")


def test_session_rejects_stale_foreign_and_row5_only_forgery():
    declaration = ActionDeclaration.from_domain(positive_declaration(POSITIVE_VECTORS[0]))
    candidate = ActionCandidate.play("LEAD_PLAY", declaration, wire_level(POSITIVE_VECTORS[0]))
    session = ActionProtocolSession("session-a", 1, "LEAD_PLAY", candidate.physical_cards,
                                    (candidate,), {"confirmed": True})
    original = session.legal_rows.rows[0]
    next_session = session.select(original).session
    with pytest.raises(ActionProtocolError, match="row_foreign_or_stale"):
        next_session.select(original)
    forged = ActionRow(original.values, "session-b", 1, original.generation)
    with pytest.raises(ActionProtocolError, match="row_foreign_or_stale"):
        session.select(forged)
    other_candidate = ActionCandidate.exchange("TRIBUTE", 0)
    other_rows = generate_legal_rows((other_candidate,), phase="TRIBUTE",
                                     session_id="session-a", state_version=1)
    assert other_rows.rows[0].generation != original.generation
    changed_payload = list(original.values)
    changed_payload[3] = 299
    with pytest.raises(ValueError):
        session.select(ActionRow(tuple(changed_payload), "session-a", 1, original.generation))


def test_prefix_steps_do_not_commit_or_mutate_confirmed_domain_and_clone_is_independent():
    declaration = ActionDeclaration.from_domain(positive_declaration(POSITIVE_VECTORS[0]))
    candidate = ActionCandidate.play("LEAD_PLAY", declaration, wire_level(POSITIVE_VECTORS[0]))
    domain = {"revision": 7}
    session = ActionProtocolSession("clone", 7, "LEAD_PLAY", candidate.physical_cards,
                                    (candidate,), domain, token_step=20, committed_step=3)
    clone = session.clone()
    step = clone.select(clone.legal_rows.rows[0])
    assert step.committed_action is None
    assert step.session.token_step == 21 and step.session.committed_step == 3
    assert session.prefix == () and session.token_step == 20
    assert session.confirmed_domain is domain is step.session.confirmed_domain
    assert domain == {"revision": 7}


def test_explicit_action_row_capacity_overflow_is_rejected():
    row = ActionRow.for_token("LEAD_PLAY", 0, 8)
    with pytest.raises(ValueError, match="legal_rows_overflow"):
        LegalActionRows((row,) * 257, (1.0,) * 257, (False,) * 257,
                        "LEAD_PLAY", ()).validate()


def test_session_from_complete_state_preserves_exact_counters_and_prefix():
    from guance.infrastructure.model.marvel_v2 import ExchangeFacts, MarvelState, PreviousResultFacts
    state = MarvelState(0, 0, 0, None, "LEAD_PLAY", 5, (0,), (), (), (8,),
                        (1, 27, 27, 27), (0, 0, 0, 0), (), 0, None, None, 0,
                        PreviousResultFacts((1, 2, 3, 4), 0, "HEAD_THIRD", False),
                        ExchangeFacts(), 4, 2, True)
    candidate = ActionCandidate.play("LEAD_PLAY", _simple("SINGLE", (0,), 3), 5)
    session = ActionProtocolSession.from_state(state, session_id="state", state_version=4,
                                               candidates=(candidate,), confirmed_domain=object())
    assert session.prefix == (8,) and session.token_step == 4 and session.committed_step == 2
