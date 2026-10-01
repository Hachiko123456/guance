from __future__ import annotations

import pytest

from guance.domain.card_counter import count_visible_cards
from guance.domain.cards import card_label, encode_card, full_deck, validate_deck
from guance.domain.events import (
    ActionEvent, DealEvent, OwnSeatCandidateEvent, SettlementEvent, SourceMeta, StartEvent,
)
from guance.domain.state import GameState, SeatConfirmationConflict, StateError


def test_card_codec_covers_two_deck_physical_cards() -> None:
    deck = full_deck()
    validate_deck(deck)
    assert len(deck) == len(set(deck)) == 108
    assert card_label(encode_card(2, 2, 0)) == "H2"
    assert card_label(encode_card(0, 14, 0)) == "JOKER_SMALL"


def test_state_advances_only_on_confirmed_actions() -> None:
    state = GameState("fixture")
    hand = tuple(full_deck()[:27])
    state.apply_deal(DealEvent(my_seat=-1, level=5, hand=hand))
    state.apply_start(StartEvent(first_seat=2))
    state.apply_action(ActionEvent(player=2, kind="play", cards=(hand[0],),
                                   combo_type=1, next_player=1, remaining_after=26,
                                   action_index=1, expected_trick=0))
    assert state.snapshot().state_version == 3
    assert state.my_seat == 2
    try:
        state.apply_action(ActionEvent(player=3, kind="pass", next_player=0,
                                       action_index=2, expected_trick=0))
    except StateError:
        pass
    else:
        raise AssertionError("actor mismatch must fail closed")
    assert state.snapshot().state_version == 3
    assert state.my_seat == 2


def test_start_rejects_conflicting_deal_first_seat_hint() -> None:
    hand = tuple(full_deck()[:27])
    state = GameState("hint-conflict")
    state.apply_deal(DealEvent(
        my_seat=-1, level=5, hand=hand,
        first_seat_hint=1, first_seat_hint_present=True,
    ))

    with pytest.raises(StateError, match="first-seat hints conflict"):
        state.apply_start(StartEvent(first_seat=2))


def test_own_seat_candidate_confirms_before_start_without_observation() -> None:
    hand = tuple(full_deck()[:27])
    state = GameState("preplay-seat")
    state.apply_deal(DealEvent(my_seat=-1, level=5, hand=hand))

    changed = state.apply_own_seat_candidate(OwnSeatCandidateEvent(
        candidate_seat=2, candidate_seat_present=True, referenced_cards=hand,
    ))

    assert changed is True
    assert state.my_seat == 2
    assert state.phase == "waiting_start"
    assert state.observation("session", 0.0) is None


def test_own_seat_candidate_rejects_stale_connection_or_sequence() -> None:
    hand = tuple(full_deck()[:27])
    state = GameState("preplay-seat")
    state.apply_deal(DealEvent(
        my_seat=-1, level=5, hand=hand,
        meta=SourceMeta(connection_id="current", sequence=10),
    ))

    stale_connection = OwnSeatCandidateEvent(
        candidate_seat=2, candidate_seat_present=True, referenced_cards=hand,
        meta=SourceMeta(connection_id="old", sequence=11),
    )
    with pytest.raises(StateError, match="connection mismatch"):
        state.apply_own_seat_candidate(stale_connection)

    stale_sequence = OwnSeatCandidateEvent(
        candidate_seat=2, candidate_seat_present=True, referenced_cards=hand,
        meta=SourceMeta(connection_id="current", sequence=9),
    )
    with pytest.raises(StateError, match="precedes the deal"):
        state.apply_own_seat_candidate(stale_sequence)


