"""Immutable normalized game events; no Qt, network, model, or protobuf dependency."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

ActionKind = Literal["play", "pass"]
UNCONFIRMED_SEAT = -1


class SeatRelation(str, Enum):
    """Relationship of the observed first seat to the observer."""

    SELF = "self"
    LEFT = "left"
    PARTNER = "partner"
    RIGHT = "right"


@dataclass(frozen=True, slots=True)
class SourceMeta:
    """Local transport metadata, not a server-provided action sequence."""

    received_at: float = 0.0
    sequence: int = 0
    connection_id: str = ""
    message_index: int = -1
    sub_index: int = -1


@dataclass(frozen=True, slots=True)
class DealEvent:
    # The live 3001 field formerly wired here is a start/lead hint, not the
    # observer's seat.  The parser therefore emits -1 and GameState confirms
    # the seat from later public plays.  Keep the field for contract
    # compatibility while making the sentinel explicit.
    my_seat: int
    level: int
    hand: tuple[int, ...] = field(repr=False)
    first_seat_hint: int | None = None
    first_seat_hint_present: bool = False
    first_seat_hint_secondary: int | None = None
    meta: SourceMeta = field(default_factory=SourceMeta)


@dataclass(frozen=True, slots=True)
class StartEvent:
    first_seat: int
    first_seat_present: bool = True
    meta: SourceMeta = field(default_factory=SourceMeta)


@dataclass(frozen=True, slots=True)
class OwnSeatCandidateEvent:
    """Neutral 3019 candidate; GameState must validate ownership before confirming."""

    candidate_seat: int
    candidate_seat_present: bool
    referenced_cards: tuple[int, ...] = field(repr=False)
    source: Literal["3019"] = "3019"
    meta: SourceMeta = field(default_factory=SourceMeta)


@dataclass(frozen=True, slots=True)
class ActionEvent:
    player: int
    kind: ActionKind
    cards: tuple[int, ...] = ()
    combo_type: int | None = None
    next_player: int | None = None
    remaining_after: int | None = None
    reset_flag: int | None = None
    finish_rank: int | None = None
    own_hand_snapshot: tuple[int, ...] | None = field(default=None, repr=False)
    # Replay-only assertions; the observed live protocol has no verified action ID.
    action_index: int | None = None
    expected_trick: int | None = None
    free_lead_before: bool | None = None
    meta: SourceMeta = field(default_factory=SourceMeta)


@dataclass(frozen=True, slots=True)
class SettlementEvent:
    ranks_by_seat: tuple[int, ...]
    # Private opponent cards may be present only after settlement. Validate them
    # ephemerally, then discard. Never serialize this event into diagnostics/UI.
    remaining_hands: tuple[tuple[int, ...], ...] = field(repr=False)
    meta: SourceMeta = field(default_factory=SourceMeta)


@dataclass(frozen=True, slots=True)
class InformationalEvent:
    command: int
    # 3003 carries pre-play numeric evidence whose business semantics are not
    # established by the available captures. Keep each observed shape
    # separate and neutral; never turn it into a per-player doubling result.
    reported_seat_snapshot: tuple[int, ...] | None = None
    reported_values: tuple[float, ...] | None = None
    reported_flag: int | None = None
    meta: SourceMeta = field(default_factory=SourceMeta)


ProtocolEvent = DealEvent | StartEvent | OwnSeatCandidateEvent | ActionEvent | SettlementEvent | InformationalEvent
