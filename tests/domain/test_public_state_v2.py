"""GMV2-S2-001..008: synthetic public+self contract and aggregate evidence."""
from __future__ import annotations

from dataclasses import replace
import json
import traceback

import pytest

from guance.domain.cards import full_deck, encode_card
from guance.domain.events import (
    ActionEvent, DealEvent, DeclaredActionCommitted, ExchangeResolvedObserved,
    OwnExchangeSelectionObserved, OwnSeatCandidateEvent, PhaseObserved,
    PublicResultObserved, SettlementEvent, SourceMeta, StartEvent,
)
from guance.domain.state import GameState, SeatConfirmationConflict, StateError
from guance.public_state_contract import (
    CanonicalGuandanPublicState, CurrentWinning, EvidenceRef, ExchangeState, ExchangeTransfer, Knowledge,
    KnowledgeStatus as KS, PreviousResultSummary, PublicDeclaration,
    PublicStateContractError, WildAssignment, wire_rank_to_guandan_rank,
)
from declaration_fixture_oracle import (
    POSITIVE_VECTORS as DECLARATION_POSITIVE_VECTORS,
    declaration_raw as fixture_declaration_raw,
    positive_declaration as fixture_positive_declaration,
)


DECK = full_deck()
HAND = DECK[:27]
META = SourceMeta(sequence=0, connection_id="synthetic-connection", message_index=0, sub_index=0)


def dealt(hand=HAND):
    state = GameState("game-one")
    state.apply_deal(DealEvent(-1, 5, hand, meta=META))
    return state


def started(hand=HAND):
    state = dealt(hand)
    state.apply_start(StartEvent(0, meta=replace(META, sequence=1)))
    state.confirm_my_seat_relative("self")
    return state


def projection(state):
    return state.public_state_v2("synthetic-session")


def test_post_deal_partial_roundtrip_and_unknown_not_absent():
    state = dealt()
    view = projection(state)
    assert CanonicalGuandanPublicState.from_dict(view.to_dict()) == view
    assert view.identity.public_state_revision == 1
    assert view.schema_version == 2
    assert view.phase.status is KS.UNKNOWN
    assert view.observer_seat.status is KS.UNKNOWN
    assert view.previous_result.status is KS.UNKNOWN
    assert view.active_seat.status is KS.UNKNOWN
    assert view.current_winning.status is KS.UNKNOWN
    assert view.own_hand.value == tuple(sorted(HAND))
    assert view.remaining_counts.value == (27, 27, 27, 27)
    assert view.emptied_seats.value == ()
    assert view.ruleset_compatibility.status is KS.UNSUPPORTED
    assert view.history.observed_action_count == 0
    for fact in (view.history.public_history_complete, view.history.self_private_history_complete,
                 view.history.transport_contiguous, view.history.token_step, view.history.committed_step):
        assert fact.status is KS.UNSUPPORTED
    assert view.exchange.anti_tribute.status is KS.UNSUPPORTED
    assert view.own_hand.evidence.sequence == 0
    assert view.own_hand.evidence.sub_index == 0


def test_leader_winner_pass_and_non_self_turn_are_separate_from_v1():
    state = started()
    before = projection(state)
    assert before.phase.value == "LEAD_PLAY"
    assert before.current_winning.status is KS.ABSENT
    state.apply_action(ActionEvent(0, "play", (HAND[0],), combo_type=1, next_player=3))
    state.apply_action(ActionEvent(3, "play", (DECK[81],), combo_type=1, next_player=2))
    view = projection(state)
    assert view.phase.value == "FOLLOW_PLAY"
    assert view.leader_seat.value == 0
    assert view.current_winner_seat.value == 3
    assert view.active_seat.value == 2 != view.observer_seat.value
    assert view.current_winning.value.cards == (DECK[81],)
    assert view.current_winning.value.declaration.status is KS.UNSUPPORTED
    assert view.history.actions[-1].declaration.status is KS.UNSUPPORTED
    for seat, nxt, reset in [(2, 1, 0), (1, 0, 0), (0, 3, 1)]:
        state.apply_action(ActionEvent(seat, "pass", next_player=nxt, reset_flag=reset))
        assert projection(state).history.actions[-1].declaration.status is KS.ABSENT
    view = projection(state)
    assert view.phase.value == "LEAD_PLAY"
    assert view.leader_seat.value == 3
    assert view.current_winner_seat.status is KS.ABSENT
    assert view.consecutive_passes.value == 0
    assert view.identity.public_state_revision > before.identity.public_state_revision
    assert before.history.actions == ()
    assert state.observation("synthetic-session", 0).to_dict()["schema_version"] == 1


def test_consecutive_passes_reset_on_play_and_transaction_failure_keeps_v2():
    state = started()
    state.apply_action(ActionEvent(0, "play", (HAND[0],), combo_type=1, next_player=3))
    state.apply_action(ActionEvent(3, "pass", next_player=2, reset_flag=0))
    assert projection(state).consecutive_passes.value == 1
    old = projection(state)
    snap = state.snapshot()
    with pytest.raises(StateError):
        state.apply_action(ActionEvent(2, "play", (DECK[54],), combo_type=1, next_player=0))
    assert state.snapshot() == snap
    assert projection(state) == old
    state.apply_action(ActionEvent(2, "play", (DECK[54],), combo_type=1, next_player=1))
    assert projection(state).consecutive_passes.value == 0


def test_complete_declaration_preserves_canonical_order_without_combo_inference():
    state = started()
    action = ActionEvent(0, "play", HAND[:2], combo_type=71, next_player=3)
    declaration = PublicDeclaration(
        "PAIR", (14,), 14, None, 2, tuple(reversed(HAND[:2])),
        canonical_action_cards=tuple(reversed(HAND[:2])),
    )
    state.apply_declared_action(DeclaredActionCommitted(action, declaration))
    view = projection(state)
    committed = view.history.actions[0]
    assert committed.cards == declaration.canonical_cards
    assert committed.declaration.value == declaration
    assert view.current_winning.value.declaration.value == declaration
    assert state.history[0].combo_type == 71
    assert state.history[0].cards == HAND[:2]  # v1 wire order unchanged
    assert CanonicalGuandanPublicState.from_dict(view.to_dict()) == view
    assert view.history.self_private_history_complete.status is KS.UNSUPPORTED


@pytest.mark.parametrize("substituted", [False, True])
def test_wildcard_natural_and_substituted_semantics(substituted):
    wild = encode_card(2, 5)
    hand = (wild, *HAND[1:])
    state = started(hand)
    assignment = WildAssignment(wild, 8 if substituted else 5,
                                "CLUBS" if substituted else "HEARTS", substituted)
    declaration = PublicDeclaration(
        "SINGLE", (assignment.assigned_rank,), assignment.assigned_rank, None, 1,
        (wild,), (assignment,),
        canonical_action_cards=(wild,),
    )
    action = ActionEvent(0, "play", (wild,), combo_type=1, next_player=3)
    old = projection(state)
    expected = "declared action semantic invalid"
    with pytest.raises(StateError, match=expected):
        state.apply_declared_action(DeclaredActionCommitted(
            action, replace(declaration, wild_assignments=())))
    assert projection(state) == old
    state.apply_declared_action(DeclaredActionCommitted(action, declaration))
    assert projection(state).history.actions[0].declaration.value == declaration




