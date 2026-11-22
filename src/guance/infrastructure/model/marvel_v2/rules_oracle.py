from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
import importlib
from importlib.machinery import ModuleSpec
import sys
from pathlib import Path
from types import ModuleType
from typing import Iterable

from guance.public_state_contract import (
    PublicDeclaration, WildAssignment, guandan_card_id_to_wire,
    wire_card_to_guandan_id, wire_rank_to_guandan_rank,
)
from .action_decoder import ActionCandidate
from .contracts import ActionDeclaration
RULES_IDENTITY = "rules.GD-RULES-0.1.verified"
_SOURCE_FILES = {
    "guandan/cards.py": (9015, "dcbf3a8dbdbc910fec9d37935f83d6980bc33a308400a5aa99f00b100bb68dca"),
    "guandan/combos.py": (26526, "a375a28964ecc6ef36dd5a487d9c039b422afd78c68ce54ac970265d64efacc8"),
    "guandan/round.py": (15472, "b7df95a68e58bb7422e7f94b67649d7adf9a2b043b9b89f9d26ca06a21c251b0"),
    "guandan/state.py": (16458, "20f08df290a04678c89b0d29713683474dc0ff7f737b13ce607346e2913d5925"),
}
_FAMILY_BY_COMBO = {
    1: "SINGLE", 2: "PAIR", 3: "TRIPLE", 4: "STRAIGHT", 5: "FULL_HOUSE",
    6: "PAIR_SEQUENCE", 7: "TRIPLE_SEQUENCE", 8: "RANK_BOMB",
    9: "STRAIGHT_FLUSH", 10: "RANK_BOMB", 11: "FOUR_KINGS",
}
_VALUE_TO_FAMILY = {
    "single": "SINGLE", "pair": "PAIR", "triple": "TRIPLE",
    "full_house": "FULL_HOUSE", "straight": "STRAIGHT",
    "pair_sequence": "PAIR_SEQUENCE", "triple_sequence": "TRIPLE_SEQUENCE",
    "rank_bomb": "RANK_BOMB", "straight_flush": "STRAIGHT_FLUSH",
    "four_kings": "FOUR_KINGS",
}
_RANK_WINDOWS = {
    5: tuple(tuple((15, *range(3, 15))[i:i + 5]) for i in range(9)) + ((14, 15, 3, 4, 5),),
    3: tuple(tuple((15, *range(3, 15))[i:i + 3]) for i in range(11)) + ((14, 15, 3),),
    2: tuple(tuple((15, *range(3, 15))[i:i + 2]) for i in range(12)) + ((14, 15),),
}
_SUIT_NAMES = ("CLUBS", "DIAMONDS", "HEARTS", "SPADES", "JOKER")


def _public_declaration(declaration: ActionDeclaration) -> PublicDeclaration:
    """Convert one decoded/resolved MARVEL declaration into the public DTO."""
    if type(declaration) is not ActionDeclaration:
        _fail("LIVE_DECLARATION_AMBIGUOUS")
    result = PublicDeclaration(
        family=declaration.family, declared_ranks=tuple(declaration.declared_ranks),
        comparison_rank=declaration.comparison_rank, suit=declaration.suit,
        length=declaration.length, canonical_cards=tuple(declaration.canonical_cards),
        wild_assignments=tuple(
            WildAssignment(item.physical_card, item.assigned_rank,
                           item.assigned_suit, item.substituted)
            for item in declaration.wild_assignments),
        canonical_action_cards=tuple(declaration.canonical_action_cards),
        protocol_length=declaration.protocol_length)
    return result


