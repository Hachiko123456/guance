"""Output-validation tests: follow beats, wild positive, ambiguity (D03).

The 9/3-follow-8 control must reach the shared output validator INSIDE
worker.run: 9 passes and renders, 3 is rejected there with
MODEL_OUTPUT_NOT_BEATING.  The full_house_wild_substituted_pair positive
renders (wild-aware shape feasibility).  A wild-containing follow lead
whose engine-point interpretation changes the verdict fails closed for
non-pass candidates (MODEL_OUTPUT_AMBIGUOUS) without declared facts.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from guance.contracts import (AdviceCandidate, CanonicalAdviceResult,
                              CanonicalPublicGameView, ModelContractError,
                              PublicAction, validate_advice_for_view)
from guance.infrastructure.model.codec import dumps, envelope
from guance.infrastructure.model.worker import run

LEVEL = 5
# A hand carrying the pairs/bombs the controls need (two decks).
HAND = (0x13, 0x113, 0x23, 0x123,          # four threes (bomb)
        0x19, 0x119, 0x29,                  # three nines
        0x25,                               # heart five (the level wildcard)
        0x14, 0x114, 0x1A, 0x11A, 0x2A, 0x3A,  # two fours, three tens
        0x17, 0x117, 0x27)                  # three sevens
# The eights stay out of the hand: the opponent's lead plays them.


def follow_view(lead_cards=(0x18,), combo_type=1, hand=HAND):
    lead = PublicAction(1, 1, "play", lead_cards, combo_type, 0, 0)
    return CanonicalPublicGameView("t", "g", 2, 0, 0, 0, 2, LEVEL, 0,
        tuple(hand), (len(hand), 26, 27, 27), (lead,), lead, False)


def worker_reply(view: CanonicalPublicGameView, candidates, advisor_log: list):
    class Advisor:
        def load(self) -> dict:
            return {}

        def recommend(self, current, request_id):
            advisor_log.append("advisor_recommend")
            return CanonicalAdviceResult(*current.key, request_id,
                provider="fixture", model_version="v1", score_kind="test_score",
                candidates=tuple(candidates))

        def close(self) -> None:
            pass

    class Factory:
        def create(self, source):
            return Advisor()

    ids = {"request_id": "ov-request", "session_id": view.session_id,
        "game_id": view.game_id, "state_version": view.state_version,
        "epoch": 0, "generation": 1}
    messages = [envelope("load", ids, source="controlled-fixture"),
        envelope("recommend", ids, request_kind="public",
                 observation=view.to_dict()),
        envelope("stop", ids)]
    output = io.StringIO()
    code = run(input_stream=io.StringIO("".join(dumps(m) for m in messages)),
               output_stream=output, factory=Factory())
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    return code, next(r for r in replies if r["type"] in {"result", "error"})


def test_nine_follows_eight_renders_three_follows_eight_rejected():
    view = follow_view()
    log = []
    code, reply = worker_reply(view, (AdviceCandidate((0x19,), "single",
        "single", 0.5, score_kind="test_score"),), log)
    assert code == 0 and reply["type"] == "result" and log == ["advisor_recommend"]
    log = []
    code, reply = worker_reply(view, (AdviceCandidate((0x13,), "single",
        "single", 0.5, score_kind="test_score"),), log)
    assert code == 0
    assert reply["type"] == "error"
    assert reply["error_code"] == "MODEL_OUTPUT_NOT_BEATING"
    assert log == ["advisor_recommend"]  # the failure stage is the validator


def test_pass_follow_always_legal_and_pair_orders():
    view = follow_view(lead_cards=(0x18, 0x118), combo_type=2)
    log = []
    code, reply = worker_reply(view, (AdviceCandidate((), "pass", "pass", 0.5,
        score_kind="test_score"),), log)
    assert reply["type"] == "result"  # pass is always a legal follow
    log = []
    code, reply = worker_reply(view, (AdviceCandidate((0x14, 0x114), "pair",
        "pair", 0.5, score_kind="test_score"),), log)
    assert reply["type"] == "error"
    assert reply["error_code"] == "MODEL_OUTPUT_NOT_BEATING"  # 4s < 8s
    log = []
    code, reply = worker_reply(view, (AdviceCandidate((0x1A, 0x11A), "pair",
        "pair", 0.5, score_kind="test_score"),), log)
    assert reply["type"] == "result"  # 10s > 8s


def test_bomb_beats_any_non_bomb():
    view = follow_view()
    log = []
    bomb = (0x13, 0x113, 0x23, 0x123)  # four threes from the hand
    code, reply = worker_reply(view, (AdviceCandidate(bomb, "bomb", "bomb",
        0.5, score_kind="test_score"),), log)
    assert reply["type"] == "result"  # a bomb beats the single 8


def test_wild_full_house_positive_renders():
    """full_house_wild_substituted_pair: the heart five completes the triple;
    the wild-aware shape check accepts what the natural-only check rejected."""
    view = CanonicalPublicGameView("t", "g", 2, 0, 0, 0, 2, LEVEL, 0,
        (0x17, 0x117, 0x19, 0x119, 0x25, 0x13), (6, 27, 27, 27), (), None, True)
    candidate = AdviceCandidate((0x17, 0x117, 0x25, 0x19, 0x119), "full",
        "三带二", 0.5, claim=(7, 7, 7, 9, 9), score_kind="test_score")
    log = []
    code, reply = worker_reply(view, (candidate,), log)
    assert code == 0 and reply["type"] == "result"


def test_wild_full_house_follow_with_wild_target_shape():
    """A wild-containing follow candidate against a concrete target beats it
    under the max interpretation."""
    view = follow_view(lead_cards=(0x18, 0x118), combo_type=2)
    # Wild completes a pair of nines: beats the pair of eights.
    candidate_cards = (0x19, 0x25)
    log = []
    code, reply = worker_reply(view, (AdviceCandidate(candidate_cards, "pair",
        "pair", 0.5, score_kind="test_score"),), log)
    assert reply["type"] == "result"


def test_ambiguous_wild_full_house_lead_fails_closed_without_facts():
    """C5 D5 H5 C9 D9 lead: the engine-point interpretation differs between
    triple-5 (level, 15) and triple-9 (9); without a confirmed declaration
    non-pass candidates fail closed."""
    lead_cards = (0x35, 0x45, 0x25, 0x39, 0x49)
    hand = (0x1A, 0x2A, 0x3A, 0x13, 0x23)
    view = follow_view(lead_cards=lead_cards, combo_type=5, hand=hand)
    log = []
    code, reply = worker_reply(view, (AdviceCandidate((0x1A, 0x2A, 0x3A,
        0x13, 0x23), "full", "full", 0.5, score_kind="test_score"),), log)
    assert reply["type"] == "error"
    assert reply["error_code"] == "MODEL_OUTPUT_AMBIGUOUS"
    log = []
    code, reply = worker_reply(view, (AdviceCandidate((), "pass", "pass", 0.5,
        score_kind="test_score"),), log)
    assert reply["type"] == "result"


def test_concrete_lead_derives_target_without_declared_facts():
    view = follow_view(lead_cards=(0x18, 0x118, 0x48), combo_type=3)
    log = []
    triple_nine = (0x19, 0x119, 0x29)
    code, reply = worker_reply(view, (AdviceCandidate(triple_nine, "triple",
        "triple", 0.5, score_kind="test_score"),), log)
    assert reply["type"] == "result"
    log = []
    triple_seven = (0x17, 0x117, 0x27)
    code, reply = worker_reply(view, (AdviceCandidate(triple_seven, "triple",
        "triple", 0.5, score_kind="test_score"),), log)
    assert reply["type"] == "error"
    assert reply["error_code"] == "MODEL_OUTPUT_NOT_BEATING"


@pytest.mark.parametrize(("kind", "cards"), [
    ("straight", (0x11, 0x12, 0x13, 0x14, 0x15, 0x25)),
    ("sflush", (0x13, 0x14, 0x15, 0x26, 0x17)),
    ("sflush", (0x13, 0x14, 0x15, 0x17, 0x19)),
    ("rocket", (0xE, 0xF, 0x10E, 0x15)),
    ("bomb", (0x25, 0x18, 0x28)),
])
def test_worker_rejects_malformed_special_combos_on_a_free_lead(kind, cards):
    view = CanonicalPublicGameView(
        "s", "g", 2, 0, 0, 0, 2, LEVEL, 0, cards,
        (len(cards), 27, 27, 27), (), None, True,
    )
    _, reply = worker_reply(view, (AdviceCandidate(cards, kind, kind, 0.5,
                                    score_kind="test_score"),), [])
    assert reply["type"] == "error"
    assert reply["error_code"] == "MODEL_OUTPUT_INVALID"


def test_p5r1_natural_serial_windows_pass_validation():
    """P5-R1 regression: engine-legal serial shapes must pass the shared
    validator - the wire-value consecutiveness check broke A-high windows."""
    import random

    def straight_view(cards, kind):
        hand = tuple(sorted(set(cards) | {0x13}))
        return CanonicalPublicGameView("t", "g", 2, 0, 0, 0, 2, LEVEL, 0,
            hand, (len(hand), 27, 27, 27), (), None, True), cards, kind

    # TJQKA straight (A high), the acceptance counterexample.
    view, cards, kind = straight_view((0x1A, 0x1B, 0x1C, 0x1D, 0x11), "straight")
    log = []
    code, reply = worker_reply(view, (AdviceCandidate(cards, kind, "顺子", 0.5,
        score_kind="test_score"),), log)
    assert reply["type"] == "result", reply
    # A2345 wheel and 34567.
    for cards in ((0x11, 0x12, 0x13, 0x14, 0x15), (0x13, 0x14, 0x15, 0x16, 0x17)):
        view, cards, kind = straight_view(cards, "straight")
        code, reply = worker_reply(view, (AdviceCandidate(cards, kind, "顺子",
            0.5, score_kind="test_score"),), log)
        assert reply["type"] == "result", reply
    # Serial pair 223344 and serial triple 222333 through a wide hand.
    wide = (0x12, 0x112, 0x13, 0x113, 0x14, 0x114, 0x15, 0x115, 0x16, 0x116,
            0x17, 0x117)
    view = CanonicalPublicGameView("t", "g", 2, 0, 0, 0, 2, LEVEL, 0, wide,
        (12, 27, 27, 27), (), None, True)
    for cards, kind in (((0x12, 0x112, 0x13, 0x113, 0x14, 0x114), "serial_pair"),
                        ((0x13, 0x113, 0x14, 0x114, 0x15, 0x115), "serial_pair")):
        code, reply = worker_reply(view, (AdviceCandidate(cards, kind, "三连对",
            0.5, score_kind="test_score"),), log)
        assert reply["type"] == "result", (cards, reply)


def test_p5r1_all_engine_lead_serials_pass_validation():
    root = Path(__file__).resolve().parents[2]
    """Enumerate the engine lead actions for a run-rich 27-card hand and
    assert every straight/serial-pair/serial-triple row passes the shared
    validation (the acceptance fix-guidance suggestion)."""
    import random
    from guance.domain.cards import full_deck
    from guance.infrastructure.model.danks_v3.vendor.guandan.engine.moves import Moves
    from guance.infrastructure.model.danks_v3.core import (normalize_engine_rows,
                                                           engine_label,
                                                           to_danks_label)
    from guance.infrastructure.model.danks_v3.adapter import wire_cards_to_labels
    hand_codes = random.Random(20261121).sample(list(full_deck()), 27)
    labels = wire_cards_to_labels(hand_codes)
    moves = Moves()
    moves.parse_first_action([engine_label(label) for label in labels],
                             sum(1 for code in hand_codes
                                 if (code >> 4) & 15 == 2 and (code & 15) == 2),
                             "2")
    legal = normalize_engine_rows(moves.action_list)
    rows = [(c.kind, list(c.cards)) for c in legal
            if c.kind in ("Straight", "StraightPair", "StraightTriple")]
    assert rows, "the hand must contain serial candidates for this control"
    view = CanonicalPublicGameView("t", "g", 2, 0, 0, 0, 2, LEVEL, 0,
        tuple(sorted(hand_codes)), (27, 27, 27, 27), (), None, True)
    label_codes: dict = {}
    for code in sorted(hand_codes):
        label_codes.setdefault(wire_cards_to_labels([code])[0], []).append(code)
    from guance.infrastructure.model.danks_v3.provider import _KIND_TO_VALIDATOR
    checked = 0
    for kind, card_labels in rows:
        codes = []
        taken: set = set()
        feasible = True
        for label in card_labels:
            available = [code for code in label_codes.get(label, [])
                         if code not in taken]
            if not available:
                feasible = False
                break
            taken.add(available[0])
            codes.append(available[0])
        if not feasible:
            continue
        # The provider maps DanKS kinds to validator kinds before the shared
        # validation; enumerate through the same mapping.
        candidate = AdviceCandidate(tuple(sorted(codes)), _KIND_TO_VALIDATOR[kind],
                                    "串牌", 0.5, score_kind="test_score")
        result = CanonicalAdviceResult(*view.key, "req", provider="p",
            model_version="v", score_kind="s", candidates=(candidate,))
        validate_advice_for_view(view, result)
        checked += 1
    assert checked >= 1


def test_declared_target_beats_the_derived_ambiguity():
    """With confirmed facts, the declared comparison decides even when the
    raw cards are interpretation-dependent (the D03 mechanism)."""
    from guance.public_state_contract import PublicDeclaration, WildAssignment
    lead_cards = (0x35, 0x45, 0x25, 0x39, 0x49)
    hand = (0x1A, 0x2A, 0x3A, 0x13, 0x23)
    view = follow_view(lead_cards=lead_cards, combo_type=5, hand=hand)
    nine_declared = PublicDeclaration("FULL_HOUSE", (9, 5), 9, None, 5,
        lead_cards, (WildAssignment(0x25, 9, "CLUBS", True),),
        canonical_action_cards=(0x25, 0x39, 0x49, 0x35, 0x45))
    from guance.contracts import CurrentWinningDeclarationFact, DeclaredFacts
    facts = DeclaredFacts(schema="Guandan.DeclaredFacts.v1",
        current_winning_declaration=CurrentWinningDeclarationFact(
            status="known", declaration=nine_declared))
    candidate = AdviceCandidate((0x1A, 0x2A, 0x3A, 0x13, 0x23), "full",
        "full", 0.5, score_kind="test_score")
    # Direct validator call with facts: comp 9 -> triple-10 full house beats.
    validate_advice_for_view(view, _result(candidate), declared_facts=facts)
    five_declared = PublicDeclaration("FULL_HOUSE", (5, 9), 5, None, 5,
        lead_cards, (WildAssignment(0x25, 5, "HEARTS", False),),
        canonical_action_cards=(0x35, 0x45, 0x25, 0x39, 0x49))
    facts_five = DeclaredFacts(schema="Guandan.DeclaredFacts.v1",
        current_winning_declaration=CurrentWinningDeclarationFact(
            status="known", declaration=five_declared))
    try:
        validate_advice_for_view(view, _result(candidate),
                                 declared_facts=facts_five)
        raise AssertionError("comp-5 target accepted a comp-10 follow")
    except ModelContractError as exc:
        assert exc.code == "MODEL_OUTPUT_NOT_BEATING"


def _result(candidate) -> CanonicalAdviceResult:
    from guance.contracts import RequestIdentity
    return CanonicalAdviceResult("t", "g", 2, RequestIdentity("r", "t", "g", 2).request_id,
        provider="p", model_version="v", score_kind="s", candidates=(candidate,))


# --- Real-replay fixes (2026-11-21): sequence-target comparison points ----

from guance.public_state_contract import PublicDeclaration


def _declared_straight(storage, action, declared_rank, comparison):
    declaration = PublicDeclaration("STRAIGHT", (declared_rank,), comparison,
                                    None, 5, storage, (),
                                    canonical_action_cards=action)
    declaration.validate_for_level(LEVEL)
    return declaration


def _declared_facts(declaration):
    from guance.contracts import (CurrentWinningDeclarationFact, DeclaredFacts)
    return DeclaredFacts(schema="Guandan.DeclaredFacts.v2",
        current_winning_declaration=CurrentWinningDeclarationFact(
            status="known", declaration=declaration),
        history_origin="synthetic_normal_entry",
        public_history_complete="known", history_complete_value=True,
        transport_contiguous="known", transport_value=True)


def test_non_wild_level_card_keeps_natural_rank_in_following_straight():
    wheel = _declared_straight((0x43, 0x24, 0x35, 0x11, 0x32),
                               (0x11, 0x32, 0x43, 0x24, 0x35), 14, 5)
    # 23456 uses a spade five, a natural five within the straight.
    hand = (0x12, 0x33, 0x44, 0x15, 0x46)
    view = follow_view(lead_cards=wheel.canonical_cards, combo_type=4, hand=hand)
    result = CanonicalAdviceResult(*view.key, "natural-level", provider="fixture",
        model_version="v1", score_kind="test_score", candidates=(
            AdviceCandidate(hand, "straight", "straight", 0.5, score_kind="test_score"),))
    validate_advice_for_view(view, result, declared_facts=_declared_facts(wheel))


def test_wheel_straight_target_compares_in_natural_window_space():
    """The level-5 collision: the wheel A2345 declaration (comparison 5) must
    not read as the level-card 15 rule; mixed-suit 23456 (wild five) beats it
    and an equal A2345 does not (the diagnosis MODEL_OUTPUT_NOT_BEATING)."""
    from guance.contracts import validate_advice_for_view
    wheel = _declared_straight((0x43, 0x24, 0x35, 0x11, 0x32),
                               (0x11, 0x32, 0x43, 0x24, 0x35), 14, 5)
    hand = (0x12, 0x33, 0x44, 0x25, 0x46)  # S2 C3 D4 H5(wild) D6
    view = follow_view(lead_cards=(0x43, 0x24, 0x35, 0x11, 0x32),
                       combo_type=4, hand=hand)
    facts = _declared_facts(wheel)
    result = CanonicalAdviceResult(*view.key, "wheel-request", provider="fixture",
        model_version="v1", score_kind="test_score", candidates=(
            AdviceCandidate(hand, "straight", "straight", 0.5,
                            score_kind="test_score"),))
    validate_advice_for_view(view, result, declared_facts=facts)  # accepted
    equal = (0x21, 0x12, 0x23, 0x14, 0x15)  # HA S2 H3 S4 S5 - wheel, no wild
    equal_view = follow_view(lead_cards=(0x43, 0x24, 0x35, 0x11, 0x32),
                             combo_type=4, hand=equal)
    equal_result = CanonicalAdviceResult(*equal_view.key, "equal-request",
        provider="fixture", model_version="v1", score_kind="test_score",
        candidates=(AdviceCandidate(equal, "straight", "straight", 0.5,
                                    score_kind="test_score"),))
    with pytest.raises(ModelContractError) as exc:
        validate_advice_for_view(equal_view, equal_result, declared_facts=facts)
    assert exc.value.code == "MODEL_OUTPUT_NOT_BEATING"


def test_high_straight_target_rejects_the_low_straight():
    from guance.contracts import validate_advice_for_view
    high = _declared_straight((0x1A, 0x2B, 0x3C, 0x4D, 0x11),
                              (0x1A, 0x2B, 0x3C, 0x4D, 0x11), 10, 14)
    hand = (0x12, 0x33, 0x44, 0x25, 0x46)
    view = follow_view(lead_cards=(0x1A, 0x2B, 0x3C, 0x4D, 0x11),
                       combo_type=4, hand=hand)
    result = CanonicalAdviceResult(*view.key, "low-request", provider="fixture",
        model_version="v1", score_kind="test_score", candidates=(
            AdviceCandidate(hand, "straight", "straight", 0.5,
                            score_kind="test_score"),))
    with pytest.raises(ModelContractError) as exc:
        validate_advice_for_view(view, result,
                                 declared_facts=_declared_facts(high))
    assert exc.value.code == "MODEL_OUTPUT_NOT_BEATING"


def test_joker_single_target_identity_beats_only_by_the_higher_joker():
    from guance.contracts import validate_advice_for_view
    small = PublicDeclaration("SINGLE", (16,), 16, None, 1, (0x0E,), (),
                              canonical_action_cards=(0x0E,))
    small.validate_for_level(LEVEL)
    view = follow_view(lead_cards=(0x0E,), combo_type=1,
                       hand=(0x0F, 0x14))  # big joker + a four
    result = CanonicalAdviceResult(*view.key, "joker-request", provider="fixture",
        model_version="v1", score_kind="test_score", candidates=(
            AdviceCandidate((0x0F,), "single", "single", 0.5,
                            score_kind="test_score"),))
    validate_advice_for_view(view, result, declared_facts=_declared_facts(small))
    low_result = CanonicalAdviceResult(*view.key, "low-joker-request",
        provider="fixture", model_version="v1", score_kind="test_score",
        candidates=(AdviceCandidate((0x14,), "single", "single", 0.5,
                                    score_kind="test_score"),))
    with pytest.raises(ModelContractError) as exc:
        validate_advice_for_view(view, low_result, declared_facts=_declared_facts(small))
    assert exc.value.code == "MODEL_OUTPUT_NOT_BEATING"


def test_inconsistent_sequence_declaration_fails_closed():
    """A declared straight whose comparison rank contradicts its canonical
    cards must fail closed, never compare in a wrong point space."""
    from guance.contracts import _declared_target_identity
    forged = _declared_straight((0x43, 0x24, 0x35, 0x11, 0x32),
                                (0x11, 0x32, 0x43, 0x24, 0x35), 14, 5)
    assert _declared_target_identity(forged, LEVEL) == (0, 5, 4, 5)
    # Same cards declared with comparison 7: no window agrees -> fail closed.
    bad = PublicDeclaration("STRAIGHT", (14,), 7, None, 5,
                            (0x43, 0x24, 0x35, 0x11, 0x32), (),
                            canonical_action_cards=(0x11, 0x32, 0x43, 0x24, 0x35))
    with pytest.raises(ModelContractError) as exc:
        _declared_target_identity(bad, LEVEL)
    assert exc.value.code == "MODEL_INPUT_INVALID"