def test_pair_of_different_natural_ranks_is_rejected_atomically():
    state = started()
    before = projection(state)
    cards = (HAND[0], HAND[2])
    declaration = PublicDeclaration(
        "PAIR", (14,), 14, None, 2, cards,
        canonical_action_cards=cards,
    )
    event = DeclaredActionCommitted(
        ActionEvent(0, "play", cards, combo_type=2, next_player=3), declaration)
    with pytest.raises(StateError, match="declared action semantic invalid"):
        state.apply_declared_action(event)
    assert projection(state) == before


def test_single_declared_and_comparison_rank_mismatch_is_rejected_atomically():
    state = started()
    before = projection(state)
    card = HAND[0]
    declaration = PublicDeclaration(
        "SINGLE", (15,), 15, None, 1, (card,),
        canonical_action_cards=(card,),
    )
    event = DeclaredActionCommitted(
        ActionEvent(0, "play", (card,), combo_type=1, next_player=3), declaration)
    with pytest.raises(StateError, match="declared action semantic invalid"):
        state.apply_declared_action(event)
    assert projection(state) == before


def test_valid_natural_pair_is_accepted():
    state = started()
    cards = HAND[:2]
    declaration = PublicDeclaration(
        "PAIR", (14,), 14, None, 2, cards,
        canonical_action_cards=cards,
    )
    state.apply_declared_action(DeclaredActionCommitted(
        ActionEvent(0, "play", cards, combo_type=2, next_player=3), declaration))
    view = projection(state)
    assert view.history.actions[0].declaration.value == declaration
    assert view.current_winning.value.declaration.value == declaration


def test_invalid_declaration_shapes_become_state_errors_and_are_atomic():
    state = started()
    before = projection(state)
    invalid_single = PublicDeclaration(
        "SINGLE", (14,), 14, None, 2, HAND[:2],
        canonical_action_cards=HAND[:2],
    )
    event = DeclaredActionCommitted(
        ActionEvent(0, "play", HAND[:2], combo_type=1, next_player=3), invalid_single)
    with pytest.raises(PublicStateContractError, match="SEMANTIC_EVENT_INVALID"):
        event.validate()
    with pytest.raises(StateError, match="declared action semantic invalid"):
        state.apply_declared_action(event)
    assert projection(state) == before
    assert state.snapshot().state_version == before.identity.state_version


@pytest.mark.parametrize("assigned_rank,assigned_suit", [(14, "HEARTS"), (8, "JOKER")])
def test_invalid_wild_assignment_rank_or_joker_suit_is_atomic(assigned_rank, assigned_suit):
    wild = encode_card(2, 5)
    hand = (wild, *HAND[1:])
    state = started(hand)
    before = projection(state)
    declaration = PublicDeclaration(
        "SINGLE", (8,), 8, None, 1, (wild,),
        (WildAssignment(wild, assigned_rank, assigned_suit, True),),
        canonical_action_cards=(wild,),
    )
    event = DeclaredActionCommitted(
        ActionEvent(0, "play", (wild,), combo_type=1, next_player=3), declaration)
    with pytest.raises(StateError, match="declared action semantic invalid"):
        state.apply_declared_action(event)
    assert projection(state) == before

def exchange_state():
    state = dealt()
    state.apply_own_seat_candidate(OwnSeatCandidateEvent(0, True, HAND, meta=META))
    state.apply_phase_observed(PhaseObserved("TRIBUTE", 0, META))
    return state


def test_explicit_exchange_is_public_self_only_and_selection_is_not_a_commit():
    state = exchange_state()
    old_hand = set(state.hand)
    state.apply_own_exchange_selection(OwnExchangeSelectionObserved(0, "TRIBUTE", (HAND[0],), META))
    selected = projection(state)
    assert selected.exchange.self_selection.value == (HAND[0],)
    assert selected.history.observed_action_count == 0
    assert state.hand == old_hand
    with pytest.raises(StateError, match="another observer"):
        state.apply_own_exchange_selection(OwnExchangeSelectionObserved(1, "TRIBUTE", (HAND[0],), META))
    assert projection(state) == selected
    transfer = ExchangeTransfer("tribute", 0, 1, HAND[0])
    state.apply_exchange_resolved(ExchangeResolvedObserved(
        (transfer,), tribute_donors=(0,), tribute_order=(0,), anti_tribute=False,
        return_obligations=((1, 0),), meta=META,
    ))
    view = projection(state)
    assert view.remaining_counts.value == (26, 28, 27, 27)
    assert view.exchange.anti_tribute.value is False
    assert view.exchange.resolved_transfers.value == (transfer,)
    assert view.history.actions[0].phase == "TRIBUTE"
    assert view.history.actions[0].kind == "tribute"
    assert view.history.actions[0].declaration.status is KS.ABSENT
    assert view.exchange.self_selection.status is KS.KNOWN
    assert view.exchange.self_selection.value == (HAND[0],)
    assert HAND[0] not in state.hand
    with pytest.raises(StateError, match="resolved exchange counts"):
        state.apply_start(StartEvent(0))
    state.apply_phase_observed(PhaseObserved("RETURN", 1, META))
    state.apply_exchange_resolved(ExchangeResolvedObserved(
        (ExchangeTransfer("return", 1, 0, DECK[27]),), meta=META,
    ))
    state.apply_start(StartEvent(1))
    state.apply_action(ActionEvent(1, "play", (HAND[0],), combo_type=1, next_player=0))
    state.apply_action(ActionEvent(0, "play", (DECK[27],), combo_type=1, next_player=3))
    assert state.my_seat == 0  # ownership follows resolved transfers, not the old deal
    assert projection(state).history.observed_action_count == 4
    assert len(state.history) == 2
    assert CanonicalGuandanPublicState.from_dict(projection(state).to_dict()) == projection(state)


def test_exchange_failed_batch_is_atomic_and_false_empty_are_explicit_facts():
    state = exchange_state()
    old = projection(state)
    with pytest.raises(StateError):
        state.apply_exchange_resolved(ExchangeResolvedObserved((
            ExchangeTransfer("tribute", 0, 1, HAND[0]),
            ExchangeTransfer("tribute", 0, 1, DECK[27]),
        ), meta=META))
    assert projection(state) == old
    state.apply_exchange_resolved(ExchangeResolvedObserved(
        (), tribute_donors=(), tribute_order=(), anti_tribute=True,
        return_obligations=(), meta=META,
    ))
    view = projection(state)
    assert view.exchange.resolved_transfers.value == ()
    assert view.exchange.tribute_donors.value == ()
    assert view.exchange.anti_tribute.value is True
    assert view.exchange.self_selection.status is KS.UNSUPPORTED




