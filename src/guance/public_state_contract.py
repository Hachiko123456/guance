"""Strict model-independent public+self Guandan state contract.

This pure-stdlib v2 contract is separate from CanonicalPublicGameView.v1.
Unknown, absent, and unsupported facts are never inferred from zero/False/[].
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Literal

CONTRACT = "CanonicalGuandanPublicState.v2"
SCHEMA_VERSION = 2
SCHEMA_REVISION = 1
RULESET = "tencent_daguandan.v1"
DECLARATION_SCHEMA = "guandan-declaration-v1"
DECLARATION_RANK_ENCODING = "guandan-rank-v1"
DECLARATION_RULESET = "GD-RULES-0.1"
DECLARATION_RULES_SEMANTIC_SHA256 = "96b1fdccf795af4e51361e8bd155d5e46c663d50ea1d1dda22a3a6021ce663dc"
OBSERVED_PROTOCOL = "tencent-live-observed.v1"
STRICT_OBSERVED_PROTOCOL = "TencentDaguandanObservedProtocol.v20261014"
MARVEL_PROTOCOL = "GD-ACTION-0.2-MARVEL|GD-ENV-0.2-MARVEL|GD-ENCODING-0.2-MARVEL"
Phase = Literal["TRIBUTE", "RETURN", "LEAD_PLAY", "FOLLOW_PLAY", "TERMINAL"]

_PHASES = frozenset({"TRIBUTE", "RETURN", "LEAD_PLAY", "FOLLOW_PLAY", "TERMINAL"})
_FAMILIES = frozenset({"SINGLE", "PAIR", "TRIPLE", "FULL_HOUSE", "STRAIGHT",
                       "PAIR_SEQUENCE", "TRIPLE_SEQUENCE", "RANK_BOMB",
                       "STRAIGHT_FLUSH", "FOUR_KINGS"})
_SUITS = frozenset({"CLUBS", "DIAMONDS", "HEARTS", "SPADES", "JOKER"})
_OUTCOMES = frozenset({"DOUBLE_DOWN", "HEAD_THIRD", "HEAD_LAST"})
_PHYSICAL_SUITS = {1: "SPADES", 2: "HEARTS", 3: "CLUBS", 4: "DIAMONDS"}
_COMPLEX_FAMILIES = frozenset({
    "FULL_HOUSE", "STRAIGHT", "PAIR_SEQUENCE", "TRIPLE_SEQUENCE",
    "STRAIGHT_FLUSH", "FOUR_KINGS",
})
_GD_TO_WIRE_RANK = {**{rank: rank for rank in range(3, 14)}, 14: 1, 15: 2, 16: 14, 17: 15}
_WIRE_TO_GD_RANK = {wire: rank for rank, wire in _GD_TO_WIRE_RANK.items()}
_GD_TO_WIRE_SUIT = {0: 3, 1: 4, 2: 2, 3: 1, 4: 0}
_WIRE_TO_GD_SUIT = {wire: suit for suit, wire in _GD_TO_WIRE_SUIT.items()}
_GD_SUIT_NAMES = {0: "CLUBS", 1: "DIAMONDS", 2: "HEARTS", 3: "SPADES", 4: "JOKER"}
_GD_PRINTED_RANK_ORDER = tuple(range(3, 18))
_GD_WINDOW_ORDER = (15, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14)
_WINDOWS_5 = tuple(_GD_WINDOW_ORDER[index:index + 5] for index in range(9)) + ((14, 15, 3, 4, 5),)
_WINDOWS_3 = tuple(_GD_WINDOW_ORDER[index:index + 3] for index in range(11)) + ((14, 15, 3),)
_WINDOWS_2 = tuple(_GD_WINDOW_ORDER[index:index + 2] for index in range(12)) + ((14, 15),)
_FOUR_KINGS_IDS = (52, 53, 106, 107)
_FOUR_KINGS_STORAGE_IDS = (52, 106, 53, 107)


class PublicStateContractError(ValueError):
    """Bounded validation error that never echoes untrusted input."""
    def __init__(self, code: str = "PUBLIC_STATE_INVALID") -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str = "PUBLIC_STATE_INVALID") -> None:
    raise PublicStateContractError(code)


def _int(value: object, low: int = 0, high: int = 2**63 - 1) -> int:
    if type(value) is not int or not low <= value <= high:
        _fail()
    return value


def _id(value: object) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= 128 or any(
        not (c.isalnum() or c in "._:-") for c in value
    ):
        _fail()
    return value


def _text(value: object, maximum: int = 96) -> str:
    if not isinstance(value, str) or len(value) > maximum or any(c < " " or c in "\r\n" for c in value):
        _fail()
    return value


def _code(value: object) -> str:
    """Validate a non-secret reason/provenance code, never arbitrary metadata."""
    if not isinstance(value, str) or not 0 < len(value) <= 80 or any(
        not ("a" <= c <= "z" or c.isdigit() or c in "._-") for c in value
    ):
        _fail()
    return value


def _dict(value: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        _fail()
    return value


def _card(value: object) -> int:
    code = _int(value, 0, 0x14D)
    suit, rank = (code >> 4) & 15, code & 15
    if not (suit in (1, 2, 3, 4) and 1 <= rank <= 13 or suit == 0 and rank in (14, 15)):
        _fail()
    return code


def guandan_rank_to_wire_rank(value: object) -> int:
    rank = _int(value, 3, 17)
    try:
        return _GD_TO_WIRE_RANK[rank]
    except KeyError:
        _fail()


def wire_rank_to_guandan_rank(value: object) -> int:
    rank = _int(value, 1, 15)
    try:
        return _WIRE_TO_GD_RANK[rank]
    except KeyError:
        _fail()


def guandan_suit_to_wire_suit(value: object) -> int:
    suit = _int(value, 0, 4)
    try:
        return _GD_TO_WIRE_SUIT[suit]
    except KeyError:
        _fail()


def wire_suit_to_guandan_suit(value: object) -> int:
    suit = _int(value, 0, 4)
    try:
        return _WIRE_TO_GD_SUIT[suit]
    except KeyError:
        _fail()


def guandan_card_id_to_wire(value: object) -> int:
    card_id = _int(value, 0, 107)
    deck, offset = divmod(card_id, 54)
    if offset < 52:
        rank = 3 + offset // 4
        suit = offset % 4
    else:
        rank = 16 + (offset - 52)
        suit = 4
    return (deck << 8) | (guandan_suit_to_wire_suit(suit) << 4) | guandan_rank_to_wire_rank(rank)


def wire_card_to_guandan_id(value: object) -> int:
    card = _card(value)
    deck = card >> 8
    wire_suit, wire_rank = (card >> 4) & 15, card & 15
    if deck not in (0, 1):
        _fail()
    rank = wire_rank_to_guandan_rank(wire_rank)
    suit = wire_suit_to_guandan_suit(wire_suit)
    if suit == 4:
        if rank not in (16, 17):
            _fail()
        offset = 52 + (rank - 16)
    else:
        if rank not in range(3, 16):
            _fail()
        offset = (rank - 3) * 4 + suit
    return deck * 54 + offset


def _seq(value: object, decoder: Callable[[object], Any], maximum: int,
         *, unique: bool = False) -> tuple[Any, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > maximum:
        _fail()
    result = tuple(decoder(item) for item in value)
    if unique and len(result) != len(set(result)):
        _fail()
    return result


def _cards(value: object, maximum: int = 28) -> tuple[int, ...]:
    return _seq(value, _card, maximum, unique=True)


def _seat(value: object) -> int:
    return _int(value, 0, 3)


def _bool(value: object) -> bool:
    if type(value) is not bool:
        _fail()
    return value


def _phase(value: object) -> str:
    if type(value) is not str or value not in _PHASES:
        _fail()
    return value


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    source: str
    connection_id: str = ""
    sequence: int | None = None
    message_index: int | None = None
    sub_index: int | None = None
    derivation: str = ""

    def validate(self) -> None:
        _code(self.source); _text(self.connection_id)
        if self.connection_id and not (
            self.connection_id.startswith("local-")
            and len(self.connection_id) == 30
            and all(c in "0123456789abcdef" for c in self.connection_id[6:])
        ):
            _fail()
        for value in (self.sequence, self.message_index, self.sub_index):
            if value is not None: _int(value)
        if self.derivation: _code(self.derivation)

    def to_dict(self) -> dict[str, Any]:
        if type(self) is not EvidenceRef: _fail()
        self.validate()
        return {"source": self.source, "connection_id": self.connection_id,
                "sequence": self.sequence, "message_index": self.message_index,
                "sub_index": self.sub_index, "derivation": self.derivation}

    @classmethod
    def from_dict(cls, value: object) -> "EvidenceRef":
        data = _dict(value, {"source", "connection_id", "sequence", "message_index",
                             "sub_index", "derivation"})
        result = cls(**data); result.validate(); return result


class KnowledgeStatus(str, Enum):
    KNOWN = "known"
    ABSENT = "absent"
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class Knowledge:
    status: KnowledgeStatus
    value: object = None
    reason: str = ""
    evidence: EvidenceRef | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, KnowledgeStatus):
            try:
                object.__setattr__(self, "status", KnowledgeStatus(str(self.status)))
            except ValueError:
                raise PublicStateContractError() from None
        if self.status is KnowledgeStatus.KNOWN:
            if self.value is None or self.reason or type(self.evidence) is not EvidenceRef: _fail()
            self.evidence.validate()
        elif self.status is KnowledgeStatus.ABSENT:
            if self.value is not None or self.reason or type(self.evidence) is not EvidenceRef: _fail()
            self.evidence.validate()
        elif self.value is not None or self.evidence is not None or not self.reason:
            _fail()
        if self.reason: _code(self.reason)

    @classmethod
    def known(cls, value: object, evidence: EvidenceRef) -> "Knowledge":
        return cls(KnowledgeStatus.KNOWN, value=value, evidence=evidence)
    @classmethod
    def absent(cls, evidence: EvidenceRef) -> "Knowledge":
        return cls(KnowledgeStatus.ABSENT, evidence=evidence)
    @classmethod
    def unknown(cls, reason: str) -> "Knowledge":
        return cls(KnowledgeStatus.UNKNOWN, reason=reason)
    @classmethod
    def unsupported(cls, reason: str) -> "Knowledge":
        return cls(KnowledgeStatus.UNSUPPORTED, reason=reason)


def _k_to(value: Knowledge, encoder: Callable[[object], Any]) -> dict[str, Any]:
    if type(value) is not Knowledge: _fail()
    if value.status is KnowledgeStatus.KNOWN:
        assert value.evidence is not None
        return {"status": "known", "value": encoder(value.value),
                "evidence": value.evidence.to_dict()}
    if value.status is KnowledgeStatus.ABSENT:
        assert value.evidence is not None
        return {"status": "absent", "evidence": value.evidence.to_dict()}
    return {"status": value.status.value, "reason": value.reason}


def _k_from(value: object, decoder: Callable[[object], object]) -> Knowledge:
    if not isinstance(value, dict) or type(value.get("status")) is not str: _fail()
    try:
        status = KnowledgeStatus(value["status"])
    except ValueError:
        raise PublicStateContractError() from None
    if status is KnowledgeStatus.KNOWN:
        data = _dict(value, {"status", "value", "evidence"})
        return Knowledge.known(decoder(data["value"]), EvidenceRef.from_dict(data["evidence"]))
    if status is KnowledgeStatus.ABSENT:
        data = _dict(value, {"status", "evidence"})
        return Knowledge.absent(EvidenceRef.from_dict(data["evidence"]))
    data = _dict(value, {"status", "reason"})
    return Knowledge(status, reason=data["reason"])


@dataclass(frozen=True, slots=True)
class StateIdentity:
    session_id: str
    game_id: str
    state_version: int
    public_state_revision: int

    def validate(self) -> None:
        _id(self.session_id); _id(self.game_id); _int(self.state_version); _int(self.public_state_revision)
    def to_dict(self) -> dict[str, Any]:
        if type(self) is not StateIdentity: _fail()
        self.validate(); return {"session_id": self.session_id, "game_id": self.game_id,
                                "state_version": self.state_version,
                                "public_state_revision": self.public_state_revision}
    @classmethod
    def from_dict(cls, value: object) -> "StateIdentity":
        data = _dict(value, {"session_id", "game_id", "state_version", "public_state_revision"})
        result = cls(**data); result.validate(); return result


@dataclass(frozen=True, slots=True)
class WildAssignment:
    physical_card: int
    assigned_rank: int
    assigned_suit: str
    substituted: bool
    def validate(self) -> None:
        _card(self.physical_card); _int(self.assigned_rank, 3, 15); _bool(self.substituted)
        if type(self.assigned_suit) is not str or self.assigned_suit not in (_SUITS - {"JOKER"}):
            _fail()
    def to_dict(self) -> dict[str, Any]:
        if type(self) is not WildAssignment: _fail()
        self.validate(); return {"physical_card": self.physical_card,
                                "assigned_rank": self.assigned_rank,
                                "assigned_suit": self.assigned_suit,
                                "substituted": self.substituted}
    @classmethod
    def from_dict(cls, value: object) -> "WildAssignment":
        data = _dict(value, {"physical_card", "assigned_rank", "assigned_suit", "substituted"})
        result = cls(**data); result.validate(); return result


@dataclass(frozen=True, slots=True)
class PublicDeclaration:
    family: str
    declared_ranks: tuple[int, ...]
    comparison_rank: int
    suit: str | None
    length: int
    canonical_cards: tuple[int, ...]
    wild_assignments: tuple[WildAssignment, ...] = ()
    canonical_action_cards: tuple[int, ...] = ()
    protocol_length: int | None = None
    declaration_schema: str = DECLARATION_SCHEMA
    rank_encoding: str = DECLARATION_RANK_ENCODING
    declaration_ruleset: str = DECLARATION_RULESET
    rules_semantic_sha256: str = DECLARATION_RULES_SEMANTIC_SHA256

    @staticmethod
    def _storage_key(card: int) -> tuple[int, int, int, int]:
        card_id = wire_card_to_guandan_id(card)
        deck, offset = divmod(card_id, 54)
        if offset < 52:
            rank, suit = 3 + offset // 4, offset % 4
        else:
            rank, suit = 16 + (offset - 52), 4
        return (_GD_PRINTED_RANK_ORDER.index(rank), suit, deck, card_id)

    def _effective_faces(self, cards: tuple[int, ...], assignments: tuple[WildAssignment, ...]) -> tuple[tuple[int, str, int], ...]:
        by_card = {item.physical_card: item for item in assignments}
        result: list[tuple[int, str, int]] = []
        for card in cards:
            assignment = by_card.get(card)
            card_id = wire_card_to_guandan_id(card)
            if assignment is not None:
                result.append((assignment.assigned_rank, assignment.assigned_suit, card_id))
            else:
                wire_suit, wire_rank = (card >> 4) & 15, card & 15
                gd_rank = wire_rank_to_guandan_rank(wire_rank)
                gd_suit = wire_suit_to_guandan_suit(wire_suit)
                result.append((gd_rank, _GD_SUIT_NAMES[gd_suit], card_id))
        return tuple(result)

    @staticmethod
    def _window_for_ranks(ranks: tuple[int, ...], windows: tuple[tuple[int, ...], ...]) -> tuple[int, ...]:
        matches = [window for window in windows if Counter(window) == Counter(ranks)]
        if len(matches) != 1:
            _fail()
        return matches[0]

    @staticmethod
    def _ordered_cards_for_ranks(faces: tuple[tuple[int, str, int], ...], rank_order: tuple[int, ...]) -> tuple[int, ...]:
        by_rank: dict[int, list[int]] = {rank: [] for rank in rank_order}
        for rank, _suit, card_id in faces:
            if rank not in by_rank:
                _fail()
            by_rank[rank].append(card_id)
        ordered_ids = tuple(card_id for rank in rank_order for card_id in sorted(by_rank[rank]))
        return tuple(guandan_card_id_to_wire(card_id) for card_id in ordered_ids)

    def validate(self) -> None:
        if (self.declaration_schema != DECLARATION_SCHEMA or self.rank_encoding != DECLARATION_RANK_ENCODING
                or self.declaration_ruleset != DECLARATION_RULESET
                or self.rules_semantic_sha256 != DECLARATION_RULES_SEMANTIC_SHA256):
            _fail("PUBLIC_STATE_UNSUPPORTED")
        if type(self.family) is not str or self.family not in _FAMILIES:
            _fail()
        if (type(self.declared_ranks) is not tuple or type(self.canonical_cards) is not tuple
                or type(self.canonical_action_cards) is not tuple or type(self.wild_assignments) is not tuple):
            _fail()
        if self.suit is not None and (type(self.suit) is not str or self.suit not in _SUITS):
            _fail()
        ranks = _seq(self.declared_ranks, lambda x: _int(x, 3, 17), 8)
        cards = _cards(self.canonical_cards)
        action_cards = _cards(self.canonical_action_cards)
        if set(cards) != set(action_cards):
            _fail()
        comparison = _int(self.comparison_rank, 3, 17)
        _int(self.length, 1, 27)
        if self.protocol_length is not None:
            _int(self.protocol_length, 0, 10)
        if self.length != len(cards):
            _fail()
        assignments = _seq(self.wild_assignments, lambda x: x if type(x) is WildAssignment else _fail(), 4)
        for item in assignments:
            item.validate()
            if ((item.physical_card >> 4) & 15) == 0:
                _fail()
        assignment_ids = tuple(wire_card_to_guandan_id(item.physical_card) for item in assignments)
        if (assignment_ids != tuple(sorted(assignment_ids)) or len(assignment_ids) != len(set(assignment_ids))
                or not set(item.physical_card for item in assignments) <= set(cards)):
            _fail()
        faces = self._effective_faces(cards, assignments)
        effective_ranks = tuple(rank for rank, _suit, _card_id in faces)
        effective_suits = tuple(suit for _rank, suit, _card_id in faces)
        counts = Counter(effective_ranks)

        if self.family in {"SINGLE", "PAIR", "TRIPLE", "RANK_BOMB"}:
            if self.suit is not None or self.protocol_length is not None or len(ranks) != 1:
                _fail()
            expected_length = {"SINGLE": 1, "PAIR": 2, "TRIPLE": 3}.get(self.family)
            if expected_length is not None and self.length != expected_length:
                _fail()
            if self.family == "RANK_BOMB" and not 4 <= self.length <= 10:
                _fail()
            if len(counts) != 1:
                _fail()
            effective_rank = effective_ranks[0]
            if ranks != (effective_rank,) or comparison != effective_rank or action_cards != cards:
                _fail()
            return

        if len(assignments) > 2 or tuple(cards) != tuple(sorted(cards, key=self._storage_key)):
            _fail()
        if self.family == "FULL_HOUSE":
            if self.length != 5 or self.suit is not None or self.protocol_length is not None:
                _fail()
            if any(rank in (16, 17) for rank in counts) or sorted(counts.values()) != [2, 3]:
                _fail()
            triple_rank = next(rank for rank, count in counts.items() if count == 3)
            pair_rank = next(rank for rank, count in counts.items() if count == 2)
            if ranks != (triple_rank, pair_rank) or comparison != triple_rank:
                _fail()
            if action_cards != self._ordered_cards_for_ranks(faces, (triple_rank, pair_rank)):
                _fail()
        elif self.family in {"STRAIGHT", "STRAIGHT_FLUSH"}:
            if self.length != 5 or self.protocol_length is not None or len(counts) != 5:
                _fail()
            window = self._window_for_ranks(effective_ranks, _WINDOWS_5)
            if ranks != (window[0],) or comparison != window[-1]:
                _fail()
            if self.family == "STRAIGHT":
                if self.suit is not None:
                    _fail()
            elif (self.suit not in (_SUITS - {"JOKER"}) or len(set(effective_suits)) != 1
                  or effective_suits[0] != self.suit):
                _fail()
            if action_cards != self._ordered_cards_for_ranks(faces, window):
                _fail()
        elif self.family == "PAIR_SEQUENCE":
            if self.length != 6 or self.suit is not None or self.protocol_length != 3 or set(counts.values()) != {2}:
                _fail()
            window = self._window_for_ranks(tuple(counts), _WINDOWS_3)
            if ranks != (window[0],) or comparison != window[-1] or action_cards != self._ordered_cards_for_ranks(faces, window):
                _fail()
        elif self.family == "TRIPLE_SEQUENCE":
            if self.length != 6 or self.suit is not None or self.protocol_length != 2 or set(counts.values()) != {3}:
                _fail()
            window = self._window_for_ranks(tuple(counts), _WINDOWS_2)
            if ranks != (window[0],) or comparison != window[-1] or action_cards != self._ordered_cards_for_ranks(faces, window):
                _fail()
        elif self.family == "FOUR_KINGS":
            if (self.length != 4 or ranks or comparison != 17 or self.suit is not None
                    or self.protocol_length is not None or assignments):
                _fail()
            storage = tuple(guandan_card_id_to_wire(card_id) for card_id in _FOUR_KINGS_STORAGE_IDS)
            action = tuple(guandan_card_id_to_wire(card_id) for card_id in _FOUR_KINGS_IDS)
            if cards != storage or action_cards != action:
                _fail()
        else:
            _fail()

    def validate_for_level(self, wire_level: object) -> None:
        self.validate()
        level_wire = _int(wire_level, 1, 13)
        level_rank = wire_rank_to_guandan_rank(level_wire)
        assignments = {item.physical_card: item for item in self.wild_assignments}
        wild_cards = {card for card in self.canonical_cards
                      if ((card >> 4) & 15) == 2 and (card & 15) == level_wire}
        if set(assignments) != wild_cards:
            _fail()
        if self.family == "FOUR_KINGS":
            if assignments:
                _fail()
            return
        for item in self.wild_assignments:
            natural = item.assigned_rank == level_rank and item.assigned_suit == "HEARTS"
            if self.family == "STRAIGHT_FLUSH":
                if item.assigned_suit != self.suit:
                    _fail()
                expected_substituted = not natural
            else:
                if not natural and item.assigned_suit != "CLUBS":
                    _fail()
                expected_substituted = not natural
            if item.substituted != expected_substituted:
                _fail()
        if self.family == "FULL_HOUSE":
            faces = self._effective_faces(self.canonical_cards, self.wild_assignments)
            triple_rank = self.declared_ranks[0]
            assigned_ids = {wire_card_to_guandan_id(card) for card in assignments}
            if not any(rank == triple_rank and card_id not in assigned_ids for rank, _suit, card_id in faces):
                _fail()

    def to_dict(self) -> dict[str, Any]:
        if type(self) is not PublicDeclaration:
            _fail()
        self.validate()
        return {
            "declaration_schema": self.declaration_schema,
            "rank_encoding": self.rank_encoding,
            "declaration_ruleset": self.declaration_ruleset,
            "rules_semantic_sha256": self.rules_semantic_sha256,
            "family": self.family,
            "declared_ranks": list(self.declared_ranks),
            "comparison_rank": self.comparison_rank,
            "suit": self.suit,
            "length": self.length,
            "protocol_length": self.protocol_length,
            "canonical_cards": list(self.canonical_cards),
            "canonical_action_cards": list(self.canonical_action_cards),
            "wild_assignments": [item.to_dict() for item in self.wild_assignments],
        }

    @classmethod
    def from_dict(cls, value: object) -> "PublicDeclaration":
        if isinstance(value, dict) and "declaration_schema" not in value:
            _fail("PUBLIC_STATE_UNSUPPORTED")
        data = _dict(value, {
            "declaration_schema", "rank_encoding", "declaration_ruleset", "rules_semantic_sha256",
            "family", "declared_ranks", "comparison_rank", "suit", "length", "protocol_length",
            "canonical_cards", "canonical_action_cards", "wild_assignments",
        })
        if (not isinstance(data["declared_ranks"], list) or not isinstance(data["canonical_cards"], list)
                or not isinstance(data["canonical_action_cards"], list)
                or not isinstance(data["wild_assignments"], list)):
            _fail()
        result = cls(
            family=data["family"],
            declared_ranks=_seq(data["declared_ranks"], lambda x: _int(x, 3, 17), 8),
            comparison_rank=data["comparison_rank"], suit=data["suit"], length=data["length"],
            canonical_cards=_cards(data["canonical_cards"]),
            wild_assignments=_seq(data["wild_assignments"], WildAssignment.from_dict, 4),
            canonical_action_cards=_cards(data["canonical_action_cards"]),
            protocol_length=data["protocol_length"], declaration_schema=data["declaration_schema"],
            rank_encoding=data["rank_encoding"], declaration_ruleset=data["declaration_ruleset"],
            rules_semantic_sha256=data["rules_semantic_sha256"],
        )
        result.validate()
        return result

@dataclass(frozen=True, slots=True)
class CommittedActionV2:
    index: int
    actor: int
    phase: Phase
    kind: Literal["play", "pass", "tribute", "return"]
    cards: tuple[int, ...]
    declaration: Knowledge
    next_player: Knowledge
    remaining_after: Knowledge
    evidence: EvidenceRef

    def validate(self) -> None:
        _int(self.index, 1, 10000); _seat(self.actor); _phase(self.phase)
        if self.kind not in {"play", "pass", "tribute", "return"}: _fail()
        if self.kind == "play" and self.phase not in {"LEAD_PLAY", "FOLLOW_PLAY"}: _fail()
        if self.kind == "pass" and self.phase != "FOLLOW_PLAY": _fail()
        if self.kind == "tribute" and self.phase != "TRIBUTE": _fail()
        if self.kind == "return" and self.phase != "RETURN": _fail()
        cards = _cards(self.cards)
        if self.kind == "pass" and cards or self.kind != "pass" and not cards: _fail()
        if any(type(x) is not Knowledge for x in (self.declaration, self.next_player, self.remaining_after)): _fail()
        if type(self.evidence) is not EvidenceRef: _fail()
        self.evidence.validate()
        if self.kind in {"pass", "tribute", "return"} and self.declaration.status is not KnowledgeStatus.ABSENT: _fail()
        if self.kind == "play" and self.declaration.status is KnowledgeStatus.ABSENT: _fail()
        if self.declaration.status is KnowledgeStatus.KNOWN:
            if type(self.declaration.value) is not PublicDeclaration: _fail()
            self.declaration.value.validate()
            if self.declaration.value.canonical_cards != cards: _fail()
        if self.next_player.status is KnowledgeStatus.KNOWN: _seat(self.next_player.value)
        if self.remaining_after.status is KnowledgeStatus.KNOWN: _int(self.remaining_after.value, 0, 28)

    def to_dict(self) -> dict[str, Any]:
        if type(self) is not CommittedActionV2: _fail()
        self.validate(); return {"index": self.index, "actor": self.actor, "phase": self.phase,
                                "kind": self.kind, "cards": list(self.cards),
                                "declaration": _k_to(self.declaration, _decl_to),
                                "next_player": _k_to(self.next_player, _seat),
                                "remaining_after": _k_to(self.remaining_after, lambda x: _int(x, 0, 28)),
                                "evidence": self.evidence.to_dict()}
    @classmethod
    def from_dict(cls, value: object) -> "CommittedActionV2":
        data = _dict(value, {"index", "actor", "phase", "kind", "cards", "declaration",
                             "next_player", "remaining_after", "evidence"})
        result = cls(data["index"], data["actor"], data["phase"], data["kind"], _cards(data["cards"]),
                     _k_from(data["declaration"], PublicDeclaration.from_dict),
                     _k_from(data["next_player"], _seat),
                     _k_from(data["remaining_after"], lambda x: _int(x, 0, 28)),
                     EvidenceRef.from_dict(data["evidence"]))
        result.validate(); return result


def _decl_to(value: object) -> dict[str, Any]:
    if type(value) is not PublicDeclaration: _fail()
    return value.to_dict()


@dataclass(frozen=True, slots=True)
class CurrentWinning:
    actor: int
    cards: tuple[int, ...]
    declaration: Knowledge
    def validate(self) -> None:
        _seat(self.actor); cards = _cards(self.cards)
        if not cards or type(self.declaration) is not Knowledge: _fail()
        if self.declaration.status is KnowledgeStatus.KNOWN:
            if type(self.declaration.value) is not PublicDeclaration: _fail()
            self.declaration.value.validate()
            if self.declaration.value.canonical_cards != cards: _fail()
    def to_dict(self) -> dict[str, Any]:
        if type(self) is not CurrentWinning: _fail()
        self.validate(); return {"actor": self.actor, "cards": list(self.cards), "size": len(self.cards),
                                "declaration": _k_to(self.declaration, _decl_to)}
    @classmethod
    def from_dict(cls, value: object) -> "CurrentWinning":
        data = _dict(value, {"actor", "cards", "size", "declaration"}); cards = _cards(data["cards"])
        if _int(data["size"], 1, 27) != len(cards): _fail()
        result = cls(data["actor"], cards, _k_from(data["declaration"], PublicDeclaration.from_dict))
        result.validate(); return result


@dataclass(frozen=True, slots=True)
class PreviousResultSummary:
    source_game_id: Knowledge
    ranking_by_seat: tuple[int, int, int, int]
    winner_team: int
    outcome_class: str
    tribute_cancelled: Knowledge

    def validate(self) -> None:
        ranks = _seq(self.ranking_by_seat, lambda x: _int(x, 1, 4), 4, unique=True)
        if len(ranks) != 4 or type(self.source_game_id) is not Knowledge or type(self.tribute_cancelled) is not Knowledge: _fail()
        team = _int(self.winner_team, 0, 1)
        if self.outcome_class not in _OUTCOMES or ranks.index(1) % 2 != team: _fail()
        mate_rank = 2 if ranks.index(2) % 2 == team else 3 if ranks.index(3) % 2 == team else 4
        if self.outcome_class != {2: "DOUBLE_DOWN", 3: "HEAD_THIRD", 4: "HEAD_LAST"}[mate_rank]: _fail()
        if self.source_game_id.status is KnowledgeStatus.KNOWN: _id(self.source_game_id.value)
        if self.tribute_cancelled.status is KnowledgeStatus.KNOWN: _bool(self.tribute_cancelled.value)

    def to_dict(self) -> dict[str, Any]:
        if type(self) is not PreviousResultSummary: _fail()
        self.validate(); return {"source_game_id": _k_to(self.source_game_id, _id),
                                "ranking_by_seat": list(self.ranking_by_seat),
                                "winner_team": self.winner_team, "outcome_class": self.outcome_class,
                                "tribute_cancelled": _k_to(self.tribute_cancelled, _bool)}
    @classmethod
    def from_dict(cls, value: object) -> "PreviousResultSummary":
        data = _dict(value, {"source_game_id", "ranking_by_seat", "winner_team",
                             "outcome_class", "tribute_cancelled"})
        ranks = _seq(data["ranking_by_seat"], lambda x: _int(x, 1, 4), 4, unique=True)
        if len(ranks) != 4: _fail()
        result = cls(_k_from(data["source_game_id"], _id), ranks, data["winner_team"],
                     data["outcome_class"], _k_from(data["tribute_cancelled"], _bool))
        result.validate(); return result


@dataclass(frozen=True, slots=True)
class ExchangeTransfer:
    kind: Literal["tribute", "return"]
    source_seat: int
    target_seat: int
    card: int
    def validate(self) -> None:
        if self.kind not in {"tribute", "return"}: _fail()
        if _seat(self.source_seat) == _seat(self.target_seat): _fail()
        _card(self.card)
    def to_dict(self) -> dict[str, Any]:
        if type(self) is not ExchangeTransfer: _fail()
        self.validate(); return {"kind": self.kind, "source_seat": self.source_seat,
                                "target_seat": self.target_seat, "card": self.card}
    @classmethod
    def from_dict(cls, value: object) -> "ExchangeTransfer":
        data = _dict(value, {"kind", "source_seat", "target_seat", "card"})
        result = cls(**data); result.validate(); return result


def _seats(value: object) -> tuple[int, ...]:
    return _seq(value, _seat, 4, unique=True)


def _pairs(value: object) -> tuple[tuple[int, int], ...]:
    def one(item: object) -> tuple[int, int]:
        pair = _seq(item, _seat, 2)
        if len(pair) != 2 or pair[0] == pair[1]: _fail()
        return pair
    return _seq(value, one, 4, unique=True)


def _transfers(value: object) -> tuple[ExchangeTransfer, ...]:
    return _seq(value, lambda x: x if type(x) is ExchangeTransfer else ExchangeTransfer.from_dict(x), 8)


@dataclass(frozen=True, slots=True)
class ExchangeState:
    tribute_donors: Knowledge
    tribute_order: Knowledge
    anti_tribute: Knowledge
    return_obligations: Knowledge
    resolved_transfers: Knowledge
    self_selection: Knowledge

    def validate(self) -> None:
        facts = (self.tribute_donors, self.tribute_order, self.anti_tribute,
                 self.return_obligations, self.resolved_transfers, self.self_selection)
        if any(type(x) is not Knowledge for x in facts): _fail()
        if self.tribute_donors.status is KnowledgeStatus.KNOWN: _seats(self.tribute_donors.value)
        if self.tribute_order.status is KnowledgeStatus.KNOWN: _seats(self.tribute_order.value)
        if self.anti_tribute.status is KnowledgeStatus.KNOWN: _bool(self.anti_tribute.value)
        if self.return_obligations.status is KnowledgeStatus.KNOWN: _pairs(self.return_obligations.value)
        if (self.tribute_donors.status is KnowledgeStatus.KNOWN
                and self.tribute_order.status is KnowledgeStatus.KNOWN
                and set(_seats(self.tribute_donors.value)) != set(_seats(self.tribute_order.value))):
            _fail()
        if self.resolved_transfers.status is KnowledgeStatus.KNOWN:
            transfers = _transfers(self.resolved_transfers.value)
            for item in transfers: item.validate()
            seen_kind_cards: set[tuple[str, int]] = set()
            locations: dict[int, int] = {}
            for item in transfers:
                key = (item.kind, item.card)
                if key in seen_kind_cards: _fail()
                seen_kind_cards.add(key)
                if item.card in locations and locations[item.card] != item.source_seat: _fail()
                locations[item.card] = item.target_seat
            if self.tribute_donors.status is KnowledgeStatus.KNOWN:
                donors = set(_seats(self.tribute_donors.value))
                if any(item.kind == "tribute" and item.source_seat not in donors
                       for item in transfers): _fail()
            if self.return_obligations.status is KnowledgeStatus.KNOWN:
                obligations = set(_pairs(self.return_obligations.value))
                if any(item.kind == "return"
                       and (item.source_seat, item.target_seat) not in obligations
                       for item in transfers): _fail()
            if (self.anti_tribute.status is KnowledgeStatus.KNOWN
                    and self.anti_tribute.value is True
                    and any(item.kind == "tribute" for item in transfers)):
                _fail()
        if self.self_selection.status is KnowledgeStatus.KNOWN: _cards(self.self_selection.value)

    def to_dict(self) -> dict[str, Any]:
        if type(self) is not ExchangeState: _fail()
        self.validate(); return {"tribute_donors": _k_to(self.tribute_donors, lambda x: list(_seats(x))),
                                "tribute_order": _k_to(self.tribute_order, lambda x: list(_seats(x))),
                                "anti_tribute": _k_to(self.anti_tribute, _bool),
                                "return_obligations": _k_to(self.return_obligations, lambda x: [list(p) for p in _pairs(x)]),
                                "resolved_transfers": _k_to(self.resolved_transfers, lambda x: [t.to_dict() for t in _transfers(x)]),
                                "self_selection": _k_to(self.self_selection, lambda x: list(_cards(x)))}
    @classmethod
    def from_dict(cls, value: object) -> "ExchangeState":
        data = _dict(value, {"tribute_donors", "tribute_order", "anti_tribute",
                             "return_obligations", "resolved_transfers", "self_selection"})
        result = cls(_k_from(data["tribute_donors"], _seats), _k_from(data["tribute_order"], _seats),
                     _k_from(data["anti_tribute"], _bool), _k_from(data["return_obligations"], _pairs),
                     _k_from(data["resolved_transfers"], lambda x: _transfers(x)),
                     _k_from(data["self_selection"], _cards))
        result.validate(); return result


def unsupported_exchange(reason: str = "exchange_protocol_unverified") -> ExchangeState:
    return ExchangeState(*(Knowledge.unsupported(reason) for _ in range(6)))


@dataclass(frozen=True, slots=True)
class HistoryEvidence:
    actions: tuple[CommittedActionV2, ...]
    observed_action_count: int
    public_history_complete: Knowledge
    self_private_history_complete: Knowledge
    transport_contiguous: Knowledge
    token_step: Knowledge
    committed_step: Knowledge

    def validate(self) -> None:
        actions = _seq(self.actions, lambda x: x if type(x) is CommittedActionV2 else _fail(), 10000)
        if _int(self.observed_action_count, 0, 10000) != len(actions): _fail()
        for expected, action in enumerate(actions, 1):
            action.validate()
            if action.index != expected: _fail()
        for fact in (self.public_history_complete, self.self_private_history_complete, self.transport_contiguous):
            if type(fact) is not Knowledge: _fail()
            if fact.status is KnowledgeStatus.KNOWN:
                _bool(fact.value)
                if fact.value is True and (fact.evidence is None
                        or fact.evidence.source != "semantic.exact_history"
                        or fact.evidence.derivation != "confirmed_transport_chain"):
                    _fail("PUBLIC_STATE_UNSUPPORTED")
        for fact in (self.token_step, self.committed_step):
            if type(fact) is not Knowledge: _fail()
            if fact.status is KnowledgeStatus.KNOWN: _int(fact.value, 0, 2**31 - 1)

    def to_dict(self) -> dict[str, Any]:
        if type(self) is not HistoryEvidence: _fail()
        self.validate(); return {"actions": [x.to_dict() for x in self.actions],
                                "observed_action_count": self.observed_action_count,
                                "public_history_complete": _k_to(self.public_history_complete, _bool),
                                "self_private_history_complete": _k_to(self.self_private_history_complete, _bool),
                                "transport_contiguous": _k_to(self.transport_contiguous, _bool),
                                "token_step": _k_to(self.token_step, lambda x: _int(x, 0, 2**31 - 1)),
                                "committed_step": _k_to(self.committed_step, lambda x: _int(x, 0, 2**31 - 1))}
    @classmethod
    def from_dict(cls, value: object) -> "HistoryEvidence":
        data = _dict(value, {"actions", "observed_action_count", "public_history_complete",
                             "self_private_history_complete", "transport_contiguous",
                             "token_step", "committed_step"})
        result = cls(_seq(data["actions"], CommittedActionV2.from_dict, 10000), data["observed_action_count"],
                     _k_from(data["public_history_complete"], _bool),
                     _k_from(data["self_private_history_complete"], _bool),
                     _k_from(data["transport_contiguous"], _bool),
                     _k_from(data["token_step"], lambda x: _int(x, 0, 2**31 - 1)),
                     _k_from(data["committed_step"], lambda x: _int(x, 0, 2**31 - 1)))
        result.validate(); return result

@dataclass(frozen=True, slots=True)
class CanonicalGuandanPublicState:
    identity: StateIdentity
    ruleset_compatibility: Knowledge
    observer_seat: Knowledge
    active_seat: Knowledge
    leader_seat: Knowledge
    current_winner_seat: Knowledge
    phase: Knowledge
    level: Knowledge
    own_hand: Knowledge
    remaining_counts: Knowledge
    current_winning: Knowledge
    consecutive_passes: Knowledge
    emptied_seats: Knowledge
    finishing_ranks: tuple[Knowledge, Knowledge, Knowledge, Knowledge]
    previous_result: Knowledge
    exchange: ExchangeState
    history: HistoryEvidence
    unknown_reasons: tuple[str, ...] = ()
    unsupported_reasons: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION
    schema_revision: int = SCHEMA_REVISION
    contract: str = CONTRACT
    ruleset_id: str = RULESET
    observed_protocol: str = OBSERVED_PROTOCOL
    marvel_protocol: str = MARVEL_PROTOCOL
    visibility: str = "public_plus_self"

    def validate(self) -> None:
        if (type(self.identity) is not StateIdentity or type(self.exchange) is not ExchangeState
                or type(self.history) is not HistoryEvidence
                or type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION
                or type(self.schema_revision) is not int or self.schema_revision != SCHEMA_REVISION
                or self.contract != CONTRACT or self.ruleset_id != RULESET
                or self.observed_protocol not in {OBSERVED_PROTOCOL, STRICT_OBSERVED_PROTOCOL}
                or self.marvel_protocol != MARVEL_PROTOCOL
                or self.visibility != "public_plus_self"):
            _fail("PUBLIC_STATE_UNSUPPORTED")
        self.identity.validate(); self.exchange.validate(); self.history.validate()
        facts = (self.ruleset_compatibility, self.observer_seat, self.active_seat,
                 self.leader_seat, self.current_winner_seat, self.phase, self.level,
                 self.own_hand, self.remaining_counts, self.current_winning,
                 self.consecutive_passes, self.emptied_seats, self.previous_result)
        if any(type(x) is not Knowledge for x in facts): _fail()
        ranks = _seq(self.finishing_ranks, lambda x: x if type(x) is Knowledge else _fail(), 4)
        if len(ranks) != 4: _fail()
        for fact in (self.observer_seat, self.active_seat, self.leader_seat, self.current_winner_seat):
            if fact.status is KnowledgeStatus.KNOWN: _seat(fact.value)
        if self.phase.status is KnowledgeStatus.KNOWN: _phase(self.phase.value)
        if self.level.status is KnowledgeStatus.KNOWN: _int(self.level.value, 1, 13)
        if self.own_hand.status is KnowledgeStatus.KNOWN: _cards(self.own_hand.value)
        if self.remaining_counts.status is KnowledgeStatus.KNOWN:
            counts = _seq(self.remaining_counts.value, lambda x: _int(x, 0, 28), 4)
            if len(counts) != 4: _fail()
        if self.current_winning.status is KnowledgeStatus.KNOWN:
            if type(self.current_winning.value) is not CurrentWinning: _fail()
            self.current_winning.value.validate()
        if self.consecutive_passes.status is KnowledgeStatus.KNOWN: _int(self.consecutive_passes.value, 0, 3)
        if self.emptied_seats.status is KnowledgeStatus.KNOWN: _seats(self.emptied_seats.value)
        known_ranks = []
        for fact in ranks:
            if fact.status is KnowledgeStatus.KNOWN: known_ranks.append(_int(fact.value, 1, 4))
        if len(known_ranks) != len(set(known_ranks)): _fail()
        if self.previous_result.status is KnowledgeStatus.KNOWN:
            if type(self.previous_result.value) is not PreviousResultSummary: _fail()
            self.previous_result.value.validate()
            source = self.previous_result.value.source_game_id
            if source.status is KnowledgeStatus.KNOWN and source.value == self.identity.game_id: _fail()
        phase = self.phase.value if self.phase.status is KnowledgeStatus.KNOWN else None
        if phase == "TERMINAL" and self.active_seat.status is not KnowledgeStatus.ABSENT: _fail()
        if phase in {"LEAD_PLAY", "FOLLOW_PLAY"} and self.active_seat.status is not KnowledgeStatus.KNOWN: _fail()
        if phase in {"TRIBUTE", "RETURN"}:
            if (self.current_winning.status is KnowledgeStatus.KNOWN
                    or self.current_winner_seat.status is KnowledgeStatus.KNOWN): _fail()
            if any(action.kind in {"play", "pass"} for action in self.history.actions): _fail()
        if phase == "LEAD_PLAY":
            if (self.current_winning.status is not KnowledgeStatus.ABSENT
                    or self.current_winner_seat.status is not KnowledgeStatus.ABSENT): _fail()
            if (self.leader_seat.status is KnowledgeStatus.KNOWN
                    and self.active_seat.value != self.leader_seat.value): _fail()
        if phase == "FOLLOW_PLAY":
            if (self.current_winning.status is not KnowledgeStatus.KNOWN
                    or self.current_winner_seat.status is not KnowledgeStatus.KNOWN): _fail()
        if self.current_winning.status is KnowledgeStatus.KNOWN:
            if (self.current_winner_seat.status is not KnowledgeStatus.KNOWN
                    or self.current_winner_seat.value != self.current_winning.value.actor): _fail()
        elif self.current_winner_seat.status is KnowledgeStatus.KNOWN:
            _fail()
        if (self.own_hand.status is KnowledgeStatus.KNOWN
                and self.current_winning.status is KnowledgeStatus.KNOWN
                and set(self.own_hand.value).intersection(self.current_winning.value.cards)):
            _fail()
        if self.remaining_counts.status is KnowledgeStatus.KNOWN and self.emptied_seats.status is KnowledgeStatus.KNOWN:
            counts = tuple(self.remaining_counts.value)
            if tuple(seat for seat, count in enumerate(counts) if count == 0) != tuple(self.emptied_seats.value): _fail()
            if (phase != "TERMINAL" and self.active_seat.status is KnowledgeStatus.KNOWN
                    and counts[self.active_seat.value] == 0): _fail()
        plays = [action for action in self.history.actions if action.kind == "play"]
        if self.current_winning.status is KnowledgeStatus.KNOWN and plays:
            last_play = plays[-1]
            winning = self.current_winning.value
            if last_play.actor != winning.actor or last_play.cards != winning.cards:
                _fail()
            if (last_play.declaration.status is KnowledgeStatus.KNOWN
                    and winning.declaration.status is KnowledgeStatus.KNOWN
                    and last_play.declaration.value != winning.declaration.value):
                _fail()
        if phase == "FOLLOW_PLAY":
            if not plays:
                _fail()
            leads = [action for action in self.history.actions
                     if action.kind == "play" and action.phase == "LEAD_PLAY"]
            if (leads and self.leader_seat.status is KnowledgeStatus.KNOWN
                    and leads[-1].actor != self.leader_seat.value):
                _fail()
        if self.ruleset_compatibility.status is KnowledgeStatus.KNOWN: _id(self.ruleset_compatibility.value)
        known_declarations: list[PublicDeclaration] = []
        if (self.current_winning.status is KnowledgeStatus.KNOWN
                and self.current_winning.value.declaration.status is KnowledgeStatus.KNOWN):
            known_declarations.append(self.current_winning.value.declaration.value)
        known_declarations.extend(
            action.declaration.value for action in self.history.actions
            if action.declaration.status is KnowledgeStatus.KNOWN
        )
        if known_declarations:
            if self.level.status is not KnowledgeStatus.KNOWN:
                _fail("PUBLIC_STATE_UNSUPPORTED")
            level = _int(self.level.value, 1, 13)
            for declaration in known_declarations:
                declaration.validate_for_level(level)
        for reasons in (self.unknown_reasons, self.unsupported_reasons):
            clean = _seq(reasons, _code, 64, unique=True)
            if clean != tuple(sorted(clean)): _fail()
        if (self.observer_seat.status is KnowledgeStatus.KNOWN
                and self.own_hand.status is KnowledgeStatus.KNOWN
                and self.remaining_counts.status is KnowledgeStatus.KNOWN):
            seat = _seat(self.observer_seat.value); hand = _cards(self.own_hand.value)
            counts = _seq(self.remaining_counts.value, lambda x: _int(x, 0, 28), 4)
            if counts[seat] != len(hand): _fail()

    def to_dict(self) -> dict[str, Any]:
        if type(self) is not CanonicalGuandanPublicState: _fail()
        self.validate()
        return {"schema_version": self.schema_version, "schema_revision": self.schema_revision,
                "contract": self.contract,
                "ruleset_id": self.ruleset_id, "observed_protocol": self.observed_protocol,
                "marvel_protocol": self.marvel_protocol, "visibility": self.visibility,
                "identity": self.identity.to_dict(),
                "ruleset_compatibility": _k_to(self.ruleset_compatibility, _id),
                "observer_seat": _k_to(self.observer_seat, _seat),
                "active_seat": _k_to(self.active_seat, _seat),
                "leader_seat": _k_to(self.leader_seat, _seat),
                "current_winner_seat": _k_to(self.current_winner_seat, _seat),
                "phase": _k_to(self.phase, _phase),
                "level": _k_to(self.level, lambda x: _int(x, 1, 13)),
                "own_hand": _k_to(self.own_hand, lambda x: list(_cards(x))),
                "remaining_counts": _k_to(self.remaining_counts,
                    lambda x: list(_seq(x, lambda y: _int(y, 0, 28), 4))),
                "current_winning": _k_to(self.current_winning, _winning_to),
                "consecutive_passes": _k_to(self.consecutive_passes, lambda x: _int(x, 0, 3)),
                "emptied_seats": _k_to(self.emptied_seats, lambda x: list(_seats(x))),
                "finishing_ranks": [_k_to(x, lambda y: _int(y, 1, 4)) for x in self.finishing_ranks],
                "previous_result": _k_to(self.previous_result, _previous_to),
                "exchange": self.exchange.to_dict(), "history": self.history.to_dict(),
                "unknown_reasons": list(self.unknown_reasons),
                "unsupported_reasons": list(self.unsupported_reasons)}

    @classmethod
    def from_dict(cls, value: object) -> "CanonicalGuandanPublicState":
        if isinstance(value, dict) and "schema_revision" not in value:
            _fail("PUBLIC_STATE_UNSUPPORTED")
        data = _dict(value, {"schema_version", "schema_revision", "contract", "ruleset_id", "observed_protocol",
                             "marvel_protocol", "visibility", "identity", "ruleset_compatibility",
                             "observer_seat", "active_seat", "leader_seat", "current_winner_seat",
                             "phase", "level", "own_hand", "remaining_counts", "current_winning",
                             "consecutive_passes", "emptied_seats", "finishing_ranks", "previous_result",
                             "exchange", "history", "unknown_reasons", "unsupported_reasons"})
        ranks = _seq(data["finishing_ranks"], lambda x: _k_from(x, lambda y: _int(y, 1, 4)), 4)
        if len(ranks) != 4: _fail()
        result = cls(identity=StateIdentity.from_dict(data["identity"]),
            ruleset_compatibility=_k_from(data["ruleset_compatibility"], _id),
            observer_seat=_k_from(data["observer_seat"], _seat),
            active_seat=_k_from(data["active_seat"], _seat),
            leader_seat=_k_from(data["leader_seat"], _seat),
            current_winner_seat=_k_from(data["current_winner_seat"], _seat),
            phase=_k_from(data["phase"], _phase),
            level=_k_from(data["level"], lambda x: _int(x, 1, 13)),
            own_hand=_k_from(data["own_hand"], _cards),
            remaining_counts=_k_from(data["remaining_counts"],
                lambda x: _seq(x, lambda y: _int(y, 0, 28), 4)),
            current_winning=_k_from(data["current_winning"], CurrentWinning.from_dict),
            consecutive_passes=_k_from(data["consecutive_passes"], lambda x: _int(x, 0, 3)),
            emptied_seats=_k_from(data["emptied_seats"], _seats),
            finishing_ranks=ranks, previous_result=_k_from(data["previous_result"], PreviousResultSummary.from_dict),
            exchange=ExchangeState.from_dict(data["exchange"]), history=HistoryEvidence.from_dict(data["history"]),
            unknown_reasons=_seq(data["unknown_reasons"], _code, 64, unique=True),
            unsupported_reasons=_seq(data["unsupported_reasons"], _code, 64, unique=True),
            schema_version=data["schema_version"], schema_revision=data["schema_revision"],
            contract=data["contract"], ruleset_id=data["ruleset_id"],
            observed_protocol=data["observed_protocol"], marvel_protocol=data["marvel_protocol"],
            visibility=data["visibility"])
        result.validate(); return result


def _winning_to(value: object) -> dict[str, Any]:
    if type(value) is not CurrentWinning: _fail()
    return value.to_dict()


def _previous_to(value: object) -> dict[str, Any]:
    if type(value) is not PreviousResultSummary: _fail()
    return value.to_dict()
