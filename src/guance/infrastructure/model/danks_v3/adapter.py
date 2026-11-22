"""Wire-space adapter for the DanKS V3 inference core (phase 3).

Bridges the public contracts (CanonicalPublicGameView.v1 + DeclaredFacts.v1)
to the label-space core of phase 2:

- wire entity codes -> DanKS card labels (deck copies collapse to the same
  face label; a hand holding both deck copies yields a repeated label);
- a confirmed ``PublicDeclaration`` -> the engine follow target
  (``[kind, rank, cards]`` for ``Moves.parse_second_action``), using the
  frozen GD-rank to engine-label mapping (GD 10-13 -> T/J/Q/K);
- the complete legal candidate list formation point (``second_actions`` /
  ``first_actions`` through :func:`normalize_engine_rows`) - the D03
  exclusion/inclusion assertion site, before ranking, Top10 truncation and
  model inference;
- model-eligibility checks: history provenance gate and the declaration
  ambiguity oracle (an unsupported/unknown declaration over entity cards
  that admit several level-valid interpretations is ineligible).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from guance.contracts import CanonicalPublicGameView, DeclaredFacts
from guance.public_state_contract import PublicDeclaration

from .core import DanksV3CoreError, normalize_engine_rows
from .vendor.danks.retrieval.models import ActionCandidate
from .vendor.guandan.engine.moves import Moves

_WIRE_SUIT_LABEL = {1: "S", 2: "H", 3: "C", 4: "D"}
_WIRE_RANK_LABEL = {1: "A", 2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7",
                    8: "8", 9: "9", 10: "T", 11: "J", 12: "Q", 13: "K"}
# GD rank -> engine rank label; GD 10-13 are T/J/Q/K (python_rules.RANK_VALUE).
GD_TO_ENGINE_RANK = {3: "3", 4: "4", 5: "5", 6: "6", 7: "7", 8: "8", 9: "9",
                     10: "T", 11: "J", 12: "Q", 13: "K", 14: "A", 15: "2",
                     16: "BJ", 17: "RJ"}
DANKS_TO_ENGINE_JOKER = {"BJ": "SB", "RJ": "HR"}
# The three frozen joker representations with one source (GD comparison rank):
# the DanKS strategy context and features read "BJ"/"RJ" (retrieval
# normalize_rank/rank_strength space), the raw engine Moves comparison-point
# field reads "B"/"R" (python_rules RANK_VALUE 16/17 - "BJ"/"RJ" would hit the
# 0 default there), and engine entity card faces are "SB"/"HR".
_JOKER_CONTEXT_RANKS = ("BJ", "RJ")
_JOKER_GD_RANK = {"BJ": 16, "RJ": 17}
_JOKER_MOVES_RANK = {"BJ": "B", "RJ": "R"}
_FAMILY_TO_ENGINE_KIND = {"SINGLE": "Single", "PAIR": "Pair", "TRIPLE": "Trips",
                          "FULL_HOUSE": "ThreeWithTwo", "STRAIGHT": "Straight",
                          "PAIR_SEQUENCE": "ThreePair", "TRIPLE_SEQUENCE": "TwoTrips",
                          "STRAIGHT_FLUSH": "StraightFlush",
                          "FOUR_KINGS": "FourKings", "RANK_BOMB": "Bomb"}


class DanksV3AdapterError(ValueError):
    """Adapter/eligibility failure; carries a stable code."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


def wire_cards_to_labels(codes) -> list[str]:
    """Entity card codes -> DanKS labels (both deck copies share one face)."""
    labels = []
    for code in codes:
        suit, rank = (code >> 4) & 15, code & 15
        if suit == 0:
            labels.append({14: "BJ", 15: "RJ"}[rank])
            continue
        if suit not in _WIRE_SUIT_LABEL or rank not in _WIRE_RANK_LABEL:
            raise DanksV3AdapterError("WIRE_CARD_INVALID", f"invalid card code {code}")
        labels.append(f"{_WIRE_SUIT_LABEL[suit]}{_WIRE_RANK_LABEL[rank]}")
    return labels