def test_exchange_rejects_duplicate_card_within_one_event_atomically():
    state = exchange_state()
    before = projection(state)
    card = HAND[0]
    event = ExchangeResolvedObserved((
        ExchangeTransfer("tribute", 0, 1, card),
        ExchangeTransfer("tribute", 1, 2, card),
    ), tribute_donors=(0, 1), tribute_order=(0, 1), meta=META)
    with pytest.raises(StateError, match="resolved exchange semantic invalid"):
        state.apply_exchange_resolved(event)
    assert projection(state) == before
    assert state._exchange_card_locations == {}


def test_exchange_rejects_same_card_repeated_across_same_phase_atomically():
    state = exchange_state()
    card = HAND[0]
    state.apply_exchange_resolved(ExchangeResolvedObserved(
        (ExchangeTransfer("tribute", 0, 1, card),),
        tribute_donors=(0, 1), tribute_order=(0, 1), meta=META,
    ))
    before = projection(state)
    with pytest.raises(StateError, match="same phase"):
        state.apply_exchange_resolved(ExchangeResolvedObserved(
            (ExchangeTransfer("tribute", 1, 2, card),), meta=META,
        ))
    assert projection(state) == before
    assert state._exchange_card_locations == {card: 1}


def test_exchange_known_donor_rejects_non_donor_source_atomically():
    state = exchange_state()
    before = projection(state)
    event = ExchangeResolvedObserved(
        (ExchangeTransfer("tribute", 1, 2, DECK[27]),),
        tribute_donors=(0,), tribute_order=(0,), meta=META,
    )
    with pytest.raises(StateError, match="resolved exchange semantic invalid"):
        state.apply_exchange_resolved(event)
    assert projection(state) == before
    assert state._exchange_card_locations == {}


def test_exchange_late_donor_fact_must_match_prior_public_transfer():
    state = exchange_state()
    state.apply_exchange_resolved(ExchangeResolvedObserved(
        (ExchangeTransfer("tribute", 0, 1, HAND[0]),), meta=META,
    ))
    before = projection(state)
    with pytest.raises(StateError, match="known donor"):
        state.apply_exchange_resolved(ExchangeResolvedObserved(
            (), tribute_donors=(2,), tribute_order=(2,), meta=META,
        ))
    assert projection(state) == before
    assert state._exchange_card_locations == {HAND[0]: 1}


def test_exchange_known_return_obligation_rejects_wrong_pair_atomically():
    state = exchange_state()
    state.apply_phase_observed(PhaseObserved("RETURN", 1, META))
    before = projection(state)
    event = ExchangeResolvedObserved(
        (ExchangeTransfer("return", 1, 2, DECK[27]),),
        return_obligations=((1, 0),), meta=META,
    )
    with pytest.raises(StateError, match="resolved exchange semantic invalid"):
        state.apply_exchange_resolved(event)
    assert projection(state) == before
    assert state._exchange_card_locations == {}


def _known_exchange(value):
    return Knowledge.known(value, EvidenceRef("synthetic"))


def _dto_exchange(*, donors=None, order=None, anti=None, obligations=None, transfers=()):
    return ExchangeState(
        _known_exchange(donors) if donors is not None else Knowledge.unsupported("donors_unobserved"),
        _known_exchange(order) if order is not None else Knowledge.unsupported("order_unobserved"),
        _known_exchange(anti) if anti is not None else Knowledge.unsupported("anti_unobserved"),
        _known_exchange(obligations) if obligations is not None else Knowledge.unsupported("obligations_unobserved"),
        _known_exchange(transfers),
        Knowledge.unsupported("selection_unobserved"),
    )


def test_exchange_dto_rejects_known_donor_mismatch():
    exchange = _dto_exchange(
        donors=(0,), order=(0,),
        transfers=(ExchangeTransfer("tribute", 1, 2, DECK[27]),),
    )
    with pytest.raises(PublicStateContractError):
        exchange.to_dict()


def test_exchange_dto_rejects_same_kind_duplicate_card():
    card = DECK[27]
    exchange = _dto_exchange(
        transfers=(ExchangeTransfer("tribute", 0, 1, card),
                   ExchangeTransfer("tribute", 1, 2, card)),
    )
    with pytest.raises(PublicStateContractError):
        exchange.to_dict()


def test_exchange_dto_accepts_valid_tribute_return_location_chain():
    card = DECK[27]
    exchange = _dto_exchange(
        donors=(0,), order=(0,), anti=False, obligations=((1, 0),),
        transfers=(ExchangeTransfer("tribute", 0, 1, card),
                   ExchangeTransfer("return", 1, 0, card)),
    )
    encoded = exchange.to_dict()
    assert ExchangeState.from_dict(encoded) == exchange


def test_top_level_dto_rejects_ordinary_card_wildcard_assignment():
    view = projection(started())
    evidence = EvidenceRef("synthetic")
    ordinary = HAND[2]  # not the known level-heart wildcard for level 5
    declaration = PublicDeclaration(
        "SINGLE", (15,), 15, None, 1, (ordinary,),
        (WildAssignment(ordinary, 15, "SPADES", True),),
        canonical_action_cards=(ordinary,),
    )
    raw = view.to_dict()
    raw["current_winning"] = {
        "status": "known",
        "value": {
            "actor": 0,
            "cards": [ordinary],
            "size": 1,
            "declaration": {
                "status": "known",
                "value": declaration.to_dict(),
                "evidence": evidence.to_dict(),
            },
        },
        "evidence": evidence.to_dict(),
    }
    with pytest.raises(PublicStateContractError):
        CanonicalGuandanPublicState.from_dict(raw)


def test_exchange_public_card_location_rejects_conflicting_chain_atomically():
    state = exchange_state()
    card = HAND[0]
    state.apply_exchange_resolved(ExchangeResolvedObserved(
        (ExchangeTransfer("tribute", 0, 1, card),),
        tribute_donors=(0,), tribute_order=(0,), meta=META,
    ))
    state.apply_phase_observed(PhaseObserved("RETURN", 1, META))
    before = projection(state)
    with pytest.raises(StateError, match="public location"):
        state.apply_exchange_resolved(ExchangeResolvedObserved(
            (ExchangeTransfer("return", 2, 0, card),),
            return_obligations=((2, 0),), meta=META,
        ))
    assert projection(state) == before
    assert state._exchange_card_locations == {card: 1}


def test_exchange_public_card_location_allows_prior_target_as_next_source():
    state = exchange_state()
    card = HAND[0]
    state.apply_exchange_resolved(ExchangeResolvedObserved(
        (ExchangeTransfer("tribute", 0, 1, card),),
        tribute_donors=(0,), tribute_order=(0,), meta=META,
    ))
    state.apply_phase_observed(PhaseObserved("RETURN", 1, META))
    state.apply_exchange_resolved(ExchangeResolvedObserved(
        (ExchangeTransfer("return", 1, 0, card),),
        return_obligations=((1, 0),), meta=META,
    ))
    assert state._exchange_card_locations == {card: 0}
    assert card in state.hand
    assert projection(state).remaining_counts.value == (27, 27, 27, 27)

