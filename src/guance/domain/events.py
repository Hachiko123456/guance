"""Immutable normalized game events; no Qt, network, model, or protobuf dependency."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ActionKind = Literal["play", "pass"]


@dataclass(frozen=True, slots=True)
class SourceMeta:
    """Local transport metadata, not a server-provided action sequence."""

    received_at: float = 0.0
    sequence: int = 0
    connection_id: str = ""
    sub_index: int = -1


@dataclass(frozen=True, slots=True)
class DealEvent:
    my_seat: int
    level: int
    hand: tuple[int, ...] = field(repr=False)
    meta: SourceMeta = field(default_factory=SourceMeta)


@dataclass(frozen=True, slots=True)
class StartEvent:
    first_seat: int
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
    meta: SourceMeta = field(default_factory=SourceMeta)


ProtocolEvent = DealEvent | StartEvent | ActionEvent | SettlementEvent | InformationalEvent