def engine_label(card: str) -> str:
    return DANKS_TO_ENGINE_JOKER.get(card, card)


def joker_representations(gd_rank: int) -> tuple[str, str, str]:
    """(DanKS context label, Moves comparison rank, engine face) for 16/17.

    The single definition of the three frozen joker representations; every
    boundary crossing goes through this table instead of ad-hoc strings.
    """
    context = GD_TO_ENGINE_RANK.get(gd_rank)
    if context not in _JOKER_CONTEXT_RANKS:
        raise DanksV3AdapterError("JOKER_RANK_INVALID",
                                  f"not a joker comparison rank: {gd_rank}")
    return context, _JOKER_MOVES_RANK[context], DANKS_TO_ENGINE_JOKER[context]


def joker_representation_roundtrip() -> list[tuple[int, str, str, str]]:
    """Verify the three joker representations roundtrip through both engines.

    Checks, per joker: context label -> GD rank -> Moves rank -> back, the
    context -> face mapping, the Moves rank inside the face label, and the
    vendored retrieval alias table mapping Moves/faces onto the context
    label.  Returns the verified (gd_rank, context, moves, face) rows.
    """
    from .vendor.danks.retrieval.rules import normalize_rank
    rows = []
    for gd_rank in (16, 17):
        context, moves_rank, face = joker_representations(gd_rank)
        assert GD_TO_ENGINE_RANK[gd_rank] == context
        assert _JOKER_GD_RANK[context] == gd_rank
        assert _JOKER_MOVES_RANK[context] == moves_rank
        assert DANKS_TO_ENGINE_JOKER[context] == face
        assert face[1] == moves_rank
        assert normalize_rank(moves_rank) == context
        assert normalize_rank(face) == context
        assert normalize_rank(context) == context
        rows.append((gd_rank, context, moves_rank, face))
    return rows


def declaration_to_follow_target(declaration: PublicDeclaration) -> tuple[str, str, list[str]]:
    """Confirmed declaration -> engine follow target (kind, rank, cards).

    The rank and cards are in the raw engine Moves space: the comparison
    point field uses B/R for jokers and python_rules rank labels otherwise,
    and entity card faces use SB/HR.  Only families the deployment can follow
    consistently across both frozen rule sets are mapped; exchange-phase
    families and shapes with a frozen rule-set divergence must never reach
    here.
    """
    kind = _FAMILY_TO_ENGINE_KIND.get(declaration.family)
    # Both rule sets agree on ordinary triples. The A-2 window is highest
    # under verified GD rules and lowest in the DanKS move generator.
    if kind is None or (declaration.family == "TRIPLE_SEQUENCE" and declaration.comparison_rank == 15):
        raise DanksV3AdapterError("FOLLOW_TARGET_UNSUPPORTED",
                                  f"family {declaration.family} has no follow target")
    rank = GD_TO_ENGINE_RANK[declaration.comparison_rank]
    if rank in _JOKER_MOVES_RANK:
        rank = _JOKER_MOVES_RANK[rank]
    cards = [engine_label(wire_cards_to_labels([code])[0])
             for code in declaration.canonical_action_cards]
    return kind, rank, cards


def declaration_context_rank(declaration: PublicDeclaration) -> str:
    """DanKS strategy-context rank label of a confirmed declaration.

    Jokers stay in the retrieval space (BJ/RJ); every other rank shares the
    Moves-boundary label (GD 10-13 -> T/J/Q/K).
    """
    return GD_TO_ENGINE_RANK[declaration.comparison_rank]


def wire_rank_to_engine_label(wire_rank: int) -> str:
    """Frozen wire-rank -> engine rank label mapping (GD 10-13 -> T/J/Q/K).

    Single source for the level label and every rank label crossing the
    Moves boundary; str(11..13) would make the engine read jacks/kings as
    point value 0 (the phase 1 returned defect, same family).
    """
    return {1: "A", 2: "2", 10: "T", 11: "J", 12: "Q", 13: "K"}.get(
        wire_rank, str(wire_rank) if 3 <= wire_rank <= 9 else None)