@pytest.mark.parametrize("event", [PhaseObserved("TERMINAL"), PhaseObserved("FOLLOW_PLAY"),
                                   PhaseObserved("LEAD_PLAY")])
def test_phase_cannot_invent_confirmed_play_or_terminal(event):
    state = dealt()
    old = projection(state)
    with pytest.raises(StateError):
        state.apply_phase_observed(event)
    assert projection(state) == old


def test_domain_rejects_missing_presence_and_sequence_zero_is_not_unset():
    state = dealt()
    old = projection(state)
    with pytest.raises(StateError, match="presence"):
        state.apply_start(StartEvent(0, False))
    with pytest.raises(StateError, match="presence"):
        state.apply_own_seat_candidate(OwnSeatCandidateEvent(0, False, HAND))
    assert projection(state) == old
    state = GameState("later")
    state.apply_deal(DealEvent(-1, 5, HAND, meta=replace(META, sequence=1)))
    with pytest.raises(StateError, match="precedes"):
        state.apply_own_seat_candidate(OwnSeatCandidateEvent(0, True, HAND, meta=META))


def terminal_state():
    state = started()
    state.apply_action(ActionEvent(0, "play", HAND, combo_type=1, next_player=3, finish_rank=1))
    state.apply_action(ActionEvent(3, "pass", next_player=2, reset_flag=0))
    state.apply_action(ActionEvent(2, "play", DECK[54:81], combo_type=1, next_player=None, finish_rank=2))
    return state


def settlement(ranks=(1, 3, 2, 4)):
    return SettlementEvent(ranks, ((), DECK[27:54], (), DECK[81:]), meta=META)


def test_settlement_ranks_do_not_mean_everyone_emptied_and_hands_never_serialize():
    state = terminal_state()
    assert projection(state).phase.value == "TERMINAL"
    state.apply_settlement(settlement())
    view = projection(state)
    assert view.active_seat.status is KS.ABSENT
    assert view.emptied_seats.value == (0, 2)
    assert [x.value for x in view.finishing_ranks] == [1, 3, 2, 4]
    assert view.previous_result.status is KS.UNKNOWN
    raw = json.dumps(view.to_dict())
    assert "remaining_hands" not in raw
    assert str(list(DECK[27:54])) not in raw
    assert str(list(DECK[81:])) not in raw
    assert "outcome_class" not in raw  # no current/future result label
    state.apply_settlement(settlement())
    assert projection(state) == view
    candidate = state.settled_result_candidate()
    assert candidate.value.winner_team == 0
    assert candidate.value.outcome_class == "DOUBLE_DOWN"
    assert candidate.value.tribute_cancelled.status is KS.UNSUPPORTED
    with pytest.raises(StateError):
        state.apply_previous_result(PublicResultObserved(candidate.value))
    later = GameState("game-two")
    later.apply_deal(DealEvent(-1, 6, HAND))
    later.apply_previous_result(PublicResultObserved(candidate.value))
    assert projection(later).previous_result.value == candidate.value
    assert CanonicalGuandanPublicState.from_dict(projection(later).to_dict()) == projection(later)


@pytest.mark.parametrize("status", [KS.KNOWN, KS.ABSENT, KS.UNKNOWN, KS.UNSUPPORTED])
def test_knowledge_status_roundtrip(status):
    view = projection(dealt())
    fact = (Knowledge.known(False, EvidenceRef("synthetic")) if status is KS.KNOWN else
            Knowledge.absent(EvidenceRef("synthetic")) if status is KS.ABSENT else
            Knowledge(status, reason="unobserved"))
    view = replace(view, exchange=replace(view.exchange, anti_tribute=fact))
    assert CanonicalGuandanPublicState.from_dict(view.to_dict()) == view


@pytest.mark.parametrize("path", [(), ("identity",), ("phase",), ("own_hand",),
                                  ("own_hand", "evidence"), ("exchange",), ("history",)])
def test_strict_extra_keys_and_safe_errors(path):
    raw = projection(dealt()).to_dict()
    target = raw
    for key in path:
        target = target[key]
    sentinel = "PRIVATE_SENTINEL_SHOULD_NOT_BE_EXPORTED"
    target["opponent_hands"] = sentinel
    with pytest.raises(PublicStateContractError) as caught:
        CanonicalGuandanPublicState.from_dict(raw)
    assert sentinel not in str(caught.value)
    assert sentinel not in repr(caught.value)


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(schema_version=1),
    lambda d: d["identity"].update(public_state_revision=True),
    lambda d: d["phase"].update(value="TRIBUTE"),
    lambda d: d["remaining_counts"].update(value=[27, 27, 29, 27]),
])
def test_invalid_versions_scalars_and_unknown_values_rejected(mutation):
    raw = projection(dealt()).to_dict()
    mutation(raw)
    with pytest.raises(PublicStateContractError):
        CanonicalGuandanPublicState.from_dict(raw)


def test_connection_metadata_is_not_exported_verbatim():
    sentinel = "Authorization: PRIVATE_SENTINEL/raw/capture/path"
    state = GameState("synthetic-game")
    state.apply_deal(DealEvent(-1, 5, HAND, meta=replace(META, connection_id=sentinel)))
    view = projection(state)
    assert sentinel not in repr(view)
    assert sentinel not in json.dumps(view.to_dict())
    assert view.own_hand.evidence.connection_id.startswith("local-")


@pytest.mark.parametrize("outcome", ["HEAD_THIRD", "HEAD_LAST"])
def test_previous_result_outcome_uses_public_ranking_not_counts(outcome):
    state = started()
    state.apply_action(ActionEvent(0, "play", HAND, combo_type=1, next_player=3, finish_rank=1))
    state.apply_action(ActionEvent(3, "play", DECK[81:], combo_type=1, next_player=2, finish_rank=2))
    if outcome == "HEAD_THIRD":
        state.apply_action(ActionEvent(2, "play", DECK[54:81], combo_type=1, next_player=None, finish_rank=3))
        ranks, hands = (1, 4, 3, 2), ((), DECK[27:54], (), ())
    else:
        state.apply_action(ActionEvent(2, "pass", next_player=1, reset_flag=0))
        state.apply_action(ActionEvent(1, "play", DECK[27:54], combo_type=1, next_player=None, finish_rank=3))
        ranks, hands = (1, 3, 4, 2), ((), (), DECK[54:81], ())
    state.apply_settlement(SettlementEvent(ranks, hands))
    summary = state.settled_result_candidate().value
    assert summary.winner_team == 0
    assert summary.outcome_class == outcome
    assert summary.tribute_cancelled.status is KS.UNSUPPORTED


@pytest.mark.parametrize("bad_version", [True, 2.0])
def test_contract_rejects_non_exact_integer_schema_version(bad_version):
    raw = projection(dealt()).to_dict()
    raw["schema_version"] = bad_version
    with pytest.raises(PublicStateContractError):
        CanonicalGuandanPublicState.from_dict(raw)