def test_nonmatching_own_seat_candidate_is_ignored_and_conflict_fails_closed() -> None:
    hand = tuple(full_deck()[:27])
    state = GameState("preplay-seat")
    state.apply_deal(DealEvent(my_seat=-1, level=5, hand=hand))

    assert state.apply_own_seat_candidate(OwnSeatCandidateEvent(
        candidate_seat=2, candidate_seat_present=True, referenced_cards=hand[:-1],
    )) is False
    assert state.my_seat == -1

    state.apply_own_seat_candidate(OwnSeatCandidateEvent(
        candidate_seat=2, candidate_seat_present=True, referenced_cards=hand,
    ))
    with pytest.raises(SeatConfirmationConflict):
        state.apply_own_seat_candidate(OwnSeatCandidateEvent(
            candidate_seat=3, candidate_seat_present=True, referenced_cards=hand,
        ))


def _new_unconfirmed_state(first_seat: int) -> tuple[GameState, tuple[int, ...]]:
    hand = tuple(full_deck()[:27])
    state = GameState("seat-inference")
    state.apply_deal(DealEvent(my_seat=-1, level=5, hand=hand))
    state.apply_start(StartEvent(first_seat=first_seat))
    return state, hand


def _settlement_ready_state() -> GameState:
    deck = full_deck()
    state = GameState("settlement")
    state.my_seat = 0
    state.phase = "ended"
    state.remaining = [0, 0, 0, 0]
    state.hand = set()
    state._played_by_seat = [
        list(deck[offset:offset + 27]) for offset in range(0, len(deck), 27)
    ]
    state._played_public = set(deck)
    state._finished = [0, 1, 2, 3]
    return state


def _settlement_event(ranks_by_seat: tuple[int, ...] = (1, 2, 3, 4)) -> SettlementEvent:
    return SettlementEvent(
        ranks_by_seat=ranks_by_seat,
        remaining_hands=((), (), (), ()),
    )


def test_seat_inferred_from_public_play_when_first_seat_is_opponent() -> None:
    state, hand = _new_unconfirmed_state(first_seat=1)

    state.apply_action(ActionEvent(player=1, kind="play", cards=(full_deck()[27],),
                                   combo_type=1, next_player=0, remaining_after=26))
    assert state.my_seat == -1
    assert state.observation("session", 1.0) is None

    state.apply_action(ActionEvent(player=0, kind="play", cards=(hand[0],),
                                   combo_type=1, next_player=3, remaining_after=26))

    assert state.my_seat == 0
    assert hand[0] not in state.hand
    observation = state.observation("session", 2.0)
    assert observation is not None
    assert observation.my_seat == 0


def test_seat_inferred_when_first_seat_is_self() -> None:
    state, hand = _new_unconfirmed_state(first_seat=2)

    state.apply_action(ActionEvent(player=2, kind="play", cards=(hand[0],),
                                   combo_type=1, next_player=1, remaining_after=26))

    assert state.my_seat == 2
    assert state.hand == set(hand[1:])
    assert state.observation("session", 1.0) is not None


def test_multiple_opponents_can_play_before_self_is_confirmed() -> None:
    state, hand = _new_unconfirmed_state(first_seat=0)
    deck = full_deck()

    state.apply_action(ActionEvent(player=0, kind="play", cards=(deck[27],),
                                   combo_type=1, next_player=3, remaining_after=26))
    state.apply_action(ActionEvent(player=3, kind="play", cards=(deck[28],),
                                   combo_type=1, next_player=2, remaining_after=26))
    state.apply_action(ActionEvent(player=2, kind="play", cards=(deck[29],),
                                   combo_type=1, next_player=1, remaining_after=26))
    state.apply_action(ActionEvent(player=1, kind="play", cards=(hand[0],),
                                   combo_type=1, next_player=0, remaining_after=26))

    assert state.my_seat == 1
    assert hand[0] not in state.hand
    assert len(state.history) == 4


