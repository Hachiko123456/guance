"""Lossless MARVEL V2 canonical-action construction and decoding."""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Iterable, Literal, Sequence

from .contracts import (
    CARD_TOKEN_BASE, COMMIT_TOKEN, FAMILY_TOKEN, FAMILIES, LENGTH_TOKEN, PASS_TOKEN,
    PLAY_PHASES, RANK_TOKEN, SUIT_TOKEN, WILD_TOKEN_BASE, ActionProtocolError,
    ActionDeclaration, CardMapping, CommitIdentity, DecodedAction, ExchangeTransferFacts,
    MarvelProtocolError, WildAssignment,
    TOKEN_FAMILY, TOKEN_LENGTH, TOKEN_RANK, TOKEN_SUIT, _int, _reject, _tuple_ints,
    is_level_wild, sequence_comparison_rank, storage_order,
    wire_card_to_guandan_id,
)


def _wild_token(assignment: WildAssignment) -> int:
    if assignment.assigned_rank not in range(3, 16):
        _reject("wild_rank_invalid")
    suit_index = {"CLUBS": 0, "DIAMONDS": 1, "HEARTS": 2, "SPADES": 3}.get(
        assignment.assigned_suit
    )
    if suit_index is None:
        _reject("wild_suit_invalid")
    return WILD_TOKEN_BASE + (assignment.assigned_rank - 3) * 4 + suit_index


def declaration_tokens(declaration: ActionDeclaration, level: int) -> tuple[int, ...]:
    """Encode one fully validated declaration in the authoritative V1 order."""
    if type(declaration) is not ActionDeclaration:
        _reject("declaration_type_invalid")
    try:
        declaration.validate(level)
    except MarvelProtocolError:
        raise ActionProtocolError("declaration_invalid") from None
    result = [FAMILY_TOKEN[declaration.family]]
    if declaration.family == "STRAIGHT_FLUSH":
        if declaration.suit is None:
            _reject("declared_suit_missing")
        result.append(SUIT_TOKEN[declaration.suit])
    result.extend(RANK_TOKEN[rank] for rank in declaration.declared_ranks)
    if declaration.protocol_length is not None:
        result.append(LENGTH_TOKEN[declaration.protocol_length])
    result.extend(CARD_TOKEN_BASE + wire_card_to_guandan_id(card)
                  for card in declaration.canonical_action_cards)
    assignments = tuple(sorted(declaration.wild_assignments,
                               key=lambda item: wire_card_to_guandan_id(item.physical_card)))
    if assignments != declaration.wild_assignments:
        _reject("wild_assignment_order_invalid")
    result.extend(_wild_token(item) for item in assignments)
    result.append(COMMIT_TOKEN)
    if len(result) > 33:
        _reject("canonical_sequence_overflow")
    return tuple(result)