@pytest.mark.parametrize("bad_size", [True, 1.0])
def test_contract_rejects_non_exact_integer_current_winning_size(bad_size):
    state = started()
    state.apply_action(ActionEvent(0, "play", (HAND[0],), combo_type=1, next_player=3))
    raw = projection(state).to_dict()
    raw["current_winning"]["value"]["size"] = bad_size
    with pytest.raises(PublicStateContractError):
        CanonicalGuandanPublicState.from_dict(raw)


def test_contract_rejects_unsafe_evidence_connection_identifier():
    raw = projection(dealt()).to_dict()
    raw["own_hand"]["evidence"]["connection_id"] = "Authorization: PRIVATE_SENTINEL"
    with pytest.raises(PublicStateContractError) as caught:
        CanonicalGuandanPublicState.from_dict(raw)
    assert "PRIVATE_SENTINEL" not in str(caught.value)


def test_invalid_status_traceback_does_not_contain_untrusted_sentinel():
    sentinel = "PRIVATE_STATUS_SENTINEL"
    raw = projection(dealt()).to_dict()
    raw["phase"] = {"status": sentinel, "reason": "unobserved"}
    try:
        CanonicalGuandanPublicState.from_dict(raw)
    except PublicStateContractError as error:
        rendered = "".join(traceback.format_exception(error))
    else:
        raise AssertionError("invalid knowledge status must be rejected")
    assert sentinel not in rendered


def test_contract_rejects_history_whose_first_action_index_is_not_one():
    state = started()
    state.apply_action(ActionEvent(0, "play", (HAND[0],), combo_type=1, next_player=3))
    raw = projection(state).to_dict()
    raw["history"]["actions"][0]["index"] = 2
    with pytest.raises(PublicStateContractError):
        CanonicalGuandanPublicState.from_dict(raw)


@pytest.mark.parametrize("field", ["public_history_complete", "self_private_history_complete",
                                   "transport_contiguous"])
def test_contract_rejects_complete_true_claim(field):
    raw = projection(dealt()).to_dict()
    raw["history"][field] = {
        "status": "known", "value": True,
        "evidence": EvidenceRef("synthetic").to_dict(),
    }
    with pytest.raises(PublicStateContractError, match="PUBLIC_STATE_UNSUPPORTED"):
        CanonicalGuandanPublicState.from_dict(raw)


def test_contract_rejects_previous_result_sourced_from_current_game():
    view = projection(dealt())
    evidence = EvidenceRef("synthetic")
    result = PreviousResultSummary(
        Knowledge.known(view.identity.game_id, evidence), (1, 3, 2, 4), 0,
        "DOUBLE_DOWN", Knowledge.unsupported("tribute_cancelled_unobserved"),
    )
    raw = view.to_dict()
    raw["previous_result"] = {
        "status": "known", "value": result.to_dict(), "evidence": evidence.to_dict(),
    }
    with pytest.raises(PublicStateContractError):
        CanonicalGuandanPublicState.from_dict(raw)


def test_contract_rejects_untrusted_complete_true_extra_key():
    raw = projection(dealt()).to_dict()
    raw["history"]["complete"] = True
    with pytest.raises(PublicStateContractError):
        CanonicalGuandanPublicState.from_dict(raw)


@pytest.mark.parametrize("location", ["source", "derivation", "reason"])
def test_contract_rejects_credential_shaped_provenance_and_reason_safely(location):
    sentinel = "Authorization: Bearer PRIVATE_CREDENTIAL_SENTINEL"
    raw = projection(dealt()).to_dict()
    if location == "source":
        raw["own_hand"]["evidence"]["source"] = sentinel
    elif location == "derivation":
        raw["own_hand"]["evidence"]["derivation"] = sentinel
    else:
        raw["phase"] = {"status": "unknown", "reason": sentinel}
    try:
        CanonicalGuandanPublicState.from_dict(raw)
    except PublicStateContractError as error:
        rendered = "".join(traceback.format_exception(error))
        assert sentinel not in str(error)
        assert sentinel not in repr(error)
        assert sentinel not in rendered
    else:
        raise AssertionError("credential-shaped provenance must be rejected")
    # Failed validation must not produce a serialized contract payload.
    assert sentinel not in json.dumps(projection(dealt()).to_dict())


@pytest.mark.parametrize("field", ["source", "derivation"])
def test_evidence_codes_reject_non_lowercase_identifier_format(field):
    raw = EvidenceRef("synthetic", derivation="safe_derivation").to_dict()
    raw[field] = "Authorization_Bearer_SECRET"
    with pytest.raises(PublicStateContractError) as caught:
        EvidenceRef.from_dict(raw)
    assert "SECRET" not in "".join(traceback.format_exception(caught.value))


def test_reason_code_rejects_non_lowercase_identifier_format():
    sentinel = "Cookie_SECRET_Value"
    with pytest.raises(PublicStateContractError) as caught:
        Knowledge.unknown(sentinel)
    assert sentinel not in "".join(traceback.format_exception(caught.value))


def test_exchange_dto_rejects_donor_order_set_mismatch():
    exchange = _dto_exchange(donors=(0,), order=(1,), transfers=())
    with pytest.raises(PublicStateContractError):
        exchange.to_dict()


def test_exchange_dto_accepts_matching_donor_order_sets():
    exchange = _dto_exchange(donors=(0, 2), order=(2, 0), transfers=())
    assert ExchangeState.from_dict(exchange.to_dict()) == exchange


def test_state_dto_rejects_current_winner_actor_mismatch():
    state = started()
    state.apply_action(ActionEvent(0, "play", (HAND[0],), combo_type=1, next_player=3))
    raw = projection(state).to_dict()
    assert raw["current_winning"]["value"]["actor"] == 0
    raw["current_winner_seat"]["value"] = 1
    with pytest.raises(PublicStateContractError):
        CanonicalGuandanPublicState.from_dict(raw)


def test_state_dto_rejects_terminal_with_known_active_seat():
    raw = projection(terminal_state()).to_dict()
    assert raw["phase"]["value"] == "TERMINAL"
    raw["active_seat"] = {
        "status": "known", "value": 0,
        "evidence": raw["phase"]["evidence"],
    }
    with pytest.raises(PublicStateContractError):
        CanonicalGuandanPublicState.from_dict(raw)


def test_state_dto_rejects_lead_phase_with_current_winning():
    raw = projection(started()).to_dict()
    evidence = EvidenceRef("synthetic")
    winning = CurrentWinning(
        actor=1, cards=(DECK[27],),
        declaration=Knowledge.unsupported("live_declaration_unverified"),
    )
    raw["current_winning"] = {
        "status": "known", "value": winning.to_dict(), "evidence": evidence.to_dict(),
    }
    raw["current_winner_seat"] = {
        "status": "known", "value": 1, "evidence": evidence.to_dict(),
    }
    with pytest.raises(PublicStateContractError):
        CanonicalGuandanPublicState.from_dict(raw)


