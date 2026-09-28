"""Deterministic card-code utilities for the observed Tencent Daguandan wire format."""
from __future__ import annotations

from dataclasses import dataclass

SUIT_NAMES = {0: "joker", 1: "S", 2: "H", 3: "C", 4: "D"}
RANK_NAMES = {1: "A", 11: "J", 12: "Q", 13: "K", 14: "JOKER_SMALL", 15: "JOKER_BIG"}


class CardCodecError(ValueError):
    """Raised when a wire card code is not a physical card in a two-deck game."""


@dataclass(frozen=True, slots=True, order=True)
class Card:
    """A physical card; copy distinguishes the two copies in a double deck."""

    suit: int
    rank: int
    copy: int = 0

    @property
    def code(self) -> int:
        return encode_card(self.suit, self.rank, self.copy)

    @property
    def label(self) -> str:
        return card_label(self.code)


def validate_parts(suit: int, rank: int, copy: int = 0) -> tuple[int, int, int]:
    if suit not in (0, 1, 2, 3, 4):
        raise CardCodecError(f"invalid suit: {suit}")
    if copy not in (0, 1):
        raise CardCodecError(f"invalid copy index: {copy}")
    if suit == 0:
        if rank not in (14, 15):
            raise CardCodecError(f"invalid joker rank: {rank}")
    elif not 1 <= rank <= 13:
        raise CardCodecError(f"invalid rank: {rank}")
    return suit, rank, copy


def encode_card(suit: int, rank: int, copy: int = 0) -> int:
    validate_parts(suit, rank, copy)
    return (copy << 8) | (suit << 4) | rank


def card_parts(code: int) -> tuple[int, int, int]:
    if not isinstance(code, int) or isinstance(code, bool) or code < 0:
        raise CardCodecError(f"invalid card code: {code!r}")
    rank = code & 0x0F
    suit = (code >> 4) & 0x0F
    copy = code >> 8
    validate_parts(suit, rank, copy)
    return suit, rank, copy


def card_label(code: int) -> str:
    suit, rank, _ = card_parts(code)
    if suit == 0:
        return RANK_NAMES[rank]
    return f"{SUIT_NAMES[suit]}{RANK_NAMES.get(rank, str(rank))}"


def card_from_code(code: int) -> Card:
    return Card(*card_parts(code))


def full_deck() -> tuple[int, ...]:
    return tuple(
        encode_card(suit, rank, copy)
        for suit in (1, 2, 3, 4)
        for rank in range(1, 14)
        for copy in (0, 1)
    ) + tuple(encode_card(0, rank, copy) for rank in (14, 15) for copy in (0, 1))


def validate_deck(cards: list[int] | tuple[int, ...]) -> None:
    if len(cards) != 108 or len(set(cards)) != 108 or set(cards) != set(full_deck()):
        raise CardCodecError("cards are not the complete two-deck physical card set")