@dataclass(frozen=True, slots=True)
class ActionCandidate:
    """A complete legal action supplied by an exact rules oracle.

    The adapter never invents or truncates legal actions.  It only validates,
    tokenizes, groups next tokens, and reconstructs the selected full candidate.
    """

    kind: Literal["pass", "play", "tribute", "return"]
    phase: Literal["LEAD_PLAY", "FOLLOW_PLAY", "TRIBUTE", "RETURN"]
    physical_cards: tuple[int, ...] = ()
    declaration: ActionDeclaration | None = None
    level: int = 0
    weight: float = 1.0

    def validate(self) -> None:
        if self.kind not in {"pass", "play", "tribute", "return"}:
            _reject("candidate_kind_invalid")
        if type(self.weight) not in (int, float) or not isfinite(float(self.weight)) or self.weight <= 0:
            _reject("candidate_weight_invalid")
        cards = _tuple_ints(self.physical_cards, 0, 107, 27, unique=True,
                            code="candidate_cards_invalid")
        if self.kind == "pass":
            if self.phase != "FOLLOW_PLAY" or cards or self.declaration is not None or self.level != 0:
                _reject("pass_candidate_invalid")
        elif self.kind == "play":
            if self.phase not in PLAY_PHASES or type(self.declaration) is not ActionDeclaration:
                _reject("play_candidate_invalid")
            _int(self.level, 1, 13, "level_invalid")
            try:
                self.declaration.validate(self.level)
            except MarvelProtocolError:
                raise ActionProtocolError("declaration_invalid") from None
            expected = tuple(wire_card_to_guandan_id(card) for card in self.declaration.canonical_cards)
            if cards != expected:
                _reject("candidate_declaration_cards_mismatch")
        elif self.kind == "tribute":
            if self.phase != "TRIBUTE" or len(cards) != 1 or self.declaration is not None or self.level != 0:
                _reject("tribute_candidate_invalid")
        else:
            if self.phase != "RETURN" or len(cards) != 1 or self.declaration is not None or self.level != 0:
                _reject("return_candidate_invalid")

    @property
    def tokens(self) -> tuple[int, ...]:
        self.validate()
        if self.kind == "pass":
            return (PASS_TOKEN,)
        if self.kind in {"tribute", "return"}:
            return (CARD_TOKEN_BASE + self.physical_cards[0], COMMIT_TOKEN)
        assert self.declaration is not None
        return declaration_tokens(self.declaration, self.level)

    @classmethod
    def pass_action(cls, *, weight: float = 1.0) -> "ActionCandidate":
        return cls("pass", "FOLLOW_PLAY", weight=weight)

    @classmethod
    def play(cls, phase: Literal["LEAD_PLAY", "FOLLOW_PLAY"], declaration: ActionDeclaration,
             level: int, *, weight: float = 1.0) -> "ActionCandidate":
        cards = tuple(wire_card_to_guandan_id(card) for card in declaration.canonical_cards)
        result = cls("play", phase, cards, declaration, level, weight)
        result.validate()
        return result

    @classmethod
    def exchange(cls, phase: Literal["TRIBUTE", "RETURN"], card_id: int,
                 *, weight: float = 1.0) -> "ActionCandidate":
        result = cls("tribute" if phase == "TRIBUTE" else "return", phase,
                     (_int(card_id, 0, 107, "exchange_card_invalid"),), None, 0, weight)
        result.validate()
        return result


def _decoded(candidate: ActionCandidate, identity: CommitIdentity | None) -> DecodedAction:
    candidate.validate()
    cards = candidate.physical_cards
    if candidate.kind == "play":
        declaration = candidate.declaration
        assert declaration is not None
        result = DecodedAction(
            kind="play", phase=candidate.phase, tokens=candidate.tokens,
            physical_cards=cards, family=declaration.family,
            declared_ranks=declaration.declared_ranks,
            comparison_rank=declaration.comparison_rank,
            declared_suit=declaration.suit,
            physical_length=declaration.length,
            protocol_length=declaration.protocol_length,
            storage_order=tuple(wire_card_to_guandan_id(card) for card in declaration.canonical_cards),
            action_order=tuple(wire_card_to_guandan_id(card) for card in declaration.canonical_action_cards),
            wild_assignments=declaration.wild_assignments,
            declaration=declaration,
            commit_identity=identity,
        )
    elif candidate.kind == "pass":
        result = DecodedAction(
            kind="pass", phase=candidate.phase, tokens=candidate.tokens,
            physical_cards=(), family=None, declared_ranks=(), comparison_rank=None,
            declared_suit=None, physical_length=0, protocol_length=None,
            storage_order=(), action_order=(), wild_assignments=(), declaration=None,
            commit_identity=identity,
        )
    else:
        result = DecodedAction(
            kind=candidate.kind, phase=candidate.phase, tokens=candidate.tokens,
            physical_cards=cards, family=None, declared_ranks=(), comparison_rank=None,
            declared_suit=None, physical_length=1, protocol_length=None,
            storage_order=cards, action_order=cards, wild_assignments=(), declaration=None,
            commit_identity=identity,
        )
    result.validate()
    return result



def _assignment_from_token(card_id: int, token: int, level_rank: int) -> WildAssignment:
    offset = token - WILD_TOKEN_BASE
    rank, suit_index = 3 + offset // 4, offset % 4
    suit = ("CLUBS", "DIAMONDS", "HEARTS", "SPADES")[suit_index]
    natural = rank == level_rank and suit == "HEARTS"
    return WildAssignment(CardMapping.to_wire(card_id), rank, suit, not natural)