def test_state_dto_rejects_follow_phase_without_current_winning():
    raw = projection(started()).to_dict()
    raw["phase"]["value"] = "FOLLOW_PLAY"
    assert raw["current_winning"]["status"] == "absent"
    assert raw["current_winner_seat"]["status"] == "absent"
    with pytest.raises(PublicStateContractError):
        CanonicalGuandanPublicState.from_dict(raw)


def test_state_dto_rejects_active_seat_that_is_publicly_emptied():
    state = dealt()
    state.apply_start(StartEvent(1))
    state.confirm_my_seat_relative("left")  # observer 0, active/leader 1
    raw = projection(state).to_dict()
    assert raw["observer_seat"]["value"] == 0
    assert raw["active_seat"]["value"] == 1
    raw["remaining_counts"]["value"][1] = 0
    raw["emptied_seats"]["value"] = [1]
    with pytest.raises(PublicStateContractError):
        CanonicalGuandanPublicState.from_dict(raw)


def test_state_dto_valid_lead_follow_and_terminal_roundtrip():
    lead = projection(started())

    follow_state = started()
    follow_state.apply_action(ActionEvent(
        0, "play", (HAND[0],), combo_type=1, next_player=3,
    ))
    follow = projection(follow_state)

    terminal = projection(terminal_state())
    assert lead.phase.value == "LEAD_PLAY"
    assert follow.phase.value == "FOLLOW_PLAY"
    assert terminal.phase.value == "TERMINAL"
    for view in (lead, follow, terminal):
        assert CanonicalGuandanPublicState.from_dict(view.to_dict()) == view


@pytest.mark.parametrize("phase", ["TRIBUTE", "RETURN"])
def test_exchange_phase_dto_rejects_known_current_winning_and_winner(phase):
    state = exchange_state()
    if phase == "RETURN":
        state.apply_phase_observed(PhaseObserved("RETURN", 1, META))
    raw = projection(state).to_dict()
    evidence = EvidenceRef("synthetic")
    winning = CurrentWinning(
        actor=1, cards=(DECK[27],),
        declaration=Knowledge.unsupported("live_declaration_unverified"),
    )
    raw["current_winning"] = {
        "status": "known", "value": winning.to_dict(), "evidence": evidence.to_dict(),
    }
    raw["current_winner_seat"] = {
        "status": "known", "value": 1, "evidence": evidence.to_dict(),
    }
    with pytest.raises(PublicStateContractError):
        CanonicalGuandanPublicState.from_dict(raw)


@pytest.mark.parametrize("phase", ["TRIBUTE", "RETURN"])
def test_exchange_phase_projection_roundtrips_with_unknown_winning_facts(phase):
    state = exchange_state()
    if phase == "RETURN":
        state.apply_phase_observed(PhaseObserved("RETURN", 1, META))
    view = projection(state)
    assert view.phase.value == phase
    assert view.current_winning.status is KS.UNKNOWN
    assert view.current_winner_seat.status is KS.UNKNOWN
    assert CanonicalGuandanPublicState.from_dict(view.to_dict()) == view


# Declaration repair matrix: preserve every malformed shape rejection.
# Positive complex declarations below now use the fixed authoritative handoff.
def _card(suit, rank, copy=0):
    return encode_card(suit, rank, copy)


def _raw_declaration(declaration):
    return fixture_declaration_raw(declaration)


def _complex_invalid_declarations():
    wildcard = _card(2, 5)
    return (
        ("straight_nonconsecutive_after_wildcard", PublicDeclaration(
            "STRAIGHT", (3, 4, 6, 7, 8), 8, None, 5,
            (_card(1, 3), _card(1, 4), wildcard, _card(1, 6), _card(1, 8)),
            (WildAssignment(wildcard, 7, "CLUBS", True),),
            canonical_action_cards=(_card(1, 3), _card(1, 4), wildcard, _card(1, 6), _card(1, 8)),
        )),
        ("straight_flush_wrong_suit_metadata", PublicDeclaration(
            "STRAIGHT_FLUSH", (3, 4, 5, 6, 7), 7, "HEARTS", 5,
            tuple(_card(1, rank) for rank in range(3, 8)),
            canonical_action_cards=tuple(_card(1, rank) for rank in range(3, 8)),
        )),
        ("full_house_four_plus_one", PublicDeclaration(
            "FULL_HOUSE", (3, 4), 3, None, 5,
            (_card(1, 3, 0), _card(1, 3, 1), _card(2, 3, 0),
             _card(2, 3, 1), _card(1, 4, 0)),
            canonical_action_cards=(_card(1, 3, 0), _card(1, 3, 1), _card(2, 3, 0),
             _card(2, 3, 1), _card(1, 4, 0)),
        )),
        ("pair_sequence_not_all_pairs", PublicDeclaration(
            "PAIR_SEQUENCE", (3, 4, 5, 6), 6, None, 6,
            (_card(1, 3, 0), _card(1, 3, 1), _card(2, 4, 0),
             _card(2, 4, 1), _card(3, 5, 0), _card(4, 6, 0)),
            canonical_action_cards=(_card(1, 3, 0), _card(1, 3, 1), _card(2, 4, 0),
             _card(2, 4, 1), _card(3, 5, 0), _card(4, 6, 0)),
        )),
        ("triple_sequence_not_all_triples", PublicDeclaration(
            "TRIPLE_SEQUENCE", (3, 4, 5), 5, None, 6,
            (_card(1, 3, 0), _card(1, 3, 1), _card(2, 3, 0),
             _card(1, 4, 0), _card(1, 4, 1), _card(2, 5, 0)),
            canonical_action_cards=(_card(1, 3, 0), _card(1, 3, 1), _card(2, 3, 0),
             _card(1, 4, 0), _card(1, 4, 1), _card(2, 5, 0)),
        )),
        ("four_kings_contains_ordinary_card", PublicDeclaration(
            "FOUR_KINGS", (13, 14, 15), 15, None, 4,
            (_card(0, 15, 1), _card(0, 14, 0), _card(1, 13, 0),
             _card(0, 15, 0)),
            canonical_action_cards=(_card(0, 15, 1), _card(0, 14, 0), _card(1, 13, 0),
             _card(0, 15, 0)),
        )),
    )


def _complex_necessary_shape_declarations():
    families = ("STRAIGHT", "STRAIGHT_FLUSH", "FULL_HOUSE",
                "PAIR_SEQUENCE", "TRIPLE_SEQUENCE", "FOUR_KINGS")
    return tuple((family, fixture_positive_declaration(next(
        vector for vector in DECLARATION_POSITIVE_VECTORS
        if vector["family"] == family))) for family in families)


@pytest.mark.parametrize("case_id,declaration", _complex_invalid_declarations(),
                         ids=lambda value: value if isinstance(value, str) else None)