class RulesOracleError(ValueError):
    """Bounded rules/declaration error; never contains source values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise RulesOracleError(code)


def _normalized_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    except OSError:
        _fail("GD_RULES_SOURCE_UNAVAILABLE")


def verified_rules_files(root: Path) -> tuple[Path, ...]:
    """Verify the rules identity and return the four package-relative source paths."""
    root = root.resolve()
    if not root.is_dir():
        _fail("GD_RULES_SOURCE_UNAVAILABLE")
    for relative, (expected_bytes, expected_sha) in _SOURCE_FILES.items():
        path = (root / relative).resolve()
        try:
            if not path.is_relative_to(root) or not path.is_file() or path.is_symlink():
                _fail("GD_RULES_SOURCE_UNAVAILABLE")
        except (OSError, ValueError):
            _fail("GD_RULES_SOURCE_UNAVAILABLE")
        body = _normalized_bytes(path)
        if len(body) != expected_bytes or sha256(body).hexdigest() != expected_sha:
            _fail("GD_RULES_IDENTITY_MISMATCH")
    return tuple(Path(name) for name in _SOURCE_FILES)


def _load_verified_package(root: Path) -> tuple[ModuleType, ModuleType, ModuleType]:
    verified_rules_files(root)
    # Each verified root gets its own private namespace: two different roots in
    # one process (e.g. a worktree fixture and the frozen runtime source) must
    # never share cached rules modules.
    digest = sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:12]
    namespace = f"_guance_verified_gd_rules_0_1_{digest}"
    package = sys.modules.get(namespace)
    expected_path = str((root / "guandan").resolve())
    if package is not None:
        if list(getattr(package, "__path__", ())) != [expected_path]:
            _fail("GD_RULES_IDENTITY_MISMATCH")
    else:
        package = ModuleType(namespace)
        package.__package__ = namespace
        package.__path__ = [expected_path]
        package.__spec__ = ModuleSpec(namespace, loader=None, is_package=True)
        sys.modules[namespace] = package
    # The verified source is read-only frozen material: never write bytecode
    # caches into it from any process.
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        cards = importlib.import_module(namespace + ".cards")
        combos = importlib.import_module(namespace + ".combos")
        round_module = importlib.import_module(namespace + ".round")
    except Exception:
        _fail("GD_RULES_SOURCE_UNAVAILABLE")
    finally:
        sys.dont_write_bytecode = previous
    verified_rules_files(root)
    return cards, combos, round_module


def _storage_wires(card_ids: Iterable[int]) -> tuple[int, ...]:
    def key(card_id: int) -> tuple[int, int, int, int]:
        deck, offset = divmod(card_id, 54)
        rank = 3 + offset // 4 if offset < 52 else 16 + offset - 52
        suit = offset % 4 if offset < 52 else 4
        return (rank, suit, deck, card_id)
    ordered = tuple(sorted(card_ids, key=key))
    return tuple(guandan_card_id_to_wire(card_id) for card_id in ordered)


def _window(effective_ranks: Iterable[int], length: int) -> tuple[int, ...]:
    wanted = set(effective_ranks)
    matches = tuple(item for item in _RANK_WINDOWS[length] if set(item) == wanted)
    if len(matches) != 1:
        _fail("LIVE_DECLARATION_AMBIGUOUS")
    return matches[0]


@dataclass(frozen=True, slots=True)
class ObservedContinuousGameChain:
    source_game_id: str
    target_game_id: str
    source_settlement_sequence: int
    target_deal_sequence: int
    connection_id: str
    association_type: str = "experimental_transport_chain"


class VerifiedGuandanRulesOracle:
    """Read-only adapter over the frozen GD-RULES source identity.

    The source root is explicit. No interpreter search-path mutation, fallback ruleset or model
    dependency is used. Only four verified pure rules files are imported under a
    private namespace.
    """

    identity = RULES_IDENTITY

    def __init__(self, source_root: str | Path) -> None:
        self.source_root = Path(source_root).resolve()
        self.cards, self.combos, self.round = _load_verified_package(self.source_root)

    def _level_rank(self, wire_level: int):
        try:
            return self.cards.Rank(wire_rank_to_guandan_rank(wire_level))
        except Exception:
            _fail("LEVEL_UNAVAILABLE")

    def _cards(self, wire_cards: Iterable[int]):
        try:
            ids = tuple(wire_card_to_guandan_id(card) for card in wire_cards)
            return self.cards.decode_cards(ids)
        except Exception:
            _fail("PHYSICAL_CARDS_INVALID")

    def declaration_from_combination(self, combination) -> PublicDeclaration:
        family = _VALUE_TO_FAMILY.get(str(combination.kind.value))
        if family is None:
            _fail("LIVE_DECLARATION_AMBIGUOUS")
        physical_ids = tuple(combination.physical_ids())
        assignments = tuple(
            WildAssignment(
                guandan_card_id_to_wire(card_id), int(rank), _SUIT_NAMES[int(suit)],
                card_id not in set(combination.natural_wild_ids),
            )
            for card_id, (rank, suit) in sorted(combination.wild_assignments.items())
        )
        represented = {card_id: (int(rank), int(suit))
                       for card_id, (rank, suit) in combination.wild_assignments.items()}
        faces = []
        for card in combination.cards:
            if card.card_id in represented:
                rank, suit = represented[card.card_id]
            else:
                rank, suit = int(card.rank), int(card.suit)
            faces.append((rank, suit, card.card_id))
        counts = Counter(rank for rank, _suit, _card_id in faces)
        canonical = _storage_wires(physical_ids)
        protocol_length = None
        suit_name = None
        if family in {"SINGLE", "PAIR", "TRIPLE", "RANK_BOMB"}:
            declared = (int(combination.comparison_rank),)
            action_ids = tuple(wire_card_to_guandan_id(card) for card in canonical)
        elif family == "FULL_HOUSE":
            triple = int(combination.comparison_rank)
            pair = next(rank for rank, count in counts.items() if count == 2 and rank != triple)
            declared = (triple, pair)
            action_ids = tuple(card_id for target in (triple, pair)
                               for card_id in sorted(card_id for rank, _suit, card_id in faces
                                                     if rank == target))
        elif family in {"STRAIGHT", "STRAIGHT_FLUSH", "PAIR_SEQUENCE", "TRIPLE_SEQUENCE"}:
            length = 5 if family in {"STRAIGHT", "STRAIGHT_FLUSH"} else 3 if family == "PAIR_SEQUENCE" else 2
            window = _window(counts, length)
            declared = (window[0],)
            protocol_length = None if length == 5 else length
            action_ids = tuple(card_id for target in window
                               for card_id in sorted(card_id for rank, _suit, card_id in faces
                                                     if rank == target))
            if family == "STRAIGHT_FLUSH":
                suits = {suit for _rank, suit, _card_id in faces}
                if len(suits) != 1:
                    _fail("LIVE_DECLARATION_AMBIGUOUS")
                suit_name = _SUIT_NAMES[next(iter(suits))]
        else:
            declared = ()
            action_ids = (52, 53, 106, 107)
            canonical = _storage_wires((52, 106, 53, 107))
        result = PublicDeclaration(
            family=family, declared_ranks=declared,
            comparison_rank=int(combination.comparison_rank), suit=suit_name,
            length=len(physical_ids), canonical_cards=canonical,
            wild_assignments=assignments,
            canonical_action_cards=tuple(guandan_card_id_to_wire(card_id) for card_id in action_ids),
            protocol_length=protocol_length,
        )
        try:
            result.validate_for_level(int(combination.level_rank) if int(combination.level_rank) <= 13
                                      else {14: 1, 15: 2}[int(combination.level_rank)])
        except Exception:
            # validate_for_level expects wire rank, including A=1 and TWO=2.
            level_value = int(combination.level_rank)
            wire_level = {14: 1, 15: 2}.get(level_value, level_value)
            try:
                result.validate_for_level(wire_level)
            except Exception:
                _fail("LIVE_DECLARATION_AMBIGUOUS")
        return result

    @staticmethod
    def _combo_family_allowed(combo_type: int, declaration: PublicDeclaration) -> bool:
        family = _FAMILY_BY_COMBO.get(combo_type)
        if family != declaration.family:
            return False
        if combo_type == 8:
            return declaration.length in (4, 5)
        if combo_type == 10:
            return 6 <= declaration.length <= 10
        return True

    def declarations(self, wire_cards: Iterable[int], *, wire_level: int,
                     combo_type: int) -> tuple[PublicDeclaration, ...]:
        cards = self._cards(wire_cards)
        level = self._level_rank(wire_level)
        try:
            combinations = self.combos.recognize_combinations(cards, level_rank=level)
        except Exception:
            _fail("LIVE_DECLARATION_AMBIGUOUS")
        values: dict[str, PublicDeclaration] = {}
        for combination in combinations:
            declaration = self.declaration_from_combination(combination)
            if self._combo_family_allowed(combo_type, declaration):
                key = repr(declaration.to_dict())
                values[key] = declaration
        return tuple(values[key] for key in sorted(values))

    def unique_declaration(self, wire_cards: Iterable[int], *, wire_level: int,
                           combo_type: int) -> PublicDeclaration:
        values = self.declarations(wire_cards, wire_level=wire_level, combo_type=combo_type)
        if len(values) != 1:
            _fail("LIVE_DECLARATION_AMBIGUOUS")
        return values[0]

    def validate_observed_play(self, wire_cards: Iterable[int], *, wire_level: int,
                               combo_type: int, current: PublicDeclaration | None) -> PublicDeclaration:
        values = self.observed_play_declarations(wire_cards, wire_level=wire_level,
                                                 combo_type=combo_type, current=current)
        if len(values) != 1:
            _fail("LIVE_DECLARATION_AMBIGUOUS")
        return values[0]

    def observed_play_declarations(self, wire_cards: Iterable[int], *, wire_level: int,
                                   combo_type: int, current: PublicDeclaration | None,
                                   strongest_only: bool = False,
                                   ) -> tuple[PublicDeclaration, ...]:
        values = self.declarations(wire_cards, wire_level=wire_level, combo_type=combo_type)
        if not values:
            _fail("LIVE_DECLARATION_AMBIGUOUS")
        if current is None and (not strongest_only or len(values) == 1):
            return values
        current_combo = self._combination_for(current, wire_level) if current is not None else None
        try:
            matches = [(value, self._combination_for(value, wire_level)) for value in values]
            if current_combo is not None:
                matches = [(value, combo) for value, combo in matches
                           if self.combos.can_beat(combo, current_combo)]
        except RulesOracleError:
            raise
        except Exception:
            _fail("LIVE_PLAY_DOES_NOT_BEAT")
        if not matches:
            _fail("LIVE_PLAY_DOES_NOT_BEAT")
        if strongest_only and len(matches) > 1:
            strongest = max(combo.comparison_key for _, combo in matches)
            matches = [(value, combo) for value, combo in matches
                       if combo.comparison_key == strongest]
        return tuple(value for value, _ in matches)

    def _combination_for(self, declaration: PublicDeclaration, wire_level: int):
        cards = self._cards(declaration.canonical_cards)
        level = self._level_rank(wire_level)
        matches = []
        for combination in self.combos.recognize_combinations(cards, level_rank=level):
            try:
                if self.declaration_from_combination(combination) == declaration:
                    matches.append(combination)
            except RulesOracleError:
                continue
        if len(matches) != 1:
            _fail("CURRENT_DECLARATION_UNAVAILABLE")
        return matches[0]

    def legal_action_candidates(self, own_hand_wires: Iterable[int], *, wire_level: int,
                                phase: str, current: PublicDeclaration | None) -> tuple[ActionCandidate, ...]:
        if phase not in {"LEAD_PLAY", "FOLLOW_PLAY"}:
            _fail("LEGAL_ACTION_PHASE_UNSUPPORTED")
        if phase == "FOLLOW_PLAY" and current is None:
            _fail("LIVE_CURRENT_WINNING_REQUIRED")
        if phase == "LEAD_PLAY" and current is not None:
            _fail("LIVE_CURRENT_WINNING_UNEXPECTED")
        hand = self._cards(own_hand_wires)
        level = self._level_rank(wire_level)
        current_combo = None if current is None else self._combination_for(current, wire_level)
        try:
            plays = self.round.enumerate_plays(hand, level_rank=level, current=current_combo)
        except Exception:
            _fail("LEGAL_ACTIONS_UNAVAILABLE")
        result = []
        if phase == "FOLLOW_PLAY":
            result.append(ActionCandidate.pass_action())
        for play in plays:
            declaration = self.declaration_from_combination(play)
            result.append(ActionCandidate.play(phase, ActionDeclaration.from_domain(declaration), wire_level))
        unique = {candidate.tokens: candidate for candidate in result}
        if len(unique) != len(result) or not result:
            _fail("LEGAL_ACTIONS_UNAVAILABLE")
        return tuple(unique[key] for key in sorted(unique))

    def validate_resolved_play(self, declaration: ActionDeclaration, *, wire_level: int,
                               current: PublicDeclaration | None) -> None:
        """Validate one fully resolved play declaration against the verified rules.

        The declaration is reconstructed into the unique rules combination and,
        when following, must beat the current winning combination. This is the
        same oracle the decision provider uses; no second rules implementation.
        """
        public = _public_declaration(declaration)
        candidate_combo = self._combination_for(public, wire_level)
        if current is None:
            return
        current_combo = self._combination_for(current, wire_level)
        try:
            beats = bool(self.combos.can_beat(candidate_combo, current_combo))
        except RulesOracleError:
            raise
        except Exception:
            _fail("LIVE_PLAY_DOES_NOT_BEAT")
        if not beats:
            _fail("LIVE_PLAY_DOES_NOT_BEAT")

    def expected_donors(self, ranking_by_seat: tuple[int, int, int, int]) -> tuple[int, ...]:
        if tuple(sorted(ranking_by_seat)) != (1, 2, 3, 4):
            _fail("PREVIOUS_RESULT_ASSOCIATION_UNSUPPORTED")
        winner_team = ranking_by_seat.index(1) % 2
        mate = next(seat for seat in range(4) if seat % 2 == winner_team and seat != ranking_by_seat.index(1))
        donor_ranks = (3, 4) if ranking_by_seat[mate] == 2 else (4,)
        return tuple(sorted(ranking_by_seat.index(rank) for rank in donor_ranks))

    def tribute_order(self, donor_cards: Iterable[tuple[int, int]], *, wire_level: int) -> tuple[int, ...]:
        level = self._level_rank(wire_level)
        pairs = []
        for donor, wire_card in donor_cards:
            card = self._cards((wire_card,))[0]
            if card.is_level_wild(level):
                _fail("LIVE_EXCHANGE_VARIANT_UNSUPPORTED")
            pairs.append((donor, card))
        pairs.sort(key=lambda item: (
            -self.cards.effective_rank_value(item[1].rank, level), item[0], item[1].card_id,
        ))
        return tuple(donor for donor, _card in pairs)

    def legal_tribute_cards(self, own_hand_wires: Iterable[int], *, wire_level: int) -> tuple[int, ...]:
        """Return every physical card tied for the required highest tribute rank.

        GD-RULES fixes the required rank, but equivalent physical copies are
        distinct legal choices.  Live validation must not reject a confirmed
        copy merely because the reference environment deterministically picks
        the lowest card id for simulation.
        """
        hand = self._cards(own_hand_wires)
        level = self._level_rank(wire_level)
        eligible = tuple(card for card in hand if not card.is_level_wild(level))
        if not eligible:
            return ()
        best = max(self.cards.effective_rank_value(card.rank, level) for card in eligible)
        return tuple(sorted(guandan_card_id_to_wire(card.card_id) for card in eligible
                            if self.cards.effective_rank_value(card.rank, level) == best))

    def legal_tribute_card(self, own_hand_wires: Iterable[int], *, wire_level: int) -> int | None:
        cards = self.legal_tribute_cards(own_hand_wires, wire_level=wire_level)
        return cards[0] if cards else None

    def legal_return_cards(self, own_hand_wires: Iterable[int], *, wire_level: int) -> tuple[int, ...]:
        hand = self._cards(own_hand_wires)
        level = self._level_rank(wire_level)
        allowed = set(range(3, 11)) | {15}
        cards = tuple(sorted((card for card in hand
                              if not card.is_joker and not card.is_level_wild(level)
                              and card.rank is not level and int(card.rank) in allowed),
                             key=lambda item: (self.cards.effective_rank_value(item.rank, level), item.card_id)))
        return tuple(guandan_card_id_to_wire(card.card_id) for card in cards)


__all__ = [
    "ObservedContinuousGameChain", "RULES_IDENTITY", "RulesOracleError",
    "VerifiedGuandanRulesOracle",
]
