"""Public-play rank counts; never consumes opponent private hands.

The two copies of the same suit/rank are distinct physical cards. All counting
uses physical-code sets so duplicate observations do not decrement a rank more
than once. The public remaining value is total physical cards minus confirmed
public plays; an unplayed own hand is deliberately not subtracted.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import TypedDict

from .cards import card_parts, full_deck


class CardCounterRow(TypedDict):
    rank: int
    total: int
    played: int
    in_hand: int
    remaining: int
    # Compatibility alias retained for existing consumers. It means remaining
    # after public plays, not a hidden-opponent count.
    unseen: int
    is_level: bool


_DECK = frozenset(full_deck())
_TOTAL_BY_RANK = Counter(card_parts(code)[1] for code in _DECK)


def _physical_cards(cards: Iterable[int]) -> set[int]:
    result: set[int] = set()
    for code in cards:
        # Validate before deduplication (including bool/non-physical codes).
        # Invalid input must not silently distort or clamp the rank counts.
        card_parts(code)
        result.add(code)
    return result


def count_visible_cards(
    hand: Iterable[int], played_public: Iterable[int], *, level: int,
) -> list[CardCounterRow]:
    """Return 15 independent rows in physical rank order (A=1, jokers=14/15).

    Only the current own hand and confirmed public plays are accepted. The
    remaining value is full-deck rank count minus the deduplicated public plays;
    the own hand is reported separately and does not reduce remaining. Level
    changes only the highlight; wild-card claims never change a physical card's
    rank. Callers receive fresh rows and cannot mutate cached state.
    """
    own = _physical_cards(hand)
    played = _physical_cards(played_public)
    own_counts = Counter(card_parts(code)[1] for code in own)
    played_counts = Counter(card_parts(code)[1] for code in played)
    return [
        # The public-play counter intentionally does not subtract the
        # observer's unplayed hand. It answers only how many physical cards
        # of this rank have not been publicly played yet.
        CardCounterRow(
            rank=rank,
            total=_TOTAL_BY_RANK[rank],
            played=played_counts[rank],
            in_hand=own_counts[rank],
            remaining=max(0, _TOTAL_BY_RANK[rank] - played_counts[rank]),
            unseen=max(0, _TOTAL_BY_RANK[rank] - played_counts[rank]),
            is_level=(rank == level and 1 <= rank <= 13),
        )
        for rank in range(1, 16)
    ]