def test_complex_invalid_declarations_fail_all_dto_boundaries(case_id, declaration):
    del case_id
    for operation in (declaration.validate, declaration.to_dict):
        with pytest.raises(PublicStateContractError) as caught:
            operation()
        assert caught.value.code == "PUBLIC_STATE_INVALID"
    # This raw decoder negative intentionally does not call invalid to_dict().
    raw = _raw_declaration(declaration)
    with pytest.raises(PublicStateContractError) as caught:
        PublicDeclaration.from_dict(raw)
    assert caught.value.code == "PUBLIC_STATE_INVALID"


@pytest.mark.parametrize("mutation", [
    pytest.param(lambda raw: raw.update(family="Authorization: Bearer DECL_SENTINEL"),
                 id="invalid-family-sentinel"),
    pytest.param(lambda raw: raw.update(declared_ranks={"rank": 3}),
                 id="wrong-ranks-container"),
    pytest.param(lambda raw: raw.update(comparison_rank=True),
                 id="wrong-comparison-type"),
    pytest.param(lambda raw: raw.update(canonical_cards="not-a-card-container"),
                 id="wrong-cards-container"),
    pytest.param(lambda raw: raw.update(wild_assignments=["not-an-assignment"]),
                 id="wrong-wild-item-type"),
    pytest.param(lambda raw: raw.update({"Authorization: Bearer EXTRA_SENTINEL": True}),
                 id="extra-field"),
])
def test_declaration_raw_decoder_rejects_types_containers_extra_keys_safely(mutation):
    # Raw input is constructed directly; no invalid DTO serializer is called.
    declaration = PublicDeclaration(
        "SINGLE", (3,), 3, None, 1, (_card(1, 3),),
        canonical_action_cards=(_card(1, 3),),
    )
    raw = _raw_declaration(declaration)
    # Start with the current schema so each mutation, not missing metadata,
    # is what causes the bounded rejection.
    assert PublicDeclaration.from_dict(raw) == declaration
    mutation(raw)
    try:
        PublicDeclaration.from_dict(raw)
    except PublicStateContractError as error:
        rendered = "".join(traceback.format_exception(error))
        assert "DECL_SENTINEL" not in rendered
        assert "EXTRA_SENTINEL" not in rendered
        assert error.__cause__ is None
    else:
        raise AssertionError("malformed raw declaration must be rejected")


def _hand_containing(cards):
    result = list(dict.fromkeys(cards))
    result.extend(card for card in DECK if card not in result)
    return tuple(result[:27])


def _declaration_state_fingerprint(state):
    return {
        "snapshot": state.snapshot(),
        "hand": frozenset(state.hand),
        "remaining": tuple(state.remaining),
        "legacy_history": tuple(state.history),
        "v2_history": tuple(state._v2_history),
        "winning": state._v2_winning,
        "state_version": state.state_version,
        "public_state_revision": state.public_state_revision,
        "projection": projection(state),
    }


@pytest.mark.parametrize("case_id,declaration", _complex_invalid_declarations(),
                         ids=lambda value: value if isinstance(value, str) else None)
def test_complex_invalid_semantic_events_are_atomically_rejected(case_id, declaration):
    del case_id
    state = started(_hand_containing(declaration.canonical_cards))
    event = DeclaredActionCommitted(
        ActionEvent(0, "play", declaration.canonical_cards, combo_type=901,
                    next_player=3, remaining_after=27 - len(declaration.canonical_cards)),
        declaration,
    )
    before = _declaration_state_fingerprint(state)
    with pytest.raises(PublicStateContractError, match="SEMANTIC_EVENT_INVALID") as caught:
        event.validate()
    assert caught.value.__cause__ is None
    assert _declaration_state_fingerprint(state) == before
    with pytest.raises(StateError, match="declared action semantic invalid") as caught:
        state.apply_declared_action(event)
    assert caught.value.__cause__ is None
    assert _declaration_state_fingerprint(state) == before


@pytest.mark.parametrize("family,declaration", [
    ("SINGLE", PublicDeclaration(
        "SINGLE", (3,), 3, None, 1, (_card(1, 3),),
        canonical_action_cards=(_card(1, 3),),
    )),
    ("PAIR", PublicDeclaration(
        "PAIR", (3,), 3, None, 2, (_card(1, 3, 1), _card(1, 3, 0)),
        canonical_action_cards=(_card(1, 3, 1), _card(1, 3, 0)),
    )),
    ("TRIPLE", PublicDeclaration(
        "TRIPLE", (3,), 3, None, 3,
        (_card(2, 3, 0), _card(1, 3, 1), _card(1, 3, 0)),
        canonical_action_cards=(_card(2, 3, 0), _card(1, 3, 1), _card(1, 3, 0)),
    )),
    ("RANK_BOMB", PublicDeclaration(
        "RANK_BOMB", (3,), 3, None, 4,
        (_card(2, 3, 1), _card(1, 3, 0), _card(2, 3, 0), _card(1, 3, 1)),
        canonical_action_cards=(_card(2, 3, 1), _card(1, 3, 0), _card(2, 3, 0), _card(1, 3, 1)),
    )),
])
def test_evidence_backed_simple_declaration_families_roundtrip(family, declaration):
    assert declaration.family == family
    original_order = declaration.canonical_cards
    explicit_action_order = declaration.canonical_action_cards
    assert explicit_action_order == original_order and explicit_action_order
    declaration.validate()
    declaration.validate_for_level(5)
    encoded = declaration.to_dict()
    assert encoded["canonical_action_cards"] == list(explicit_action_order)
    decoded = PublicDeclaration.from_dict(encoded)
    assert decoded == declaration
    assert decoded.canonical_cards == original_order
    assert decoded.canonical_action_cards == explicit_action_order

    state = started(_hand_containing(original_order))
    observed_wire_order = tuple(reversed(original_order))
    state.apply_declared_action(DeclaredActionCommitted(
        ActionEvent(0, "play", observed_wire_order, combo_type=1905, next_player=3,
                    remaining_after=27 - len(observed_wire_order)),
        decoded,
    ))
    view = projection(state)
    assert view.history.actions[0].declaration.status is KS.KNOWN
    assert view.current_winning.value.declaration.status is KS.KNOWN
    assert view.history.actions[0].declaration.value == declaration
    assert view.current_winning.value.declaration.value == declaration
    assert state.history[0].cards == observed_wire_order
    assert state.history[0].combo_type == 1905
    assert CanonicalGuandanPublicState.from_dict(view.to_dict()) == view

    # An explicit valid simple-family order must not restore the removed
    # constructor fallback for an empty order.
    empty = replace(declaration, canonical_action_cards=())
    assert empty.canonical_action_cards == ()
    with pytest.raises(PublicStateContractError, match="^PUBLIC_STATE_INVALID$"):
        empty.validate()
    raw = _raw_declaration(empty)
    with pytest.raises(PublicStateContractError, match="^PUBLIC_STATE_INVALID$"):
        PublicDeclaration.from_dict(raw)


