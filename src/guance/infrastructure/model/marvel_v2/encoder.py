"""Pure-stdlib 512-channel MARVEL V2 public+self encoder."""
from __future__ import annotations

from typing import Iterable

from .contracts import (
    CHANNEL_COUNT, FAMILIES, MarvelState, CardMapping, UnsupportedStateError,
    _reject,
)

_FAMILY_INDEX = {family: index for index, family in enumerate(FAMILIES)}
_RANK_INDEX = {rank: index for index, rank in enumerate(range(3, 18))}
_OUTCOME_INDEX = {"DOUBLE_DOWN": 0, "HEAD_THIRD": 1, "HEAD_LAST": 2}


def _one_hot(values: list[int], start: int, size: int, selected: int | None) -> None:
    if selected is not None:
        if not 0 <= selected < size:
            _reject("one_hot_index_invalid")
        values[start + selected] = 1


def _cards(values: list[int], start: int, cards: Iterable[int]) -> None:
    for card_id in cards:
        if values[start + card_id]:
            _reject("channel_card_duplicate")
        values[start + card_id] = 1


def _bits(values: list[int], start: int, width: int, number: int) -> None:
    for offset in range(width):
        values[start + offset] = (number >> offset) & 1


def encode_channels(state: MarvelState) -> tuple[int, ...]:
    """Encode one observer's complete public+self state into exactly 512 bytes."""
    if type(state) is not MarvelState:
        raise UnsupportedStateError("marvel_state_required")
    state.validate()
    if not state.history_complete:
        raise UnsupportedStateError("exact_observer_history_required")
    result = [0] * CHANNEL_COUNT
    _cards(result, 0, state.own_hand)
    _cards(result, 108, state.current_winning_cards)
    _cards(result, 216, state.self_exchange_cards)
    for index, token in enumerate(state.private_prefix):
        result[324 + index] = token
    result[356:360] = list(state.remaining_counts)
    result[360:364] = list(state.finishing_ranks)
    _one_hot(result, 364, 4, state.observer_seat)
    _one_hot(result, 368, 4, state.active_seat)
    _one_hot(result, 372, 4, state.leader_seat)
    _one_hot(result, 376, 4, state.current_winner_seat)
    for seat in state.emptied_seats:
        result[380 + seat] = 1
    phase_index = {
        "LEAD_PLAY": 0, "FOLLOW_PLAY": 0, "TRIBUTE": 1, "RETURN": 2, "TERMINAL": 3,
    }[state.phase]
    _one_hot(result, 384, 4, phase_index)
    level_rank = CardMapping.level_to_rank(state.level)
    _one_hot(result, 388, 13, level_rank - 3)
    _one_hot(result, 401, 4, state.consecutive_passes)
    _one_hot(result, 405, 2, state.observer_seat % 2)
    if state.previous_result is not None:
        _one_hot(result, 407, 2, state.previous_result.winner_team)
        _one_hot(result, 409, 3, _OUTCOME_INDEX[state.previous_result.outcome_class])
        result[412:416] = list(state.previous_result.ranking_by_seat)
    if state.current_family is not None:
        _one_hot(result, 416, 10, _FAMILY_INDEX[state.current_family])
        assert state.current_comparison_rank is not None
        _one_hot(result, 426, 15, _RANK_INDEX[state.current_comparison_rank])
    _one_hot(result, 441, 11, state.current_winning_size)
    for seat in state.exchange.tribute_donors:
        result[452 + seat] = 1
    for index, seat in enumerate(state.exchange.tribute_order):
        result[456 + index] = seat + 1
    if state.observer_seat == state.active_seat:
        _bits(result, 460, 13, state.token_step)
    _bits(result, 473, 13, state.committed_step)
    result[486] = int(state.previous_result is not None
                      and state.previous_result.tribute_cancelled)
    result[487] = int(state.phase in {"LEAD_PLAY", "FOLLOW_PLAY", "TERMINAL"})
    result[488] = int(state.exchange.anti_tribute)
    result[489] = int(state.current_family is not None)
    result[490] = len(state.private_prefix)
    result[491] = int(state.observer_seat == state.active_seat)
    result[492] = int(state.finishing_ranks[state.observer_seat] != 0)
    result[493] = int(state.observer_seat in state.emptied_seats)
    result[494] = int(bool(state.exchange.return_obligations))
    result[495] = int(bool(state.exchange.self_selection))
    result[496] = int(state.exchange.outgoing_return_transfer)
    result[497] = int(state.previous_result is not None)
    result[498] = int(state.current_winner_seat == state.observer_seat)
    result[499] = int(state.leader_seat == state.observer_seat)
    result[500] = int(state.active_seat % 2 == state.observer_seat % 2)
    result[511] = 1
    if len(result) != CHANNEL_COUNT or any(type(value) is not int or not 0 <= value <= 255 for value in result):
        _reject("channel_value_invalid")
    if any(result[index] != 0 for index in range(501, 511)) or result[511] != 1:
        _reject("channel_reserved_invalid")
    return tuple(result)


def adapt_live_state(state: object) -> MarvelState:
    """Current live v2 state is intentionally ineligible for Phase 3 encoding.

    The existing contract cannot prove exact four-observer history/private
    prefixes and deliberately cannot mark history-completeness facts true.
    Keeping this adapter explicit prevents zero/PAD guessing or a provider
    fallback when a live field is unknown.
    """
    if state is None:
        raise UnsupportedStateError("canonical_guandan_state_required")
    raise UnsupportedStateError("phase3_live_state_not_eligible")


def encode_observer_channels(states: Iterable[MarvelState]) -> tuple[tuple[int, ...], ...]:
    """Encode the exact four observer streams for one model time step."""
    values = tuple(states)
    if len(values) != 4:
        raise UnsupportedStateError("exact_four_observers_required")
    if tuple(state.observer_seat for state in values) != (0, 1, 2, 3):
        raise UnsupportedStateError("observer_order_unavailable")
    return tuple(encode_channels(state) for state in values)


def encode_channels_batch(
    batch: Iterable[Iterable[MarvelState]],
) -> tuple[tuple[tuple[tuple[int, ...], ...], ...], ...]:
    """Encode a [B,T,4,512] tuple without constructing model tensors."""
    return tuple(tuple(encode_observer_channels(states) for states in time_steps)
                 for time_steps in batch)


__all__ = ["adapt_live_state", "encode_channels", "encode_observer_channels",
           "encode_channels_batch"]