def _to_engine_labels(cards) -> list[str]:
    """DanKS labels -> engine labels (jokers only; the Moves boundary)."""
    return [DANKS_TO_ENGINE_JOKER.get(card, card) for card in cards]


def legal_follow_candidates(hand_labels, level: int,
                            declaration: PublicDeclaration) -> list[ActionCandidate]:
    """Complete legal follow list from the declaration-derived target.

    This is the D03 assertion point: the list is formed from the confirmed
    declaration's comparison rank before any ranking, Top10 truncation or
    model inference.  The hand is converted to engine labels for the Moves
    call (the engine reads BJ/RJ as jacks otherwise); the returned rows are
    normalized back into DanKS space.  An empty engine list is rejected.
    """
    kind, rank, cards = declaration_to_follow_target(declaration)
    moves = Moves()
    moves.parse_second_action(_to_engine_labels(hand_labels), 0,
                              wire_rank_to_engine_label(level),
                              [kind, rank, cards], None, None)
    return normalize_engine_rows(moves.action_list)


def legal_lead_candidates(hand_labels, level: int) -> list[ActionCandidate]:
    moves = Moves()
    hearts = sum(1 for card in hand_labels
                 if card == f"H{wire_rank_to_engine_label(level)}")
    moves.parse_first_action(_to_engine_labels(hand_labels), hearts,
                             wire_rank_to_engine_label(level))
    return normalize_engine_rows(moves.action_list)


def _wire_level_label(level: int) -> str:
    # Kept as an alias over the frozen mapping; observation_context uses it.
    return wire_rank_to_engine_label(level)


_FAMILY_BY_TYPE_LABEL = {1: "Single", 2: "Pair", 3: "Triple", 4: "Straight",
                         5: "TriplePlus", 6: "StraightPair", 7: "StraightTriple",
                         8: "Bomb", 9: "StraightFlush", 10: "FourKings"}
_DECLARATION_FAMILY_LABEL = {"SINGLE": "Single", "PAIR": "Pair", "TRIPLE": "Triple",
                             "FULL_HOUSE": "TriplePlus", "STRAIGHT": "Straight",
                             "PAIR_SEQUENCE": "StraightPair",
                             "TRIPLE_SEQUENCE": "StraightTriple",
                             "RANK_BOMB": "Bomb", "STRAIGHT_FLUSH": "StraightFlush",
                             "FOUR_KINGS": "FourKings"}


def _wire_rank_to_gd(wire_rank: int) -> int:
    return {1: 14, 2: 15}.get(wire_rank, wire_rank)


def danks_seat(wire_seat: int) -> int:
    """Reflect a Guance wire seat into the DanKS engine seat frame.

    ROTATION DIRECTION (verified against the frozen sources 2026-11-21):
    the DanKS engine advances play CLOCKWISE - ``_next_seat_with_cards``
    returns ``(current_pos + i) % 4`` (guandan/engine/table.py) - while the
    Guance state machine advances COUNTER-CLOCKWISE - ``current_seat =
    (event.player - 1) % 4`` (domain/state.py).  The two frames are mirror
    images; feeding wire seats directly swaps the left/right opponent slots
    and the play-order semantics the network was trained on.  The reflection
    ``danks = (-wire) % 4`` maps the wire decreasing-play chain onto the
    engine increasing-play chain: wire next actor (M-1) lands at danks
    (M'+1), the partner stays at +2, and the last_player/trick-holder
    relative offsets match the training data.
    """
    return (-wire_seat) % 4