@pytest.mark.parametrize("family,declaration", _complex_necessary_shape_declarations(),
                         ids=lambda value: value if isinstance(value, str) else None)
def test_complex_necessary_shape_stays_unsupported_without_exact_metadata_evidence(
    family, declaration,
):
    # Historical test name retained: the authoritative handoff now supplies
    # the exact metadata that was previously missing, so these are known.
    assert declaration.family == family
    original_order = declaration.canonical_cards
    declaration.validate()
    decoded = PublicDeclaration.from_dict(declaration.to_dict())
    assert decoded == declaration
    assert decoded.canonical_cards == original_order


def test_live_play_stays_declaration_unsupported_pass_absent_and_v1_wire_unchanged():
    state = started()
    sentinel = "Authorization: Bearer LIVE_DECLARATION_SENTINEL"
    wire_order = (HAND[1], HAND[0])
    state.apply_action(ActionEvent(
        0, "play", wire_order, combo_type=711, next_player=3, remaining_after=25,
        meta=replace(META, sequence=10, connection_id=sentinel),
    ))
    play_view = projection(state)
    assert state.history[0].cards == wire_order
    assert state.history[0].combo_type == 711
    assert play_view.history.actions[0].cards == wire_order
    assert play_view.history.actions[0].declaration.status is KS.UNSUPPORTED
    assert play_view.current_winning.value.declaration.status is KS.UNSUPPORTED

    state.apply_action(ActionEvent(
        3, "pass", next_player=2, reset_flag=0,
        meta=replace(META, sequence=11, connection_id=sentinel),
    ))
    pass_view = projection(state)
    assert state.history[1].kind == "pass"
    assert state.history[1].cards == ()
    assert state.history[1].combo_type is None
    assert pass_view.history.actions[1].declaration.status is KS.ABSENT
    assert sentinel not in json.dumps(pass_view.to_dict())


_NON_STRING_ASSIGNED_SUITS = (
    pytest.param([], id="assigned-suit-list"),
    pytest.param({}, id="assigned-suit-dict"),
)


def _non_string_suit_declaration(assigned_suit):
    card = HAND[0]
    rank = wire_rank_to_guandan_rank(card & 15)
    return PublicDeclaration(
        "SINGLE", (rank,), rank, None, 1, (card,),
        (WildAssignment(card, rank, assigned_suit, True),),
        canonical_action_cards=(card,),
    )


def _assert_safe_bounded_exception(error, expected_code, unsafe_input):
    assert error.args == (expected_code,)
    assert str(error) == expected_code
    assert repr(error) == f"{type(error).__name__}({expected_code!r})"
    assert error.__cause__ is None
    rendered = "".join(traceback.format_exception(error))
    assert repr(unsafe_input) not in str(error)
    assert repr(unsafe_input) not in repr(error)
    assert repr(unsafe_input) not in rendered
    assert "Authorization" not in rendered
    assert "Cookie" not in rendered


@pytest.mark.parametrize("assigned_suit", _NON_STRING_ASSIGNED_SUITS)
def test_non_string_assigned_suit_rejected_by_declaration_dto_boundaries(assigned_suit):
    declaration = _non_string_suit_declaration(assigned_suit)
    for operation in (declaration.validate, declaration.to_dict):
        with pytest.raises(PublicStateContractError) as caught:
            operation()
        _assert_safe_bounded_exception(caught.value, "PUBLIC_STATE_INVALID", assigned_suit)
    # Decoder input is assembled manually; invalid declaration.to_dict() is not used.
    raw = _raw_declaration(declaration)
    assert raw["wild_assignments"][0]["assigned_suit"] is assigned_suit
    with pytest.raises(PublicStateContractError) as caught:
        PublicDeclaration.from_dict(raw)
    _assert_safe_bounded_exception(caught.value, "PUBLIC_STATE_INVALID", assigned_suit)


@pytest.mark.parametrize("assigned_suit", _NON_STRING_ASSIGNED_SUITS)
def test_non_string_assigned_suit_semantic_and_state_rejection_is_atomic(assigned_suit):
    declaration = _non_string_suit_declaration(assigned_suit)
    event = DeclaredActionCommitted(
        ActionEvent(0, "play", declaration.canonical_cards, combo_type=902,
                    next_player=3, remaining_after=26),
        declaration,
    )
    state = started()
    before = _declaration_state_fingerprint(state)
    with pytest.raises(PublicStateContractError) as caught:
        event.validate()
    _assert_safe_bounded_exception(caught.value, "SEMANTIC_EVENT_INVALID", assigned_suit)
    assert _declaration_state_fingerprint(state) == before

    with pytest.raises(StateError) as caught:
        state.apply_declared_action(event)
    _assert_safe_bounded_exception(caught.value, "declared action semantic invalid", assigned_suit)
    assert _declaration_state_fingerprint(state) == before


@pytest.mark.parametrize("assigned_suit", _NON_STRING_ASSIGNED_SUITS)
@pytest.mark.parametrize("path", ["history-only", "current-winning-only"])
def test_non_string_assigned_suit_rejected_through_top_level_raw_paths(
    assigned_suit, path,
):
    state = started()
    card = HAND[0]
    state.apply_action(ActionEvent(
        0, "play", (card,), combo_type=1, next_player=3, remaining_after=26,
    ))
    raw = projection(state).to_dict()
    invalid_declaration = _raw_declaration(_non_string_suit_declaration(assigned_suit))
    known_invalid = {
        "status": "known",
        "value": invalid_declaration,
        "evidence": EvidenceRef("synthetic").to_dict(),
    }
    if path == "history-only":
        assert raw["history"]["actions"][0]["declaration"]["status"] == "unsupported"
        assert raw["current_winning"]["value"]["declaration"]["status"] == "unsupported"
        raw["history"]["actions"][0]["declaration"] = known_invalid
    else:
        assert raw["history"]["actions"][0]["declaration"]["status"] == "unsupported"
        raw["current_winning"]["value"]["declaration"] = known_invalid
    with pytest.raises(PublicStateContractError) as caught:
        CanonicalGuandanPublicState.from_dict(raw)
    _assert_safe_bounded_exception(caught.value, "PUBLIC_STATE_INVALID", assigned_suit)


def test_post_exchange_3019_can_only_corroborate_the_confirmed_observer():
    state = exchange_state()
    transfer = ExchangeTransfer("tribute", 0, 1, HAND[0])
    state.apply_exchange_resolved(ExchangeResolvedObserved(
        (transfer,), tribute_donors=(0,), tribute_order=(0,), anti_tribute=False,
        return_obligations=((1, 0),), meta=META,
    ))
    updated_private_hand = tuple(card for card in HAND if card != HAND[0])
    assert state.apply_own_seat_candidate(OwnSeatCandidateEvent(
        0, True, updated_private_hand, meta=META,
    )) is False
    assert state.my_seat == 0
    assert state.apply_own_seat_candidate(OwnSeatCandidateEvent(
        2, True, updated_private_hand, meta=META,
    )) is False
    assert state.my_seat == 0