def test_second_actor_intersecting_initial_hand_is_a_conflicting_candidate() -> None:
    state, hand = _new_unconfirmed_state(first_seat=0)

    state.apply_action(ActionEvent(player=0, kind="play", cards=(hand[0],),
                                   combo_type=1, next_player=3, remaining_after=26))
    with pytest.raises(StateError, match="conflicting own-seat candidate"):
        state.apply_action(ActionEvent(player=3, kind="play", cards=(hand[1],),
                                       combo_type=1, next_player=2, remaining_after=26))

    # apply_action is transactional: the conflicting action did not enter the
    # history or change the confirmed candidate's remaining hand.
    assert state.my_seat == 0
    assert len(state.history) == 1
    assert hand[1] in state.hand


def test_settlement_is_rejected_without_confirmed_own_seat() -> None:
    state, _ = _new_unconfirmed_state(first_seat=1)
    state.apply_action(ActionEvent(player=1, kind="play", cards=(full_deck()[27],),
                                   combo_type=1, next_player=0, remaining_after=26))

    remaining_hands = (
        tuple(full_deck()[0:27]),
        tuple(full_deck()[28:54]),
        tuple(full_deck()[54:81]),
        tuple(full_deck()[81:108]),
    )
    with pytest.raises(StateError, match="own seat not confirmed before settlement"):
        state.apply_settlement(SettlementEvent(
            ranks_by_seat=(1, 2, 3, 4),
            remaining_hands=remaining_hands,
        ))


def test_settlement_rejection_is_transactional_before_phase_mutation() -> None:
    state = _settlement_ready_state()
    state.phase = "active"
    before_snapshot = state.snapshot()
    before_table = state.table_snapshot("session", 1.0)

    with pytest.raises(StateError, match="settlement before observed game end"):
        state.apply_settlement(_settlement_event())

    assert state.snapshot() == before_snapshot
    assert state.table_snapshot("session", 1.0) == before_table


def test_settlement_duplicate_ranks_are_idempotent_and_conflicts_do_not_replace_them() -> None:
    state = _settlement_ready_state()
    event = _settlement_event()
    state.apply_settlement(event)
    after_first_snapshot = state.snapshot()
    after_first_table = state.table_snapshot("session", 1.0)

    state.apply_settlement(event)
    assert state.snapshot() == after_first_snapshot
    assert state.table_snapshot("session", 1.0) == after_first_table

    with pytest.raises(StateError, match="conflicting settlement ranks"):
        state.apply_settlement(_settlement_event((1, 2, 4, 3)))
    assert state.snapshot() == after_first_snapshot
    assert state.table_snapshot("session", 1.0) == after_first_table


def test_finish_rank_validation_is_independent_and_apply_action_is_atomic() -> None:
    state, hand = _new_unconfirmed_state(first_seat=0)
    before_snapshot = state.snapshot()
    before_table = state.table_snapshot("session", 1.0)

    with pytest.raises(StateError, match="finish rank on non-finishing action"):
        state.apply_action(ActionEvent(
            player=0, kind="play", cards=(hand[0],), combo_type=1,
            next_player=3, remaining_after=26, finish_rank=1,
        ))

    assert state.snapshot() == before_snapshot
    assert state.table_snapshot("session", 1.0) == before_table

    with pytest.raises(StateError, match="invalid finish rank"):
        state.apply_action(ActionEvent(
            player=0, kind="pass", next_player=3, finish_rank=0,
        ))
    assert state.snapshot() == before_snapshot
    assert state.table_snapshot("session", 1.0) == before_table


def test_finishing_action_rank_must_match_expected_order_atomically() -> None:
    state, hand = _new_unconfirmed_state(first_seat=0)
    state.remaining[0] = 1
    before_snapshot = state.snapshot()
    before_table = state.table_snapshot("session", 1.0)

    with pytest.raises(StateError, match="finish-rank flag mismatch"):
        state.apply_action(ActionEvent(
            player=0, kind="play", cards=(hand[0],), combo_type=1,
            next_player=3, remaining_after=0, finish_rank=2,
        ))

    assert state.snapshot() == before_snapshot
    assert state.table_snapshot("session", 1.0) == before_table


