"""Immutable normalized game events; no Qt, network, model, or protobuf dependency."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from guance.public_state_contract import (
    ExchangeState, ExchangeTransfer, Knowledge, EvidenceRef, Phase,
    PreviousResultSummary, PublicDeclaration, PublicStateContractError,
)

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
    record_type: int = 0
    record_version: int = 0
    command_variant: int = 0
    group_shape: str = ""


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
class ExchangeInitializationEvent:
    """Observed 3011 boundary. Internal fields intentionally remain opaque."""

    meta: SourceMeta = field(default_factory=SourceMeta)


@dataclass(frozen=True, slots=True)
class TributeSelectionEvent:
    """Observed 3012 public candidate fields; private hand bytes are discarded."""

    donor_seat: int
    tribute_card: int
    tribute_cards: tuple[int, ...]
    recipient_seats: tuple[int, ...]
    meta: SourceMeta = field(default_factory=SourceMeta)


@dataclass(frozen=True, slots=True)
class TributeSummaryEntry:
    tribute_card: int
    donor_seat: int
    recipient_seat: int


@dataclass(frozen=True, slots=True)
class TributeSummaryEvent:
    entries: tuple[TributeSummaryEntry, ...]
    meta: SourceMeta = field(default_factory=SourceMeta)


@dataclass(frozen=True, slots=True)
class ReturnSelectionEvent:
    source_seat: int
    return_card: int
    target_seat: int
    meta: SourceMeta = field(default_factory=SourceMeta)


@dataclass(frozen=True, slots=True)
class ExchangeCardSource:
    card: int
    source_seat: int


@dataclass(frozen=True, slots=True)
class ExchangeSummaryEvent:
    card_sources: tuple[ExchangeCardSource, ...]
    return_entries: tuple[ReturnSelectionEvent, ...]
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


def _semantic_require(condition: bool) -> None:
    if not condition:
        raise PublicStateContractError("SEMANTIC_EVENT_INVALID")


def _semantic_seat(value: object) -> bool:
    return type(value) is int and 0 <= value < 4


@dataclass(frozen=True, slots=True)
class PhaseObserved:
    """Explicit confirmed semantics; no current live command produces this event."""

    phase: Phase
    active_seat: int | None = None
    meta: SourceMeta = field(default_factory=SourceMeta)

    def validate(self) -> None:
        _semantic_require(type(self.phase) is str and self.phase in {
            "TRIBUTE", "RETURN", "LEAD_PLAY", "FOLLOW_PLAY", "TERMINAL",
        })
        _semantic_require(self.active_seat is None or _semantic_seat(self.active_seat))


@dataclass(frozen=True, slots=True)
class PublicResultObserved:
    """A confirmed, explicitly associated *previous* game's public result."""

    result: PreviousResultSummary
    meta: SourceMeta = field(default_factory=SourceMeta)

    def validate(self) -> None:
        _semantic_require(type(self.result) is PreviousResultSummary)
        self.result.to_dict()


@dataclass(frozen=True, slots=True)
class DeclaredActionCommitted:
    """A confirmed play plus its lossless declaration, not a client intention.

    The legacy action still supplies its own observed combo code. No mapping
    from that opaque code to this declaration is defined or assumed.
    """

    action: ActionEvent
    declaration: PublicDeclaration

    def validate(self) -> None:
        try:
            _semantic_require(type(self.action) is ActionEvent and self.action.kind == "play")
            _semantic_require(type(self.declaration) is PublicDeclaration)
            self.declaration.to_dict()
            _semantic_require(type(self.action.cards) is tuple)
            _semantic_require(set(self.action.cards) == set(self.declaration.canonical_cards))
        except PublicStateContractError:
            raise PublicStateContractError("SEMANTIC_EVENT_INVALID") from None


@dataclass(frozen=True, slots=True)
class ExchangeResolvedObserved:
    """Public, resolved transfers and optional explicitly observed exchange facts.

    None means unobserved, while an explicit empty tuple/False is a fact. This
    event deliberately has no private selection or any player's hidden hand.
    """

    transfers: tuple[ExchangeTransfer, ...]
    tribute_donors: tuple[int, ...] | None = None
    tribute_order: tuple[int, ...] | None = None
    anti_tribute: bool | None = None
    return_obligations: tuple[tuple[int, int], ...] | None = None
    meta: SourceMeta = field(default_factory=SourceMeta)

    def validate(self) -> None:
        _semantic_require(type(self.transfers) is tuple and len(self.transfers) <= 8)
        _semantic_require(all(type(t) is ExchangeTransfer for t in self.transfers))
        for values in (self.tribute_donors, self.tribute_order, self.return_obligations):
            _semantic_require(values is None or type(values) is tuple)
        if self.return_obligations is not None:
            _semantic_require(all(type(pair) is tuple for pair in self.return_obligations))
        evidence = EvidenceRef("semantic.exchange")
        def fact(value: object) -> Knowledge:
            return (Knowledge.unsupported("exchange_field_unobserved") if value is None
                    else Knowledge.known(value, evidence))
        ExchangeState(fact(self.tribute_donors), fact(self.tribute_order),
                      fact(self.anti_tribute), fact(self.return_obligations),
                      fact(self.transfers), Knowledge.unsupported("selection_unobserved")).to_dict()


@dataclass(frozen=True, slots=True)
class OwnExchangeSelectionObserved:
    """Self-visible selection, never a resolved action and never another seat's."""

    observer_seat: int
    phase: Literal["TRIBUTE", "RETURN"]
    cards: tuple[int, ...] = field(repr=False)
    meta: SourceMeta = field(default_factory=SourceMeta)

    def validate(self) -> None:
        _semantic_require(_semantic_seat(self.observer_seat))
        _semantic_require(type(self.phase) is str and self.phase in {"TRIBUTE", "RETURN"})
        _semantic_require(type(self.cards) is tuple and len(self.cards) <= 1)
        evidence = EvidenceRef("semantic.self_selection")
        missing = Knowledge.unsupported("exchange_field_unobserved")
        ExchangeState(missing, missing, missing, missing, missing,
                      Knowledge.known(self.cards, evidence)).to_dict()


SemanticEvent = (PhaseObserved | PublicResultObserved | DeclaredActionCommitted
                 | ExchangeResolvedObserved | OwnExchangeSelectionObserved)
ProtocolEvent = (DealEvent | StartEvent | OwnSeatCandidateEvent | ActionEvent
                 | SettlementEvent | ExchangeInitializationEvent | TributeSelectionEvent
                 | TributeSummaryEvent | ReturnSelectionEvent | ExchangeSummaryEvent
                 | InformationalEvent | SemanticEvent)