def decode_canonical_tokens(tokens: Sequence[int], *, phase: str, level: int,
                            own_hand: Iterable[int],
                            identity: CommitIdentity | None = None) -> DecodedAction:
    """Parse and semantically validate one complete canonical action sequence."""
    sequence = _tuple_ints(tokens, 0, 255, 33, code="committed_tokens_invalid")
    hand = set(_tuple_ints(own_hand, 0, 107, 28, unique=True, code="own_hand_invalid"))
    if phase == "FOLLOW_PLAY" and sequence == (PASS_TOKEN,):
        result = DecodedAction("pass", phase, sequence, (), None, (), None, None, 0,
                               None, (), (), (), None, identity)
        result.validate(); return result
    if phase in {"TRIBUTE", "RETURN"}:
        if len(sequence) != 2 or sequence[1] != COMMIT_TOKEN or not 64 <= sequence[0] < 172:
            raise ActionProtocolError("exchange_sequence_invalid")
        card_id = sequence[0] - CARD_TOKEN_BASE
        if card_id not in hand:
            raise ActionProtocolError("committed_cards_not_in_hand")
        kind = "tribute" if phase == "TRIBUTE" else "return"
        result = DecodedAction(kind, phase, sequence, (card_id,), None, (), None, None,
                               1, None, (card_id,), (card_id,), (), None, identity)
        result.validate(); return result
    if phase not in PLAY_PHASES or len(sequence) < 3 or sequence[-1] != COMMIT_TOKEN:
        raise ActionProtocolError("play_sequence_invalid")
    body = sequence[:-1]
    family = TOKEN_FAMILY.get(body[0])
    if family is None:
        raise ActionProtocolError("play_family_missing")
    index = 1
    suit = None
    if family == "STRAIGHT_FLUSH":
        if index >= len(body) or body[index] not in TOKEN_SUIT:
            raise ActionProtocolError("declared_suit_missing")
        suit = TOKEN_SUIT[body[index]]; index += 1
        if suit == "JOKER":
            raise ActionProtocolError("declared_suit_invalid")
    rank_count = 0 if family == "FOUR_KINGS" else 2 if family == "FULL_HOUSE" else 1
    ranks = []
    for _ in range(rank_count):
        if index >= len(body) or body[index] not in TOKEN_RANK:
            raise ActionProtocolError("declared_rank_missing")
        ranks.append(TOKEN_RANK[body[index]]); index += 1
    protocol_length = None
    if family in {"PAIR_SEQUENCE", "TRIPLE_SEQUENCE"}:
        if index >= len(body) or body[index] not in TOKEN_LENGTH:
            raise ActionProtocolError("declared_length_missing")
        protocol_length = TOKEN_LENGTH[body[index]]; index += 1
    elif index < len(body) and body[index] in TOKEN_LENGTH:
        raise ActionProtocolError("declared_length_forbidden")
    cards = []
    while index < len(body) and CARD_TOKEN_BASE <= body[index] < CARD_TOKEN_BASE + 108:
        cards.append(body[index] - CARD_TOKEN_BASE); index += 1
    if not cards or len(cards) != len(set(cards)) or not set(cards) <= hand:
        raise ActionProtocolError("committed_cards_not_in_hand")
    wild_tokens = []
    while index < len(body) and WILD_TOKEN_BASE <= body[index] < WILD_TOKEN_BASE + 52:
        wild_tokens.append(body[index]); index += 1
    if index != len(body):
        raise ActionProtocolError("play_token_order_invalid")
    level_rank = CardMapping.level_to_rank(level)
    physical_wilds = tuple(sorted(card_id for card_id in cards if is_level_wild(card_id, level_rank)))
    if len(physical_wilds) != len(wild_tokens):
        raise ActionProtocolError("wild_assignment_count_invalid")
    assignments = tuple(_assignment_from_token(card_id, token, level_rank)
                        for card_id, token in zip(physical_wilds, wild_tokens))
    if family in {"SINGLE", "PAIR", "TRIPLE", "RANK_BOMB", "FULL_HOUSE"}:
        comparison = ranks[0]
    elif family in {"STRAIGHT", "STRAIGHT_FLUSH"}:
        comparison = sequence_comparison_rank(ranks[0], 5)
    elif family == "PAIR_SEQUENCE":
        if protocol_length != 3:
            raise ActionProtocolError("declared_length_invalid")
        comparison = sequence_comparison_rank(ranks[0], 3)
    elif family == "TRIPLE_SEQUENCE":
        if protocol_length != 2:
            raise ActionProtocolError("declared_length_invalid")
        comparison = sequence_comparison_rank(ranks[0], 2)
    else:
        comparison = 17
    storage_ids = storage_order(cards)
    declaration = ActionDeclaration(
        family=family, declared_ranks=tuple(ranks), comparison_rank=comparison, suit=suit,
        length=len(cards), canonical_cards=tuple(CardMapping.to_wire(card_id) for card_id in storage_ids),
        wild_assignments=assignments,
        canonical_action_cards=tuple(CardMapping.to_wire(card_id) for card_id in cards),
        protocol_length=protocol_length,
    )
    try:
        declaration.validate(level)
    except MarvelProtocolError:
        raise ActionProtocolError("declaration_invalid") from None
    result = DecodedAction(
        kind="play", phase=phase, tokens=sequence, physical_cards=storage_ids, family=family,
        declared_ranks=tuple(ranks), comparison_rank=comparison, declared_suit=suit,
        physical_length=len(cards), protocol_length=protocol_length, storage_order=storage_ids,
        action_order=tuple(cards), wild_assignments=assignments, declaration=declaration,
        commit_identity=identity,
    )
    result.validate(); return result

