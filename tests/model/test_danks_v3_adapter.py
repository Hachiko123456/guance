"""D03/D04 adapter tests: legal-candidate formation, ambiguity, provenance.

The D03 exclusion/inclusion assertion runs at the legal-candidate list
formation point (``second_actions`` output), before ranking, Top10
truncation and model inference, exactly per the phase 1 frozen design
(d03_design.json fixtures).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from guance.infrastructure.model.danks_v3.adapter import (
    DanksV3AdapterError, declaration_context_rank,
    declaration_interpretations, declaration_to_follow_target, eligibility,
    legal_follow_candidates, legal_lead_candidates, observation_context,
    wire_cards_to_labels, wire_rank_to_engine_label,
)
from guance.infrastructure.model.danks_v3.core import normalize_engine_rows
from guance.infrastructure.model.danks_v3.history import (
    from_public_state, live_capture, synthetic_normal_entry,
)
from guance.public_state_contract import PublicDeclaration, WildAssignment

ROOT = Path(__file__).resolve().parents[2]
D03 = ROOT / "tests/fixtures/danks_v3" / "d03_design.json"
LEVEL = 5
OPPONENT_CARDS = (0x35, 0x45, 0x25, 0x39, 0x49)  # C5 D5 H5 C9 D9
OBSERVER_ACTION_CARDS = (0x1A, 0x2A, 0x3A, 0x13, 0x23)  # ST HT CT S3 H3


@pytest.fixture(scope="module")
def d03():
    return json.loads(D03.read_text(encoding="utf-8"))


def _observer_hand(d03) -> tuple[int, ...]:
    return tuple(d03["observer_hand"])


def _natural() -> PublicDeclaration:
    return PublicDeclaration("FULL_HOUSE", (5, 9), 5, None, 5, OPPONENT_CARDS,
        (WildAssignment(0x25, 5, "HEARTS", False),),
        canonical_action_cards=(0x35, 0x45, 0x25, 0x39, 0x49))


def _nine_substituted() -> PublicDeclaration:
    return PublicDeclaration("FULL_HOUSE", (9, 5), 9, None, 5, OPPONENT_CARDS,
        (WildAssignment(0x25, 9, "CLUBS", True),),
        canonical_action_cards=(0x25, 0x39, 0x49, 0x35, 0x45))


def test_declaration_to_follow_target_uses_frozen_rank_labels():
    kind, rank, cards = declaration_to_follow_target(_natural())
    assert (kind, rank) == ("ThreeWithTwo", "5")
    assert cards == ["S3", "S3", "S3", "S9", "S9"] or True  # labels below
    assert sorted(cards) == ["C5", "C5", "C9", "D5", "D9"] or True
    # Exact: canonical action cards are wire codes C5 D5 H5 C9 D9 (triple
    # first by storage order), mapped to DanKS labels then engine jokers.
    assert cards == ["C5", "D5", "H5", "C9", "D9"]
    kind_b, rank_b, _cards_b = declaration_to_follow_target(_nine_substituted())
    assert (kind_b, rank_b) == ("ThreeWithTwo", "9")


def test_d03_legal_candidate_list_exclusion_and_inclusion(d03):
    # The normalized candidate list carries DanKS kind names: the engine row
    # kind "ThreeWithTwo" normalizes to "TriplePlus".
    hand = [wire_cards_to_labels([code])[0] for code in _observer_hand(d03)]
    wanted = ("TriplePlus", "T", tuple(sorted(["ST", "HT", "CT", "S3", "H3"])))

    natural_list = legal_follow_candidates(hand, LEVEL, _natural())
    keys = [(c.kind, c.rank, tuple(sorted(c.cards))) for c in natural_list
            if c.kind != "PASS"]
    assert wanted not in keys  # comparison rank = level 5 -> excluded
    assert len(keys) == d03["cases"]["confirmed_triple_five_pair_nine"]["legal_action_count"]

    nine_list = legal_follow_candidates(hand, LEVEL, _nine_substituted())
    keys_b = [(c.kind, c.rank, tuple(sorted(c.cards))) for c in nine_list
              if c.kind != "PASS"]
    assert wanted in keys_b  # comparison rank 9 -> included
    assert len(keys_b) == \
        d03["cases"]["confirmed_triple_nine_pair_five"]["legal_action_count"]


def test_d03_assertion_point_precedes_ranking(d03):
    """The assertion reads the raw second_actions list via normalize_engine_rows
    - the same formation point the production engine consumes before ranking."""
    hand = [wire_cards_to_labels([code])[0] for code in _observer_hand(d03)]
    kind, rank, cards = declaration_to_follow_target(_natural())
    from guance.infrastructure.model.danks_v3.vendor.guandan.engine.moves import Moves
    moves = Moves()
    moves.parse_second_action(hand, 0, "5", [kind, rank, cards], None, None)
    raw_keys = [(row[0], row[1], tuple(sorted(row[2]))) for row in moves.action_list]
    assert ("ThreeWithTwo", "T", tuple(sorted(["ST", "HT", "CT", "S3", "H3"]))) \
        not in raw_keys
    normalized = normalize_engine_rows(moves.action_list)
    assert all(isinstance(c.index, int) for c in normalized)


def test_declaration_interpretations_match_the_phase1_oracle():
    interpretations = declaration_interpretations(OPPONENT_CARDS, LEVEL)
    summary = [(d.declared_ranks, d.comparison_rank,
                d.wild_assignments[0].assigned_rank,
                d.wild_assignments[0].assigned_suit,
                d.wild_assignments[0].substituted) for d in interpretations]
    assert summary == [((5, 9), 5, 5, "HEARTS", False),
                       ((5, 9), 5, 5, "CLUBS", True),
                       ((9, 5), 9, 9, "CLUBS", True)]
    # Same entity cards, same three-field key for the two comparison-5 facts,
    # full declarations differ.
    assert interpretations[0].comparison_rank == interpretations[1].comparison_rank == 5
    assert interpretations[0].wild_assignments != interpretations[1].wild_assignments
    assert interpretations[0].to_dict() != interpretations[1].to_dict()


def test_wire_labels_collapse_deck_copies():
    labels = wire_cards_to_labels([0x13, 0x113, 0x25, 0x125, 0x0E, 0x10F])
    assert labels == ["S3", "S3", "H5", "H5", "BJ", "RJ"]
    with pytest.raises(DanksV3AdapterError):
        wire_cards_to_labels([0x55])


def test_observation_context_maps_relative_seats_and_counts():
    from guance.contracts import CanonicalPublicGameView, PublicAction
    # Observer seat 2 holds two cards and follows the single-8 lead of seat 1.
    lead = PublicAction(1, 1, "play", (0x18,), 1, 2, 0)
    view = CanonicalPublicGameView("s", "g", 3, 2, 2, 1, 0, 5, 0,
        (0x13, 0x19), (27, 26, 2, 27), (lead,), lead, False)
    ctx = observation_context(view)
    # The frame is GAME-ABSOLUTE in DANKS seats (the reflected wire frame:
    # danks = (-wire) % 4; the DanKS engine advances play clockwise
    # (+1) while the Guance state machine advances counter-clockwise (-1)).
    assert ctx["my_seat"] == 2  # danks_seat(2) = 2
    assert ctx["public_counts"] == [27, 27, 2, 26]
    assert ctx["current_kind"] == "Single" and ctx["current_size"] == 1
    assert ctx["last_player"] == 3  # danks_seat(1) = 3
    assert ctx["curRank"] == "5"
    assert ctx["played_cards"] == ["S8"]
    assert ctx["known_hand_cards"] == {"2": ["S3", "S9"]}
    assert ctx["history"] == [{"pos": 3, "kind": "Single", "rank": "8",
                               "cards": ["S8"]}]


def test_wire_level_labels_use_frozen_engine_labels():
    from guance.infrastructure.model.danks_v3.adapter import wire_rank_to_engine_label
    assert [wire_rank_to_engine_label(level) for level in (1, 2, 10, 11, 12, 13)]         == ["A", "2", "T", "J", "Q", "K"]


def test_joker_hand_reaches_the_engine_as_sb_hr():
    """P3-R2 regression: DanKS joker labels convert to SB/HR for the Moves
    call; the engine otherwise reads "RJ"[1] as a jack."""
    hand = ["S3", "H3", "BJ", "RJ"]
    candidates = legal_lead_candidates(hand, 2)
    keys = [(c.kind, c.rank, tuple(sorted(c.cards))) for c in candidates]
    assert ("Single", "BJ", ("BJ",)) in keys
    assert ("Single", "RJ", ("RJ",)) in keys


@pytest.mark.parametrize("level", [11, 12, 13])
def test_high_level_lead_matches_the_frozen_engine_oracle(level):
    """P3-R1 regression: levels 11-13 produce J/Q/K engine labels; the legal
    lead list must equal a direct correct-label Moves reference."""
    from guance.infrastructure.model.danks_v3.adapter import wire_cards_to_labels
    from guance.infrastructure.model.danks_v3.vendor.guandan.engine.moves import Moves
    from guance.infrastructure.model.danks_v3.vendor.danks.retrieval.cards import ALL_CARDS
    import random as _random
    rng = _random.Random(20261120 + level)
    hand = rng.sample(list(ALL_CARDS) * 2, 27)
    candidates = legal_lead_candidates(hand, level)
    moves = Moves()
    label = wire_rank_to_engine_label(level)
    hearts = sum(1 for card in hand if card == f"H{label}")
    engine_hand = [{"BJ": "SB", "RJ": "HR"}.get(card, card) for card in hand]
    moves.parse_first_action(engine_hand, hearts, label)
    # Both paths go through the same row normalization so kinds and joker
    # labels are comparable.
    reference = normalize_engine_rows(moves.action_list)
    actual = [(c.kind, c.rank, tuple(sorted(c.cards))) for c in candidates]
    expected = [(c.kind, c.rank, tuple(sorted(c.cards))) for c in reference]
    assert len(actual) == len(expected)
    assert sorted(map(str, actual)) == sorted(map(str, expected))


def test_wild03_frozen_anchor_seven_legal_actions():
    """The acceptance counterexample: the adapter produced 19 for the frozen
    wild-03 sample; after the fix it must match the frozen oracle exactly."""
    samples = json.loads((ROOT / "tests/fixtures/danks_v3"
                          / "samples_32.json").read_text(encoding="utf-8"))
    sample = next(row for row in samples["samples"]
                  if row["sample_id"] == "danks-p1-wild-03")
    declaration = PublicDeclaration.from_dict(
        sample["current_winning_declaration_fact"]["declaration"])
    hand = [wire_cards_to_labels([code])[0]
            for code in sample["observation"]["hand"]]
    items = legal_follow_candidates(hand, sample["level"], declaration)
    assert len(items) == sample["legality_oracle"]["legal_action_count"] == 7


def test_provenance_gate_live_capture_ineligible():
    assert live_capture().eligible is False
    assert live_capture().ineligible_reason
    assert synthetic_normal_entry().eligible is True


def test_from_public_state_stays_unsupported():
    from guance.domain.cards import full_deck
    from guance.domain.events import ActionEvent, DealEvent, StartEvent
    from guance.domain.state import GameState
    hand = tuple(sorted(full_deck())[:27])
    state = GameState("phase3-history")
    state.apply_deal(DealEvent(-1, 5, hand))
    state.apply_start(StartEvent(0))
    state.confirm_my_seat_relative("self")
    state.apply_action(ActionEvent(0, "play", (hand[0],), combo_type=1,
                                   next_player=3, remaining_after=26,
                                   action_index=1, expected_trick=0))
    provenance = from_public_state(state, "phase3-history")
    assert provenance.public_history_complete == "unsupported"
    assert provenance.transport_contiguous == "unsupported"
    assert provenance.eligible is False


def test_eligibility_rules():
    from guance.contracts import (CanonicalPublicGameView, CurrentWinningDeclarationFact,
                                  DeclaredFacts, PublicAction)
    lead = PublicAction(1, 1, "play", OPPONENT_CARDS, 5, 3, 0)
    view = CanonicalPublicGameView("s", "g", 4, 3, 3, 1, 0, 5, 0,
        tuple(sorted({*OPPONENT_CARDS[:0], 0x1A, 0x2A, 0x3A, 0x13, 0x23})),
        (5, 22, 27, 27), (lead,), lead, False)
    # Free lead without facts: eligible.
    free_view = CanonicalPublicGameView("s", "g", 4, 0, 0, 0, 2, 5, 0,
        (0x13, 0x19), (2, 27, 27, 27), (), None, True)
    assert eligibility(free_view, None, synthetic_normal_entry())["eligible"]
    # Live provenance: ineligible regardless of facts.
    blocked = eligibility(view, None, live_capture())
    assert not blocked["eligible"] and blocked["code"] == "HISTORY_PROVENANCE_UNVERIFIED"
    # Missing declaration over the ambiguous five-card set: ineligible.
    missing_facts = DeclaredFacts(schema="Guandan.DeclaredFacts.v1",
        current_winning_declaration=CurrentWinningDeclarationFact(
            status="unsupported", reason="live_declaration_unverified"))
    verdict = eligibility(view, missing_facts, synthetic_normal_entry())
    assert not verdict["eligible"] and verdict["code"] == "DECLARATION_AMBIGUOUS"
    # Confirmed declaration: eligible.
    known_facts = DeclaredFacts(schema="Guandan.DeclaredFacts.v1",
        current_winning_declaration=CurrentWinningDeclarationFact(
            status="known", declaration=_nine_substituted()))
    assert eligibility(view, known_facts, synthetic_normal_entry())["eligible"]


# --- Real-replay fixes (2026-11-21): joker representations + straights -----

SMALL_JOKER = 0x0E  # wire suit 0 rank 14
BIG_JOKER = 0x0F    # wire suit 0 rank 15


def _joker_declaration(gd_rank: int, card: int) -> PublicDeclaration:
    declaration = PublicDeclaration("SINGLE", (gd_rank,), gd_rank, None, 1,
                                    (card,), (), canonical_action_cards=(card,))
    declaration.validate_for_level(5)
    return declaration


def test_joker_representation_roundtrip_is_frozen():
    from guance.infrastructure.model.danks_v3.adapter import (
        joker_representation_roundtrip, joker_representations)
    assert joker_representation_roundtrip() == [
        (16, "BJ", "B", "SB"), (17, "RJ", "R", "HR")]
    assert joker_representations(16) == ("BJ", "B", "SB")
    assert joker_representations(17) == ("RJ", "R", "HR")
    with pytest.raises(DanksV3AdapterError) as exc:
        joker_representations(15)
    assert exc.value.code == "JOKER_RANK_INVALID"


def test_joker_follow_target_uses_the_moves_boundary():
    """The Moves comparison-point field reads B/R; faces stay SB/HR; the
    strategy context keeps BJ/RJ (the real-replay rows 416/957/1181/1234)."""
    small = _joker_declaration(16, SMALL_JOKER)
    big = _joker_declaration(17, BIG_JOKER)
    assert declaration_to_follow_target(small) == ("Single", "B", ["SB"])
    assert declaration_to_follow_target(big) == ("Single", "R", ["HR"])
    assert declaration_context_rank(small) == "BJ"
    assert declaration_context_rank(big) == "RJ"


def test_plain_singles_never_beat_joker_targets():
    """Baseline row 416 defect: the wrong target rank let 4/T/A into the legal
    list.  With the B/R boundary rank the engine offers no plain single."""
    hand = [wire_cards_to_labels([c])[0] for c in (0x14, 0x1A, 0x21, SMALL_JOKER)]
    keys = [(c.kind, c.rank, tuple(sorted(c.cards))) for c in
            legal_follow_candidates(hand, 5, _joker_declaration(16, SMALL_JOKER))]
    assert keys == [("PASS", "PASS", ())]
    # The big joker beats the small joker; the small joker does not.
    hand_pair = [wire_cards_to_labels([c])[0] for c in (0x14, SMALL_JOKER, BIG_JOKER)]
    keys_with_rj = [(c.kind, c.rank, tuple(sorted(c.cards))) for c in
                    legal_follow_candidates(hand_pair, 5,
                                            _joker_declaration(16, SMALL_JOKER))]
    assert ("Single", "RJ", ("RJ",)) in keys_with_rj
    assert ("Single", "BJ", ("BJ",)) not in keys_with_rj
    # Nothing beats the big joker without bombs.
    keys_big = [(c.kind, c.rank, tuple(sorted(c.cards))) for c in
                legal_follow_candidates(hand_pair, 5,
                                        _joker_declaration(17, BIG_JOKER))]
    assert keys_big == [("PASS", "PASS", ())]


def _straight_declaration(cards_storage, cards_action, declared_rank, comparison):
    declaration = PublicDeclaration("STRAIGHT", (declared_rank,), comparison,
                                    None, 5, cards_storage, (),
                                    canonical_action_cards=cards_action)
    declaration.validate_for_level(5)
    return declaration


def test_straight_follow_target_maps_to_engine_straight():
    """Rows 1246/1258/1286: the missing STRAIGHT mapping caused
    FOLLOW_TARGET_UNSUPPORTED; the confirmed comparison rank carries over."""
    # 2-3-4-5-6 with the natural club-five level card; window (15,3,4,5,6).
    target = _straight_declaration((0x33, 0x44, 0x35, 0x46, 0x12),
                                   (0x12, 0x33, 0x44, 0x35, 0x46), 15, 6)
    assert declaration_to_follow_target(target) == ("Straight", "6", [
        "S2", "C3", "D4", "C5", "D6"])
    assert declaration_context_rank(target) == "6"


def test_level5_wheel_straight_boundary_beats_and_loses():
    """The diagnosis extra case: at level 5 the mixed-suit 23456 (wild heart
    five as the 5) beats the mixed-suit A2345 (natural club five), both
    frozen rule sets accept it, and the low straight never beats TJQKA."""
    wheel = _straight_declaration((0x43, 0x24, 0x35, 0x11, 0x32),
                                  (0x11, 0x32, 0x43, 0x24, 0x35), 14, 5)
    assert declaration_to_follow_target(wheel)[:2] == ("Straight", "5")
    hand = [wire_cards_to_labels([c])[0] for c in
            (0x12, 0x33, 0x44, 0x25, 0x46)]  # S2 C3 D4 H5(wild) D6
    keys = [(c.kind, c.rank, tuple(sorted(c.cards))) for c in
            legal_follow_candidates(hand, 5, wheel)]
    assert ("Straight", "6", ("C3", "D4", "D6", "H5", "S2")) in keys
    assert len(keys) == 2  # PASS + exactly that straight
    high = _straight_declaration((0x1A, 0x2B, 0x3C, 0x4D, 0x11),
                                 (0x1A, 0x2B, 0x3C, 0x4D, 0x11), 10, 14)
    keys_high = [(c.kind, c.rank) for c in legal_follow_candidates(hand, 5, high)]
    assert all(kind in ("PASS", "Bomb", "StraightFlush", "FourKings")
               for kind, _rank in keys_high)
    assert ("Straight", "6") not in keys_high


def test_other_engine_supported_follow_families_map():
    """PAIR_SEQUENCE/STRAIGHT_FLUSH/FOUR_KINGS reach the engine; the A-2
    TRIPLE_SEQUENCE stays unsupported (recorded frozen rule divergence)."""
    pair_seq = PublicDeclaration("PAIR_SEQUENCE", (15,), 4, None, 6,
        (0x33, 0x23, 0x34, 0x24, 0x32, 0x22), (),
        canonical_action_cards=(0x32, 0x22, 0x33, 0x23, 0x34, 0x24),
        protocol_length=3)
    pair_seq.validate_for_level(5)
    assert declaration_to_follow_target(pair_seq)[:2] == ("ThreePair", "4")
    trips_seq = PublicDeclaration("TRIPLE_SEQUENCE", (14,), 15, None, 6,
        (0x31, 0x21, 0x11, 0x32, 0x22, 0x12), (),
        canonical_action_cards=(0x31, 0x21, 0x11, 0x32, 0x22, 0x12),
        protocol_length=2)
    trips_seq.validate_for_level(5)
    with pytest.raises(DanksV3AdapterError) as exc:
        declaration_to_follow_target(trips_seq)
    assert exc.value.code == "FOLLOW_TARGET_UNSUPPORTED"
