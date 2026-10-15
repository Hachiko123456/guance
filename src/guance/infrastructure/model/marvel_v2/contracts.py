"""Pure protocol contracts for the offline Guandan MARVEL V2 adapter.

This module deliberately contains no Torch, native extension, capture, provider,
or application imports.  It accepts only public+self facts and raises a bounded
unsupported error when a required fact is absent or ambiguous.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Iterable, Literal, Sequence

from guance.public_state_contract import guandan_card_id_to_wire, wire_card_to_guandan_id

MARVEL_PROTOCOL = "GD-ACTION-0.2-MARVEL|GD-ENV-0.2-MARVEL|GD-ENCODING-0.2-MARVEL"
CHANNEL_COUNT = 512
OBSERVER_COUNT = 4
EVENT_WIDTH = 48
ACTION_ROW_WIDTH = 8
MAX_ACTION_ROWS = 256
MAX_PREFIX_TOKENS = 32
VOCAB_SIZE = 320
CHANNEL_SLICES = (
    "0:108 own hand", "108:216 current winning cards",
    "216:324 self exchange cards", "324:356 private prefix",
    "356:360 remaining counts", "360:364 finishing ranks",
    "364:368 observer", "368:372 active", "372:376 leader",
    "376:380 winner", "380:384 emptied", "384:388 phase",
    "388:401 level", "401:405 pass count", "405:407 team",
    "407:409 previous winner team", "409:412 previous outcome",
    "412:416 previous ranking", "416:426 current family",
    "426:441 current rank", "441:452 current size",
    "452:456 tribute donors", "456:460 tribute order",
    "460:473 token_step bits", "473:486 committed_step bits",
    "486:501 flags/scalars", "501:511 reserved", "511:512 sentinel",
)
PAD = 0
BOS = 1
PASS_TOKEN = 6
COMMIT_TOKEN = 7
EVENT_RESET = 256
EVENT_PRIVATE_PREFIX = 257
EVENT_PUBLIC_COMMIT = 258
EVENT_PRIVATE_EXCHANGE_COMMIT = 259
EVENT_EXCHANGE_RESOLVED = 260
EVENT_HIDDEN_PRIVATE_STEP = 261
ROW_BEGIN = 262
ROW_END = 307
EVENT_END = 308

FAMILIES = (
    "SINGLE", "PAIR", "TRIPLE", "FULL_HOUSE", "STRAIGHT", "PAIR_SEQUENCE",
    "TRIPLE_SEQUENCE", "RANK_BOMB", "STRAIGHT_FLUSH", "FOUR_KINGS",
)
SUITS = ("CLUBS", "DIAMONDS", "HEARTS", "SPADES", "JOKER")
PHASES = ("TRIBUTE", "RETURN", "LEAD_PLAY", "FOLLOW_PLAY", "TERMINAL")
PLAY_PHASES = frozenset(("LEAD_PLAY", "FOLLOW_PLAY"))
PHASE_TOKEN = {"LEAD_PLAY": 2, "FOLLOW_PLAY": 3, "TRIBUTE": 4, "RETURN": 5}
TOKEN_PHASE = {value: key for key, value in PHASE_TOKEN.items()}
FAMILY_TOKEN = {family: 8 + i for i, family in enumerate(FAMILIES)}
TOKEN_FAMILY = {value: key for key, value in FAMILY_TOKEN.items()}
RANK_TOKEN = {rank: 18 + rank - 3 for rank in range(3, 18)}
TOKEN_RANK = {value: key for key, value in RANK_TOKEN.items()}
SUIT_TOKEN = {suit: 33 + i for i, suit in enumerate(SUITS)}
TOKEN_SUIT = {value: key for key, value in SUIT_TOKEN.items()}
LENGTH_TOKEN = {length: 38 + length for length in range(0, 11)}
TOKEN_LENGTH = {value: key for key, value in LENGTH_TOKEN.items()}
CARD_TOKEN_BASE = 64
WILD_TOKEN_BASE = 172
def _reject(code: str = "MARVEL_PROTOCOL_INVALID") -> None:
    raise MarvelProtocolError(code)


class MarvelProtocolError(ValueError):
    """A bounded, non-secret protocol error."""

    def __init__(self, code: str = "MARVEL_PROTOCOL_INVALID") -> None:
        self.code = code
        super().__init__(code)


class UnsupportedStateError(MarvelProtocolError):
    """A required state fact is missing, unknown, or not safely derivable."""


class ActionProtocolError(MarvelProtocolError):
    """A row, prefix, or committed action is stale or semantically invalid."""


def _int(value: object, low: int, high: int, code: str = "MARVEL_PROTOCOL_INVALID") -> int:
    if type(value) is not int or not low <= value <= high:
        _reject(code)
    return value


def _seat(value: object) -> int:
    return _int(value, 0, 3, "seat_invalid")


def _tuple_ints(value: Iterable[object], low: int, high: int, maximum: int,
                *, unique: bool = False, code: str = "MARVEL_PROTOCOL_INVALID") -> tuple[int, ...]:
    if isinstance(value, (str, bytes)):
        _reject(code)
    try:
        result = tuple(_int(item, low, high, code) for item in value)
    except TypeError:
        _reject(code)
    if len(result) > maximum or unique and len(result) != len(set(result)):
        _reject(code)
    return result


def card_face(card_id: int) -> tuple[int, int, int]:
    card_id = _int(card_id, 0, 107, "card_id_invalid")
    deck, offset = divmod(card_id, 54)
    if offset < 52:
        return 3 + offset // 4, offset % 4, deck
    return 16 + offset - 52, 4, deck


def storage_order(card_ids: Iterable[int]) -> tuple[int, ...]:
    ids = _tuple_ints(card_ids, 0, 107, 108, unique=True, code="card_set_invalid")
    return tuple(sorted(ids, key=lambda item: (*card_face(item), item)))


def sequence_ranks(start_rank: int, length: int) -> tuple[int, ...]:
    start_rank = _int(start_rank, 3, 15, "sequence_start_invalid")
    length = _int(length, 2, 5, "sequence_length_invalid")
    if start_rank == 14:
        values = (14, 15, 3, 4, 5)
    elif start_rank == 15:
        values = (15, 3, 4, 5, 6)
    else:
        values = tuple(range(start_rank, start_rank + length))
        if values[-1] > 15:
            _reject("sequence_window_invalid")
    return values[:length]


def sequence_comparison_rank(start_rank: int, length: int) -> int:
    return sequence_ranks(start_rank, length)[-1]


def is_level_wild(card_id: int, level_rank: int) -> bool:
    rank, suit, _deck = card_face(card_id)
    return rank == _int(level_rank, 3, 15, "level_rank_invalid") and suit == 2


class CardMapping:
    """Algorithmic copy/deck/card mapping; no runtime handoff or fixture access."""

    card_count = 108
    level_count = 13

    @staticmethod
    def to_wire(card_id: int) -> int:
        return guandan_card_id_to_wire(_int(card_id, 0, 107, "card_id_invalid"))

    @staticmethod
    def from_wire(physical_code: int) -> int:
        try:
            return wire_card_to_guandan_id(physical_code)
        except (ValueError, TypeError):
            _reject("physical_card_invalid")

    @staticmethod
    def level_to_rank(level: int) -> int:
        level = _int(level, 1, 13, "level_invalid")
        return 14 if level == 1 else 15 if level == 2 else level

    @staticmethod
    def rank_to_level(rank: int) -> int:
        rank = _int(rank, 3, 15, "level_rank_invalid")
        return 1 if rank == 14 else 2 if rank == 15 else rank

    @classmethod
    def validate(cls) -> None:
        wires = tuple(cls.to_wire(card_id) for card_id in range(cls.card_count))
        if len(set(wires)) != cls.card_count:
            _reject("card_mapping_nonunique")
        if any(cls.from_wire(wire) != card_id for card_id, wire in enumerate(wires)):
            _reject("card_mapping_not_bijective")
        ranks = tuple(cls.level_to_rank(level) for level in range(1, 14))
        if ranks != (14, 15, *range(3, 14)):
            _reject("level_mapping_invalid")


@dataclass(frozen=True, slots=True)
class WildAssignment:
    physical_card: int
    assigned_rank: int
    assigned_suit: str
    substituted: bool

    def validate(self) -> None:
        try:
            CardMapping.from_wire(self.physical_card)
        except MarvelProtocolError:
            _reject("wild_physical_card_invalid")
        _int(self.assigned_rank, 3, 15, "wild_rank_invalid")
        if self.assigned_suit not in {"CLUBS", "DIAMONDS", "HEARTS", "SPADES"}:
            _reject("wild_suit_invalid")
        if type(self.substituted) is not bool:
            _reject("wild_mode_invalid")


def _window(ranks: Iterable[int], length: int) -> tuple[int, ...]:
    wanted = sorted(ranks)
    starts = {5: (15, *range(3, 11), 14), 3: (15, *range(3, 13), 14),
              2: (15, *range(3, 14), 14)}[length]
    matches = [sequence_ranks(start, length) for start in starts
               if sorted(sequence_ranks(start, length)) == wanted]
    if len(matches) != 1:
        _reject("sequence_window_invalid")
    return matches[0]


def _ordered_by_effective_rank(faces: Sequence[tuple[int, str, int]],
                               rank_order: Sequence[int]) -> tuple[int, ...]:
    by_rank = {rank: [] for rank in rank_order}
    for rank, _suit, card_id in faces:
        if rank not in by_rank:
            _reject("canonical_action_order_invalid")
        by_rank[rank].append(card_id)
    return tuple(card_id for rank in rank_order for card_id in sorted(by_rank[rank]))


@dataclass(frozen=True, slots=True)
class ActionDeclaration:
    """Lossless model-layer declaration independent of domain DTO classes."""

    family: str
    declared_ranks: tuple[int, ...]
    comparison_rank: int
    suit: str | None
    length: int
    canonical_cards: tuple[int, ...]
    wild_assignments: tuple[WildAssignment, ...] = ()
    canonical_action_cards: tuple[int, ...] = ()
    protocol_length: int | None = None

    @classmethod
    def from_domain(cls, value: object) -> "ActionDeclaration":
        """Copy a structurally compatible domain DTO without retaining it."""
        try:
            assignments = tuple(
                WildAssignment(item.physical_card, item.assigned_rank,
                               item.assigned_suit, item.substituted)
                for item in value.wild_assignments
            )
            return cls(
                family=value.family, declared_ranks=tuple(value.declared_ranks),
                comparison_rank=value.comparison_rank, suit=value.suit,
                length=value.length, canonical_cards=tuple(value.canonical_cards),
                wild_assignments=assignments,
                canonical_action_cards=tuple(value.canonical_action_cards),
                protocol_length=value.protocol_length,
            )
        except (AttributeError, TypeError):
            _reject("declaration_adapter_invalid")

    def validate(self, level: int) -> None:
        if self.family not in FAMILIES or type(self.declared_ranks) is not tuple:
            _reject("declaration_family_invalid")
        if self.suit is not None and self.suit not in SUITS:
            _reject("declaration_suit_invalid")
        ranks = _tuple_ints(self.declared_ranks, 3, 17, 8, code="declared_ranks_invalid")
        _int(self.comparison_rank, 3, 17, "comparison_rank_invalid")
        _int(self.length, 1, 27, "physical_length_invalid")
        if self.protocol_length is not None:
            _int(self.protocol_length, 0, 10, "protocol_length_invalid")
        card_ids = tuple(CardMapping.from_wire(card) for card in self.canonical_cards)
        action_ids = tuple(CardMapping.from_wire(card) for card in self.canonical_action_cards)
        if (len(card_ids) != len(set(card_ids)) or len(action_ids) != len(set(action_ids))
                or set(card_ids) != set(action_ids) or self.length != len(card_ids)):
            _reject("declaration_cards_invalid")
        if card_ids != storage_order(card_ids):
            _reject("declaration_storage_order_invalid")
        if type(self.wild_assignments) is not tuple or len(self.wild_assignments) > 2:
            _reject("wild_assignments_invalid")
        level_rank = CardMapping.level_to_rank(level)
        assignments: dict[int, WildAssignment] = {}
        assignment_ids = []
        for assignment in self.wild_assignments:
            if type(assignment) is not WildAssignment:
                _reject("wild_assignments_invalid")
            assignment.validate()
            card_id = CardMapping.from_wire(assignment.physical_card)
            assignment_ids.append(card_id)
            assignments[card_id] = assignment
            natural = assignment.assigned_rank == level_rank and assignment.assigned_suit == "HEARTS"
            if self.family == "STRAIGHT_FLUSH":
                if assignment.assigned_suit != self.suit:
                    _reject("wild_assignment_suit_invalid")
            elif not natural and assignment.assigned_suit != "CLUBS":
                _reject("wild_assignment_suit_invalid")
            if assignment.substituted == natural:
                _reject("wild_assignment_mode_invalid")
        expected_wilds = {card_id for card_id in card_ids if is_level_wild(card_id, level_rank)}
        if (assignment_ids != sorted(assignment_ids) or len(assignments) != len(assignment_ids)
                or set(assignments) != expected_wilds):
            _reject("wild_assignment_cards_invalid")
        faces = []
        for card_id in card_ids:
            rank, suit_index, _deck = card_face(card_id)
            assignment = assignments.get(card_id)
            if assignment is not None:
                rank = assignment.assigned_rank
                suit = assignment.assigned_suit
            else:
                suit = SUITS[suit_index]
            faces.append((rank, suit, card_id))
        effective_ranks = tuple(face[0] for face in faces)
        counts = {rank: effective_ranks.count(rank) for rank in set(effective_ranks)}
        if self.family in {"SINGLE", "PAIR", "TRIPLE", "RANK_BOMB"}:
            expected = {"SINGLE": 1, "PAIR": 2, "TRIPLE": 3}.get(self.family)
            if (self.suit is not None or self.protocol_length is not None or len(ranks) != 1
                    or expected is not None and self.length != expected
                    or self.family == "RANK_BOMB" and not 4 <= self.length <= 10
                    or len(counts) != 1 or ranks[0] != effective_ranks[0]
                    or self.comparison_rank != effective_ranks[0] or action_ids != card_ids):
                _reject("simple_declaration_invalid")
            return
        if self.family == "FULL_HOUSE":
            values = sorted(counts.values())
            if (self.length != 5 or self.suit is not None or self.protocol_length is not None
                    or values != [2, 3] or any(rank > 15 for rank in counts)):
                _reject("full_house_invalid")
            triple = next(rank for rank, count in counts.items() if count == 3)
            pair = next(rank for rank, count in counts.items() if count == 2)
            if (ranks != (triple, pair) or self.comparison_rank != triple
                    or action_ids != _ordered_by_effective_rank(faces, (triple, pair))):
                _reject("full_house_invalid")
            assigned = set(assignments)
            if not any(rank == triple and card_id not in assigned for rank, _suit, card_id in faces):
                _reject("full_house_all_wild_triple_invalid")
            return
        if self.family in {"STRAIGHT", "STRAIGHT_FLUSH"}:
            window = _window(counts, 5)
            if (self.length != 5 or self.protocol_length is not None or set(counts.values()) != {1}
                    or ranks != (window[0],) or self.comparison_rank != window[-1]
                    or action_ids != _ordered_by_effective_rank(faces, window)):
                _reject("straight_invalid")
            suits = {face[1] for face in faces}
            if self.family == "STRAIGHT" and self.suit is not None:
                _reject("straight_suit_forbidden")
            if self.family == "STRAIGHT_FLUSH" and (self.suit not in SUITS[:4] or suits != {self.suit}):
                _reject("straight_flush_suit_invalid")
            return
        if self.family in {"PAIR_SEQUENCE", "TRIPLE_SEQUENCE"}:
            multiplicity = 2 if self.family == "PAIR_SEQUENCE" else 3
            sequence_length = 3 if multiplicity == 2 else 2
            window = _window(counts, sequence_length)
            if (self.length != 6 or self.suit is not None or self.protocol_length != sequence_length
                    or set(counts.values()) != {multiplicity} or ranks != (window[0],)
                    or self.comparison_rank != window[-1]
                    or action_ids != _ordered_by_effective_rank(faces, window)):
                _reject("sequence_declaration_invalid")
            return
        if self.family == "FOUR_KINGS":
            if (self.length != 4 or ranks or self.comparison_rank != 17 or self.suit is not None
                    or self.protocol_length is not None or assignments
                    or card_ids != (52, 106, 53, 107)
                    or action_ids != (52, 53, 106, 107)):
                _reject("four_kings_invalid")
            return
        _reject("declaration_family_invalid")


@dataclass(frozen=True, slots=True)
class PreviousResultFacts:
    ranking_by_seat: tuple[int, int, int, int]
    winner_team: int
    outcome_class: Literal["DOUBLE_DOWN", "HEAD_THIRD", "HEAD_LAST"]
    tribute_cancelled: bool

    def validate(self) -> None:
        if tuple(sorted(self.ranking_by_seat)) != (1, 2, 3, 4):
            _reject("previous_result_ranking_invalid")
        _int(self.winner_team, 0, 1, "previous_result_team_invalid")
        if self.outcome_class not in {"DOUBLE_DOWN", "HEAD_THIRD", "HEAD_LAST"}:
            _reject("previous_result_outcome_invalid")
        winner_seat = self.ranking_by_seat.index(1)
        if winner_seat % 2 != self.winner_team:
            _reject("previous_result_team_invalid")
        mate_seat = next(seat for seat in range(4)
                         if seat % 2 == self.winner_team and seat != winner_seat)
        mate_rank = self.ranking_by_seat[mate_seat]
        expected = {2: "DOUBLE_DOWN", 3: "HEAD_THIRD", 4: "HEAD_LAST"}[mate_rank]
        if self.outcome_class != expected:
            _reject("previous_result_outcome_invalid")
        if type(self.tribute_cancelled) is not bool:
            _reject("previous_result_tribute_invalid")


@dataclass(frozen=True, slots=True)
class ExchangeTransferFacts:
    kind: Literal["tribute", "return"]
    source_seat: int
    target_seat: int
    card_id: int

    def validate(self) -> None:
        if self.kind not in {"tribute", "return"}:
            _reject("exchange_kind_invalid")
        _seat(self.source_seat); _seat(self.target_seat)
        if self.source_seat == self.target_seat:
            _reject("exchange_seats_invalid")
        _int(self.card_id, 0, 107, "exchange_card_invalid")


@dataclass(frozen=True, slots=True)
class ExchangeFacts:
    tribute_donors: tuple[int, ...] = ()
    tribute_order: tuple[int, ...] = ()
    anti_tribute: bool = False
    return_obligations: tuple[tuple[int, int], ...] = ()
    resolved_transfers: tuple[ExchangeTransferFacts, ...] = ()
    self_selection: tuple[int, ...] = ()
    outgoing_return_transfer: bool = False

    def validate(self) -> None:
        donors = _tuple_ints(self.tribute_donors, 0, 3, 4, unique=True, code="exchange_donors_invalid")
        order = _tuple_ints(self.tribute_order, 0, 3, 4, unique=True, code="exchange_order_invalid")
        if order and set(order) != set(donors):
            _reject("exchange_order_mismatch")
        if type(self.anti_tribute) is not bool or type(self.outgoing_return_transfer) is not bool:
            _reject("exchange_flag_invalid")
        obligations = tuple(tuple(pair) for pair in self.return_obligations)
        if len(obligations) > 4 or any(len(pair) != 2 for pair in obligations):
            _reject("return_obligations_invalid")
        for source, target in obligations:
            _seat(source); _seat(target)
            if source == target:
                _reject("return_obligation_seats_invalid")
        if len(self.resolved_transfers) > 8:
            _reject("exchange_transfer_overflow")
        for transfer in self.resolved_transfers:
            if type(transfer) is not ExchangeTransferFacts:
                _reject("exchange_transfer_invalid")
            transfer.validate()
        _tuple_ints(self.self_selection, 0, 107, 1, unique=True, code="exchange_selection_invalid")


@dataclass(frozen=True, slots=True)
class MarvelState:
    """Complete public+self state required by the 512 channel contract."""

    observer_seat: int
    active_seat: int
    leader_seat: int
    current_winner_seat: int | None
    phase: Literal["TRIBUTE", "RETURN", "LEAD_PLAY", "FOLLOW_PLAY", "TERMINAL"]
    level: int
    own_hand: tuple[int, ...]
    current_winning_cards: tuple[int, ...]
    self_exchange_cards: tuple[int, ...]
    private_prefix: tuple[int, ...]
    remaining_counts: tuple[int, int, int, int]
    finishing_ranks: tuple[int, int, int, int]
    emptied_seats: tuple[int, ...]
    consecutive_passes: int
    current_family: str | None
    current_comparison_rank: int | None
    current_winning_size: int
    previous_result: PreviousResultFacts | None
    exchange: ExchangeFacts
    token_step: int
    committed_step: int
    history_complete: bool = True

    def validate(self) -> None:
        _seat(self.observer_seat); _seat(self.active_seat); _seat(self.leader_seat)
        if self.current_winner_seat is not None:
            _seat(self.current_winner_seat)
        if self.phase not in PHASES:
            _reject("phase_invalid")
        _int(self.level, 1, 13, "level_invalid")
        hand = _tuple_ints(self.own_hand, 0, 107, 28, unique=True, code="own_hand_invalid")
        winning = _tuple_ints(self.current_winning_cards, 0, 107, 27, unique=True, code="winning_cards_invalid")
        exchange_cards = _tuple_ints(self.self_exchange_cards, 0, 107, 1, unique=True, code="self_exchange_invalid")
        prefix = _tuple_ints(self.private_prefix, 0, 255, MAX_PREFIX_TOKENS, code="private_prefix_invalid")
        if any(token < 8 for token in prefix):
            _reject("private_prefix_invalid")
        if prefix and self.phase not in {"LEAD_PLAY", "FOLLOW_PLAY"}:
            _reject("private_prefix_phase_invalid")
        counts = _tuple_ints(self.remaining_counts, 0, 28, 4, code="remaining_counts_invalid")
        ranks = _tuple_ints(self.finishing_ranks, 0, 4, 4, code="finishing_ranks_invalid")
        emptied = _tuple_ints(self.emptied_seats, 0, 3, 4, unique=True, code="emptied_seats_invalid")
        _int(self.consecutive_passes, 0, 3, "pass_count_invalid")
        if self.current_family is not None and self.current_family not in FAMILIES:
            _reject("current_family_invalid")
        if self.current_family is None:
            if winning or self.current_comparison_rank is not None or self.current_winning_size != 0:
                _reject("current_winning_metadata_invalid")
        else:
            if not winning or self.current_comparison_rank not in range(3, 18):
                _reject("current_winning_metadata_invalid")
            if self.current_winning_size != len(winning) or not 1 <= self.current_winning_size <= 10:
                _reject("current_winning_size_invalid")
        if self.current_family is None and self.current_winning_size != 0:
            _reject("current_winning_size_invalid")
        if self.previous_result is not None:
            if type(self.previous_result) is not PreviousResultFacts:
                _reject("previous_result_invalid")
            self.previous_result.validate()
        self.exchange.validate()
        _int(self.token_step, 0, 8191, "token_step_invalid")
        _int(self.committed_step, 0, 8191, "committed_step_invalid")
        if self.committed_step > self.token_step:
            _reject("counter_order_invalid")
        if type(self.history_complete) is not bool:
            _reject("history_complete_invalid")
        if counts[self.observer_seat] != len(hand):
            _reject("own_hand_count_mismatch")
        CardMapping.validate()


@dataclass(frozen=True, slots=True)
class MarvelEvent:
    kind: Literal["RESET", "PRIVATE_PREFIX", "PRIVATE_EXCHANGE_COMMIT", "EXCHANGE_RESOLVED", "PUBLIC_COMMIT"]
    phase: Literal["TRIBUTE", "RETURN", "LEAD_PLAY", "FOLLOW_PLAY"]
    actor: int
    level: int | None = None
    tokens: tuple[int, ...] = ()
    transfers: tuple[ExchangeTransferFacts, ...] = ()
    last_actor: int | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if self.kind not in {
            "RESET", "PRIVATE_PREFIX", "PRIVATE_EXCHANGE_COMMIT",
            "EXCHANGE_RESOLVED", "PUBLIC_COMMIT",
        }:
            _reject("event_kind_invalid")
        if self.phase not in PHASE_TOKEN:
            _reject("event_phase_invalid")
        _seat(self.actor)
        if type(self.tokens) is not tuple:
            _reject("event_tokens_type_invalid")
        if type(self.transfers) is not tuple:
            _reject("event_transfers_type_invalid")
        if self.kind != "RESET" and self.level is not None:
            _reject("event_level_unexpected")
        if self.kind != "EXCHANGE_RESOLVED" and self.last_actor is not None:
            _reject("event_last_actor_unexpected")
        if self.kind != "EXCHANGE_RESOLVED" and self.transfers:
            _reject("event_transfers_unexpected")
        if self.kind == "EXCHANGE_RESOLVED" and self.tokens:
            _reject("event_tokens_unexpected")
        if self.kind == "RESET":
            if self.level is None or type(self.level) is not int or not 1 <= self.level <= 13:
                _reject("event_level_missing")
            if self.tokens or self.transfers or self.last_actor is not None:
                _reject("event_payload_invalid")
            return
        if self.kind == "PRIVATE_PREFIX":
            _tuple_ints(self.tokens, 0, 255, 1, code="event_tokens_invalid")
            if (self.phase in {"TRIBUTE", "RETURN"}
                    and not CARD_TOKEN_BASE <= self.tokens[0] < CARD_TOKEN_BASE + 108):
                _reject("private_prefix_token_invalid")
            return
        if self.kind == "PRIVATE_EXCHANGE_COMMIT":
            if (self.phase not in {"TRIBUTE", "RETURN"}
                    or len(self.tokens) != 2
                    or not CARD_TOKEN_BASE <= self.tokens[0] < CARD_TOKEN_BASE + 108
                    or self.tokens[1] != COMMIT_TOKEN):
                _reject("private_exchange_commit_invalid")
            return
        if self.kind == "PUBLIC_COMMIT":
            _tuple_ints(self.tokens, 0, 255, 32, code="event_tokens_invalid")
            for token in self.tokens:
                try:
                    action_token_kind(token)
                except MarvelProtocolError:
                    _reject("event_tokens_invalid")
            if self.phase not in {"LEAD_PLAY", "FOLLOW_PLAY"}:
                _reject("public_commit_phase_invalid")
            if self.tokens == (PASS_TOKEN,):
                if self.phase != "FOLLOW_PLAY":
                    _reject("public_pass_phase_invalid")
            elif (len(self.tokens) < 2 or self.tokens[-1] != COMMIT_TOKEN):
                _reject("public_commit_sequence_invalid")
            return
        assert self.kind == "EXCHANGE_RESOLVED"
        if self.phase not in {"TRIBUTE", "RETURN"}:
            _reject("exchange_resolution_phase_invalid")
        if self.last_actor is None:
            _reject("event_last_actor_missing")
        _seat(self.last_actor)
        if self.actor != self.last_actor:
            _reject("event_actor_last_actor_mismatch")
        if not self.transfers or len(self.transfers) > 8:
            _reject("event_transfers_invalid")
        seen: set[tuple[str, int, int]] = set()
        cards: set[int] = set()
        for transfer in self.transfers:
            if type(transfer) is not ExchangeTransferFacts:
                _reject("event_transfer_type_invalid")
            transfer.validate()
            key = (transfer.kind, transfer.source_seat, transfer.target_seat)
            if key in seen or transfer.card_id in cards:
                _reject("event_transfers_nonunique")
            seen.add(key)
            cards.add(transfer.card_id)


def action_token_kind(token: int) -> int:
    if token == PASS_TOKEN:
        return 296
    if token == COMMIT_TOKEN:
        return 297
    if token in TOKEN_FAMILY:
        return 298
    if token in TOKEN_RANK:
        return 299
    if token in TOKEN_SUIT:
        return 300
    if token in TOKEN_LENGTH:
        return 301
    if CARD_TOKEN_BASE <= token < CARD_TOKEN_BASE + 108:
        return 302
    if WILD_TOKEN_BASE <= token < WILD_TOKEN_BASE + 52:
        return 303
    _reject("token_kind_unknown")


@dataclass(frozen=True, slots=True)
class ActionRow:
    values: tuple[int, int, int, int, int, int, int, int]
    session_id: str = ""
    state_version: int = 0
    generation: str = ""

    def __post_init__(self) -> None:
        if len(self.values) != ACTION_ROW_WIDTH:
            _reject("action_row_width_invalid")
        if self.values[0] != ROW_BEGIN or self.values[7] != ROW_END:
            _reject("action_row_frame_invalid")
        if self.values[4] != 304 or self.values[1] not in (2, 3, 4, 5):
            _reject("action_row_header_invalid")
        if not 263 <= self.values[2] <= 295:
            _reject("action_row_stage_invalid")
        if self.values[3] not in {296, 297, 298, 299, 300, 301, 302, 303}:
            _reject("action_row_kind_invalid")
        if not 0 <= self.values[5] <= 255 or self.values[6] not in (305, 306):
            _reject("action_row_payload_invalid")
        if self.values[3] != action_token_kind(self.values[5]):
            _reject("action_row_kind_invalid")
        if (self.values[6] == 306) != (self.values[5] in (PASS_TOKEN, COMMIT_TOKEN)):
            _reject("action_row_commit_invalid")
        if self.values[5] == PASS_TOKEN and self.values[1] != PHASE_TOKEN["FOLLOW_PLAY"]:
            _reject("action_row_pass_phase_invalid")

    @property
    def phase(self) -> str:
        return TOKEN_PHASE[self.values[1]]

    @property
    def prefix_stage(self) -> int:
        return self.values[2] - 263

    @property
    def token(self) -> int:
        return self.values[5]

    @property
    def is_commit(self) -> bool:
        return self.values[6] == 306

    @property
    def kind(self) -> str:
        return {296: "PASS", 297: "COMMIT", 298: "FAMILY", 299: "RANK",
                300: "SUIT", 301: "LENGTH", 302: "CARD", 303: "WILD_ASSIGNMENT"}[self.values[3]]

    @classmethod
    def for_token(cls, phase: str, prefix_stage: int, token: int, *, session_id: str = "",
                  state_version: int = 0, generation: str = "") -> "ActionRow":
        if phase not in PHASE_TOKEN:
            _reject("action_phase_invalid")
        _int(prefix_stage, 0, MAX_PREFIX_TOKENS, "prefix_stage_invalid")
        _int(token, 0, 255, "token_invalid")
        kind = action_token_kind(token)
        commit = token in (PASS_TOKEN, COMMIT_TOKEN)
        return cls((ROW_BEGIN, PHASE_TOKEN[phase], 263 + prefix_stage, kind, 304, token,
                    306 if commit else 305, ROW_END), session_id, state_version, generation)


@dataclass(frozen=True, slots=True)
class LegalActionRows:
    rows: tuple[ActionRow, ...]
    weights: tuple[float, ...]
    is_commit: tuple[bool, ...]
    phase: str
    prefix: tuple[int, ...]
    session_id: str = ""
    state_version: int = 0

    def validate(self) -> None:
        if len(self.rows) != len(self.weights) or len(self.rows) != len(self.is_commit):
            _reject("legal_rows_metadata_mismatch")
        if len(self.rows) > MAX_ACTION_ROWS:
            _reject("legal_rows_overflow")
        tokens = []
        for index, (row, weight, commit) in enumerate(zip(self.rows, self.weights, self.is_commit)):
            if (type(row) is not ActionRow or row.phase != self.phase
                    or row.prefix_stage != len(self.prefix)
                    or row.session_id != self.session_id
                    or row.state_version != self.state_version):
                _reject("legal_row_context_invalid")
            if (type(weight) not in (int, float) or weight != weight
                    or weight in (float("inf"), float("-inf")) or weight <= 0):
                _reject("legal_row_weight_invalid")
            if bool(commit) != row.is_commit:
                _reject("legal_row_commit_mismatch")
            tokens.append(row.token)
        if tokens != sorted(tokens) or len(tokens) != len(set(tokens)):
            _reject("legal_rows_nonunique_or_unstable")


@dataclass(frozen=True, slots=True)
class CommitIdentity:
    session_id: str
    state_version: int
    token_step: int
    token_digest: str

    def validate(self) -> None:
        if (not isinstance(self.session_id, str) or not self.session_id
                or len(self.session_id) > 128):
            _reject("commit_session_invalid")
        _int(self.state_version, 0, 2**31 - 1, "commit_state_version_invalid")
        _int(self.token_step, 1, 2**31 - 1, "commit_token_step_invalid")
        if (not isinstance(self.token_digest, str) or len(self.token_digest) != 64
                or any(char not in "0123456789abcdef" for char in self.token_digest)):
            _reject("commit_digest_invalid")

    @classmethod
    def from_tokens(cls, session_id: str, state_version: int, token_step: int,
                    tokens: Sequence[int]) -> "CommitIdentity":
        body = ",".join(str(token) for token in tokens).encode("ascii")
        result = cls(session_id, state_version, token_step, sha256(body).hexdigest())
        result.validate()
        return result


@dataclass(frozen=True, slots=True)
class DecodedAction:
    kind: Literal["pass", "play", "tribute", "return"]
    phase: str
    tokens: tuple[int, ...]
    physical_cards: tuple[int, ...]
    family: str | None
    declared_ranks: tuple[int, ...]
    comparison_rank: int | None
    declared_suit: str | None
    physical_length: int
    protocol_length: int | None
    storage_order: tuple[int, ...]
    action_order: tuple[int, ...]
    wild_assignments: tuple[WildAssignment, ...]
    declaration: ActionDeclaration | None
    commit_identity: CommitIdentity | None = None

    def validate(self) -> None:
        if self.phase not in PHASE_TOKEN:
            _reject("decoded_phase_invalid")
        if self.kind not in {"pass", "play", "tribute", "return"}:
            _reject("decoded_kind_invalid")
        if self.kind == "pass":
            if self.phase != "FOLLOW_PLAY" or self.tokens != (PASS_TOKEN,) or self.physical_cards:
                _reject("pass_semantics_invalid")
        elif self.kind in {"tribute", "return"}:
            if self.phase != ("TRIBUTE" if self.kind == "tribute" else "RETURN"):
                _reject("exchange_phase_invalid")
            if len(self.physical_cards) != 1 or self.tokens[-1] != COMMIT_TOKEN:
                _reject("exchange_semantics_invalid")
        else:
            if self.phase not in PLAY_PHASES or self.family not in FAMILIES or self.declaration is None:
                _reject("play_semantics_invalid")
            if type(self.declaration) is not ActionDeclaration:
                _reject("decoded_declaration_mismatch")
            declaration = self.declaration
            if (declaration.family != self.family
                    or declaration.declared_ranks != self.declared_ranks
                    or declaration.comparison_rank != self.comparison_rank
                    or declaration.suit != self.declared_suit
                    or declaration.length != self.physical_length
                    or declaration.protocol_length != self.protocol_length
                    or tuple(CardMapping.from_wire(card) for card in declaration.canonical_cards) != self.storage_order
                    or tuple(CardMapping.from_wire(card) for card in declaration.canonical_action_cards) != self.action_order
                    or declaration.wild_assignments != self.wild_assignments):
                _reject("decoded_declaration_mismatch")
        _tuple_ints(self.physical_cards, 0, 107, 27, unique=True, code="decoded_cards_invalid")
        _tuple_ints(self.storage_order, 0, 107, 27, unique=True, code="decoded_storage_order_invalid")
        _tuple_ints(self.action_order, 0, 107, 27, unique=True, code="decoded_action_order_invalid")
        if set(self.storage_order) != set(self.physical_cards) or set(self.action_order) != set(self.physical_cards):
            _reject("decoded_order_set_invalid")
        if self.storage_order != storage_order(self.physical_cards):
            _reject("decoded_storage_order_invalid")
        if self.physical_length != len(self.physical_cards):
            _reject("decoded_length_invalid")
        if self.commit_identity is not None:
            if type(self.commit_identity) is not CommitIdentity:
                _reject("commit_identity_invalid")
            self.commit_identity.validate()


__all__ = [name for name in globals() if not name.startswith("_")]