def test_finish_rank_is_recorded_and_exposed_without_private_hands() -> None:
    state = GameState("ranked")
    hand = tuple(full_deck()[:27])
    state.apply_deal(DealEvent(my_seat=-1, level=5, hand=hand))
    state.apply_start(StartEvent(first_seat=2))
    # Finish the starting player in one synthetic action for the rank path.
    state.remaining[2] = 1
    state.apply_action(ActionEvent(
        player=2, kind="play", cards=(full_deck()[27],), combo_type=1,
        next_player=1, remaining_after=0, finish_rank=1,
    ))
    table = state.table_snapshot("session", 1.0)
    assert table["finish_ranks_by_seat"] == [-1, -1, 1, -1]
    assert "remaining_hands" not in table


def test_table_snapshot_is_available_after_deal_and_counts_full_deck() -> None:
    state = GameState("table")
    hand = tuple(full_deck()[:27])
    state.apply_deal(DealEvent(my_seat=-1, level=7, hand=hand))

    table = state.table_snapshot("session", 123.5)

    assert table["session_id"] == "session"
    assert table["game_id"] == "table"
    assert table["phase"] == "waiting_start"
    assert table["my_seat"] == -1
    assert table["first_seat"] == -1
    assert table["current_seat"] == -1
    assert table["seat_confirmed"] is False
    assert table["hand"] == sorted(hand)
    assert table["history"] == []
    assert table["finish_ranks_by_seat"] == [-1, -1, -1, -1]
    assert table["counter_unseen_total"] == 108
    assert sum(row["total"] for row in table["card_counter"]) == 108
    assert sum(row["in_hand"] for row in table["card_counter"]) == 27
    assert all(row["played"] == 0 for row in table["card_counter"])


def test_counter_handles_jokers_level_and_deduplicates_physical_codes() -> None:
    small = encode_card(0, 14, 0)
    big = encode_card(0, 15, 1)
    level_card = encode_card(2, 7, 0)
    played_card = encode_card(3, 7, 1)
    rows = count_visible_cards(
        [small, small, big, level_card],
        [played_card, played_card],
        level=7,
    )
    by_rank = {row["rank"]: row for row in rows}

    assert by_rank[7] == {
        "rank": 7, "total": 8, "played": 1, "in_hand": 1,
        "remaining": 7, "unseen": 7, "is_level": True,
    }
    assert by_rank[14]["total"] == 2
    assert by_rank[14]["in_hand"] == 1
    assert by_rank[14]["unseen"] == 2
    assert by_rank[15]["total"] == 2
    assert by_rank[15]["in_hand"] == 1
    assert by_rank[15]["unseen"] == 2
    assert min(row["unseen"] for row in rows) >= 0
    assert sum(row["unseen"] for row in rows) == 107


def test_own_public_play_decrements_public_remaining_once() -> None:
    hand = tuple(full_deck()[:27])
    state = GameState("counter")
    state.apply_deal(DealEvent(my_seat=-1, level=5, hand=hand))
    before = state.table_snapshot("session", 1.0)

    state.apply_start(StartEvent(first_seat=0))
    state.apply_action(ActionEvent(
        player=0, kind="play", cards=(hand[0],), combo_type=1,
        next_player=3, remaining_after=26,
    ))
    after = state.table_snapshot("session", 2.0)

    assert before["counter_unseen_total"] == 108
    assert after["counter_unseen_total"] == 107
    rank_value = hand[0] & 0x0F
    before_rank = next(row for row in before["card_counter"] if row["rank"] == rank_value)
    after_rank = next(row for row in after["card_counter"] if row["rank"] == rank_value)
    assert after_rank["in_hand"] == before_rank["in_hand"] - 1
    assert after_rank["played"] == before_rank["played"] + 1
    assert after_rank["unseen"] == before_rank["unseen"] - 1