def observation_context(view: CanonicalPublicGameView,
                        target_declaration: PublicDeclaration | None = None,
                        declared_facts: Any = None) -> dict[str, Any]:
    """Public view + resolved target -> retrieval context for the phase 2 core.

    The frame is GAME-ABSOLUTE in DANKS seats (the reflected wire frame, see
    :func:`danks_seat`): ``my_seat``/``public_counts``/``known_hand_cards``/
    ``last_player``/history ``pos`` all use danks seat indices; the vendored
    featurizer and card memory relativize internally by ``my_seat``
    (``(actor - my_seat) % 4``) exactly as in the training data.  The
    current-trick fields come from the CONFIRMED declaration's comparison
    rank when one is resolved; history events use the DECLARED history kinds
    and comparison ranks when the index-aligned facts carry them, with real
    PASS events marked kind="PASS" (the card memory's response-evidence
    tracking keys on it).
    """
    hand = wire_cards_to_labels(view.hand)
    my_seat = danks_seat(view.my_seat)
    # counts ordered by danks seats: danks seat s <-> wire seat (-s) % 4.
    public_counts = [view.remaining[(-s) % 4] for s in range(4)]
    current_kind, current_rank, current_size = "Lead", None, 0
    if target_declaration is not None:
        kind, _moves_rank, cards = declaration_to_follow_target(target_declaration)
        from .vendor.danks.retrieval.rules import normalize_kind
        current_kind = normalize_kind(kind)
        # Strategy context keeps the DanKS rank space (jokers BJ/RJ, GD 10-13
        # T/J/Q/K); the Moves-boundary B/R form never enters features.
        current_rank = declaration_context_rank(target_declaration)
        current_size = len(cards)
    elif not view.free_lead and view.lead_action is not None:
        lead = view.lead_action
        current_kind = _FAMILY_BY_TYPE_LABEL.get(lead.combo_type, "Lead")
        current_rank = GD_TO_ENGINE_RANK[_wire_rank_to_gd(_lead_comparison(lead, view.level))]
        current_size = len(lead.cards)
    last_player = None
    if not view.free_lead and view.lead_action is not None:
        last_player = danks_seat(view.lead_action.player)
    played = [label for action in view.public_history for label in
              wire_cards_to_labels(action.cards)]
    declared_by_index = {}
    if declared_facts is not None:
        for item in declared_facts.history_declarations:
            if item.status == "known" and item.declaration is not None:
                declared_by_index[item.index] = item.declaration
    history = []
    for action in view.public_history:
        if action.kind == "pass":
            # Real PASS events are tracked by the card memory's response
            # evidence; they must be marked PASS, not Unknown.
            history.append({"pos": danks_seat(action.player), "kind": "PASS",
                            "rank": None, "cards": []})
            continue
        declaration = declared_by_index.get(action.index)
        if declaration is not None:
            kind = _DECLARATION_FAMILY_LABEL[declaration.family]
            rank = GD_TO_ENGINE_RANK[declaration.comparison_rank]
        else:
            kind = _FAMILY_BY_TYPE_LABEL.get(action.combo_type, "Unknown")
            rank = GD_TO_ENGINE_RANK[_wire_rank_to_gd(_lead_comparison(action, view.level))]                 if action.combo_type in (1, 2, 3, 8) else None
        history.append({"pos": danks_seat(action.player), "kind": kind,
                        "rank": rank,
                        "cards": wire_cards_to_labels(action.cards)})
    return {"my_seat": my_seat,
            "curRank": _wire_level_label(view.level),
            "public_counts": public_counts,
            "current_kind": current_kind, "current_rank": current_rank,
            "current_size": current_size, "last_player": last_player,
            "played_cards": played,
            "known_hand_cards": {str(my_seat): hand},
            "history": history}


def _lead_comparison(lead, level: int) -> int:
    """Best-fit comparison rank of the raw lead cards (wire rank space)."""
    wild = (2 << 4) | level
    ranks = [(code & 15) for code in lead.cards if code != wild]
    if not ranks:
        return level
    counts: dict[int, int] = {}
    for rank in ranks:
        counts[rank] = counts.get(rank, 0) + 1
    if len(lead.cards) == 5:
        triples = [rank for rank, count in counts.items() if count >= 3]
        return max(triples) if triples else max(counts)
    return max(counts)


