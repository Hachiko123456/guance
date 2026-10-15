from __future__ import annotations

from dataclasses import replace

import pytest

from guance.infrastructure.model.marvel_v2 import (
    ExchangeFacts, MarvelState, PreviousResultFacts, UnsupportedStateError,
    adapt_live_state, encode_channels, encode_channels_batch, encode_observer_channels,
)


def _state(**changes):
    base = MarvelState(
        observer_seat=1, active_seat=1, leader_seat=0, current_winner_seat=3,
        phase="FOLLOW_PLAY", level=1, own_hand=(0, 54), current_winning_cards=(4,),
        self_exchange_cards=(8,), private_prefix=(11, 22), remaining_counts=(27, 2, 26, 25),
        finishing_ranks=(0, 0, 1, 0), emptied_seats=(2,), consecutive_passes=2,
        current_family="SINGLE", current_comparison_rank=4, current_winning_size=1,
        previous_result=PreviousResultFacts((1, 2, 3, 4), 0, "HEAD_THIRD", False),
        exchange=ExchangeFacts((1,), (1,), False, ((3, 1),), (), (8,), True),
        token_step=5, committed_step=2, history_complete=True,
    )
    return replace(base, **changes)


def test_channel_encoder_exact_shape_slices_and_sentinel():
    encoded = encode_channels(_state())
    assert len(encoded) == 512
    assert set(encoded) <= set(range(256))
    assert encoded[0] == encoded[54] == 1
    assert encoded[108 + 4] == 1
    assert encoded[216 + 8] == 1
    assert encoded[324:328] == (11, 22, 0, 0)
    assert encoded[356:360] == (27, 2, 26, 25)
    assert encoded[360:364] == (0, 0, 1, 0)
    assert encoded[365] == encoded[369] == encoded[372] == encoded[379] == 1
    assert encoded[384] == 1
    assert encoded[399] == 1  # ACE level
    assert encoded[403] == 1
    assert encoded[406] == encoded[407] == encoded[410] == 1
    assert encoded[412:416] == (1, 2, 3, 4)
    assert encoded[416] == 1 and encoded[427] == 1 and encoded[442] == 1
    assert encoded[453] == 1 and encoded[456] == 2
    assert encoded[460:473] == tuple((5 >> bit) & 1 for bit in range(13))
    assert encoded[473:486] == tuple((2 >> bit) & 1 for bit in range(13))
    assert encoded[487:501] == (1, 0, 1, 2, 1, 0, 0, 1, 1, 1, 1, 0, 0, 1)
    assert encoded[501:511] == (0,) * 10
    assert encoded[511] == 1


def test_token_step_bits_are_visible_only_to_active_observer():
    active = encode_channels(_state(observer_seat=1))
    inactive = encode_channels(_state(observer_seat=0, own_hand=tuple(range(27)),
                                      remaining_counts=(27, 2, 26, 25)))
    assert any(active[460:473])
    assert inactive[460:473] == (0,) * 13
    assert inactive[473:486] == active[473:486]


def test_missing_exact_history_is_unsupported_not_zero_filled():
    with pytest.raises(UnsupportedStateError, match="exact_observer_history_required"):
        encode_channels(_state(history_complete=False))


@pytest.mark.parametrize("changes", [
    {"private_prefix": tuple(range(33))},
    {"own_hand": (0, 0), "remaining_counts": (27, 2, 26, 25)},
    {"token_step": 1, "committed_step": 2},
    {"current_family": None},
])
def test_invalid_or_ambiguous_channel_state_fails_closed(changes):
    with pytest.raises(ValueError):
        encode_channels(_state(**changes))


def test_current_real_live_contract_is_explicitly_ineligible():
    from guance.domain.cards import full_deck
    from guance.domain.events import DealEvent, StartEvent
    from guance.domain.state import GameState

    domain = GameState("phase3-live-boundary")
    domain.apply_deal(DealEvent(-1, 3, tuple(full_deck())[:27]))
    domain.apply_start(StartEvent(0))
    domain.confirm_my_seat_relative("self")
    live = domain.public_state_v2("phase3-live")
    with pytest.raises(UnsupportedStateError, match="phase3_live_state_not_eligible"):
        adapt_live_state(live)


@pytest.mark.parametrize("ranking,team,outcome", [
    ((1, 2, 3, 4), 0, "HEAD_THIRD"),
    ((1, 4, 2, 3), 0, "DOUBLE_DOWN"),
    ((1, 3, 4, 2), 0, "HEAD_LAST"),
    ((2, 1, 4, 3), 1, "HEAD_THIRD"),
])
def test_previous_result_three_outcomes_encode_without_guessing(ranking, team, outcome):
    state = _state(previous_result=PreviousResultFacts(ranking, team, outcome, False))
    encoded = encode_channels(state)
    assert encoded[407 + team] == 1
    assert encoded[409 + {"DOUBLE_DOWN": 0, "HEAD_THIRD": 1, "HEAD_LAST": 2}[outcome]] == 1
    assert encoded[412:416] == ranking


def test_four_observer_and_batch_channel_shapes_are_exact():
    states = tuple(_state(observer_seat=index, own_hand=(index,),
                          remaining_counts=(1, 1, 1, 1)) for index in range(4))
    observers = encode_observer_channels(states)
    assert len(observers) == 4 and all(len(row) == 512 for row in observers)
    batch = encode_channels_batch(((states,),))
    assert len(batch) == 1 and len(batch[0]) == 1 and len(batch[0][0]) == 4
    assert len(batch[0][0][0]) == 512
    with pytest.raises(UnsupportedStateError, match="exact_four_observers_required"):
        encode_observer_channels(states[:3])


def test_confirmed_absent_previous_result_has_zero_channels_and_presence_flag():
    encoded = encode_channels(_state(previous_result=None))
    assert encoded[407:416] == (0,) * 9
    assert encoded[486] == 0 and encoded[497] == 0
