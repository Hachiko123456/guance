from __future__ import annotations

from pathlib import Path

import pytest

from guance.infrastructure.model.marvel_v2.rules_oracle import (
    RulesOracleError, VerifiedGuandanRulesOracle,
)
from guance.public_state_contract import guandan_card_id_to_wire
from tests.domain.declaration_fixture_oracle import POSITIVE_VECTORS, positive_declaration, wire_level

SOURCE = Path(r"D:\project\guandan")
pytestmark = pytest.mark.skipif(not SOURCE.is_dir(), reason="verified Guandan source unavailable")
COMBO = {
    "FULL_HOUSE": 5, "STRAIGHT": 4, "PAIR_SEQUENCE": 6,
    "TRIPLE_SEQUENCE": 7, "STRAIGHT_FLUSH": 9, "FOUR_KINGS": 11,
}


def oracle() -> VerifiedGuandanRulesOracle:
    return VerifiedGuandanRulesOracle(SOURCE)


def test_verified_oracle_contains_every_frozen_positive_complex_declaration():
    rules = oracle()
    for vector in POSITIVE_VECTORS:
        expected = positive_declaration(vector)
        cards = tuple(guandan_card_id_to_wire(item["card_id"])
                      for item in vector["physical_cards"])
        declarations = rules.declarations(
            cards, wire_level=wire_level(vector), combo_type=COMBO[vector["family"]],
        )
        assert expected in declarations, vector["id"]


def test_unique_natural_single_and_ambiguous_wild_straight_are_distinguished():
    rules = oracle()
    single = rules.unique_declaration(
        (guandan_card_id_to_wire(0),), wire_level=5, combo_type=1,
    )
    assert single.family == "SINGLE" and single.declared_ranks == (3,)
    ambiguous_cards = tuple(guandan_card_id_to_wire(card_id)
                            for card_id in (8, 9, 10, 24, 25))
    assert len(rules.declarations(ambiguous_cards, wire_level=5, combo_type=5)) > 1
    with pytest.raises(RulesOracleError, match="LIVE_DECLARATION_AMBIGUOUS"):
        rules.unique_declaration(ambiguous_cards, wire_level=5, combo_type=5)


def test_legal_candidates_are_complete_unique_and_bound_to_own_hand():
    rules = oracle()
    hand = tuple(guandan_card_id_to_wire(card_id) for card_id in range(12))
    candidates = rules.legal_action_candidates(
        hand, wire_level=5, phase="LEAD_PLAY", current=None,
    )
    assert candidates
    assert len({candidate.tokens for candidate in candidates}) == len(candidates)
    assert all(set(candidate.physical_cards) <= set(range(12)) for candidate in candidates)


def test_tribute_oracle_accepts_every_physical_copy_tied_at_the_required_rank():
    rules = oracle()
    hand = tuple(guandan_card_id_to_wire(card_id) for card_id in (11, 63, 0, 1))
    legal = rules.legal_tribute_cards(hand, wire_level=5)
    assert legal == tuple(sorted((guandan_card_id_to_wire(11), guandan_card_id_to_wire(63))))
    assert rules.legal_tribute_card(hand, wire_level=5) == legal[0]