def declaration_interpretations(cards: tuple[int, ...], level: int,
                                family: str = "FULL_HOUSE") -> list[PublicDeclaration]:
    """All level-valid complete interpretations of one entity-card set.

    Bounded oracle for the ambiguity gate: for a five-card full house with
    exactly one heart-level wildcard it enumerates the wildcard assignments
    (native level-rank/HEARTS and substituted rank/CLUBS for each distinct
    natural rank) and keeps the complete declarations that validate for the
    level.  Same entity cards with different assignments are different
    declarations.
    """
    interpretations: list[PublicDeclaration] = []
    wild = (2 << 4) | level
    if family != "FULL_HOUSE" or len(cards) != 5:
        return interpretations
    if sum(1 for code in cards if code == wild) != 1:
        return interpretations
    naturals = [code for code in cards if code != wild]
    natural_gd_counts: dict[int, int] = {}
    for code in naturals:
        gd = _wire_rank_to_gd(code & 15)
        natural_gd_counts[gd] = natural_gd_counts.get(gd, 0) + 1
    gd_level = _wire_rank_to_gd(level)
    assignments = [(gd_level, "HEARTS", False), (gd_level, "CLUBS", True)]
    for wire_rank in sorted({code & 15 for code in naturals}):
        if wire_rank != level:
            assignments.append((_wire_rank_to_gd(wire_rank), "CLUBS", True))
    from guance.public_state_contract import WildAssignment
    gd_to_wire = {14: 1, 15: 2, **{rank: rank for rank in range(3, 14)}}
    for assigned_gd, suit, substituted in assignments:
        counts = dict(natural_gd_counts)
        counts[assigned_gd] = counts.get(assigned_gd, 0) + 1
        triples = [gd for gd, count in counts.items() if count == 3]
        pairs = [gd for gd, count in counts.items() if count == 2]
        for triple_gd in triples:
            for pair_gd in pairs:
                if pair_gd == triple_gd:
                    continue
                declaration = PublicDeclaration(
                    "FULL_HOUSE", (triple_gd, pair_gd), triple_gd, None, 5,
                    tuple(cards),
                    (WildAssignment(wild, gd_to_wire[assigned_gd], suit,
                                    substituted),), ())
                faces = declaration._effective_faces(declaration.canonical_cards,
                                                     declaration.wild_assignments)
                object.__setattr__(declaration, "canonical_action_cards",
                                   declaration._ordered_cards_for_ranks(
                                       faces, (triple_gd, pair_gd)))
                try:
                    declaration.validate_for_level(level)
                except Exception:
                    continue
                interpretations.append(declaration)
    return interpretations


def eligibility(view: CanonicalPublicGameView, declared_facts: DeclaredFacts | None,
                provenance) -> dict[str, Any]:
    """Model-eligibility decision for one public decision state.

    Rules (phase 1 frozen): unverified real-transport provenance is
    ineligible; an unknown/unsupported current-winning declaration is
    ineligible exactly when the entity cards admit more than one level-valid
    interpretation; a confirmed declaration or a free lead is eligible.
    """
    if provenance is not None and not provenance.eligible:
        return {"eligible": False,
                "code": "HISTORY_PROVENANCE_UNVERIFIED",
                "detail": provenance.ineligible_reason}
    fact = declared_facts.current_winning_declaration if declared_facts is not None \
        else None
    if fact is None:
        return {"eligible": True, "code": "ELIGIBLE",
                "detail": "free-lead public view without declaration facts"}
    if fact.status == "known":
        return {"eligible": True, "code": "ELIGIBLE",
                "detail": "confirmed declaration"}
    if fact.status == "absent":
        return {"eligible": True, "code": "ELIGIBLE", "detail": "free lead"}
    interpretations = declaration_interpretations(
        tuple(view.lead_action.cards), view.level)
    if len(interpretations) > 1:
        return {"eligible": False, "code": "DECLARATION_AMBIGUOUS",
                "detail": f"{len(interpretations)} level-valid interpretations"}
    return {"eligible": True, "code": "ELIGIBLE",
            "detail": "unsupported declaration with a unique interpretation"}