def decode_committed_tokens(tokens: Sequence[int], candidates: Iterable[ActionCandidate],
                            *, own_hand: Iterable[int], identity: CommitIdentity | None = None) -> DecodedAction:
    """Decode only by matching the entire committed canonical sequence.

    A single row payload is intentionally insufficient.  Duplicate semantic
    candidates or a stale/forged sequence fail closed.
    """
    sequence = _tuple_ints(tokens, 0, 255, 33, code="committed_tokens_invalid")
    matches = []
    for candidate in candidates:
        candidate.validate()
        if candidate.tokens == sequence:
            matches.append(candidate)
    if len(matches) != 1:
        raise ActionProtocolError("committed_action_not_unique")
    candidate = matches[0]
    decoded = decode_canonical_tokens(sequence, phase=candidate.phase, level=candidate.level,
                                      own_hand=own_hand, identity=identity)
    expected = _decoded(candidate, identity)
    if decoded != expected:
        raise ActionProtocolError("committed_action_semantic_mismatch")
    return decoded


@dataclass(frozen=True, slots=True)
class DecodedExchangeResolution:
    transfers: tuple[ExchangeTransferFacts, ...]
    anti_tribute: bool
    commit_identity: CommitIdentity | None = None

    def validate(self) -> None:
        if type(self.anti_tribute) is not bool or len(self.transfers) > 8:
            _reject("exchange_resolution_invalid")
        seen: set[tuple[str, int, int]] = set()
        cards: set[int] = set()
        for transfer in self.transfers:
            if type(transfer) is not ExchangeTransferFacts:
                _reject("exchange_resolution_invalid")
            transfer.validate()
            key = (transfer.kind, transfer.source_seat, transfer.target_seat)
            if key in seen or transfer.card_id in cards:
                _reject("exchange_resolution_nonunique")
            seen.add(key); cards.add(transfer.card_id)
        if self.anti_tribute and any(item.kind == "tribute" for item in self.transfers):
            _reject("anti_tribute_conflict")


def decode_exchange_resolution(transfers: Iterable[ExchangeTransferFacts], *, anti_tribute: bool,
                               identity: CommitIdentity | None = None) -> DecodedExchangeResolution:
    result = DecodedExchangeResolution(tuple(transfers), anti_tribute, identity)
    result.validate()
    return result


__all__ = ["ActionCandidate", "DecodedExchangeResolution", "declaration_tokens",
           "decode_canonical_tokens", "decode_committed_tokens",
           "decode_exchange_resolution"]
