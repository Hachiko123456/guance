"""Generic test-only adapter for the authoritative Guandan declaration fixtures.

No vector id controls interpretation. Candidate physical cards, semantic tokens,
family grammar, level, and explicit mutation fields are the only inputs.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from guance.public_state_contract import (
    PublicDeclaration, WildAssignment, guandan_card_id_to_wire,
    guandan_rank_to_wire_rank,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "guandan_declaration_v1"
RULES_PATH = FIXTURE_ROOT / "guandan_declaration_rules_v1.json"
VECTORS_PATH = FIXTURE_ROOT / "guandan_declaration_golden_vectors_v1.json"
PROVENANCE_PATH = FIXTURE_ROOT / "provenance.json"
SHA_PATH = FIXTURE_ROOT / "SHA256SUMS"

RULES = json.loads(RULES_PATH.read_text(encoding="utf-8"))
VECTORS = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
PROVENANCE = json.loads(PROVENANCE_PATH.read_text(encoding="utf-8"))
POSITIVE_VECTORS = tuple(v for v in VECTORS["vectors"] if v["polarity"] == "positive")
NEGATIVE_VECTORS = tuple(v for v in VECTORS["vectors"] if v["polarity"] == "negative")
FAMILY_ORDER = tuple(RULES["token_contract"]["family_order"])
SUIT_NAMES = tuple(item["name"] for item in RULES["token_contract"]["suit_order"])
RANK_NAMES = {item["value"]: item["name"] for item in RULES["token_contract"]["rank_order"]}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def semantic_sha256(document: dict) -> str:
    value = deepcopy(document)
    value["integrity"]["semantic_sha256"] = None
    body = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def card_face(card_id: int) -> tuple[int, int, int]:
    deck, offset = divmod(card_id, 54)
    if offset < 52:
        return 3 + offset // 4, offset % 4, deck
    return 16 + (offset - 52), 4, deck


def storage_ids(card_ids) -> tuple[int, ...]:
    return tuple(sorted(card_ids, key=lambda card_id: (*card_face(card_id), card_id)))


def assignment_from_fixture(item: dict) -> WildAssignment:
    return WildAssignment(
        guandan_card_id_to_wire(item["card_id"]),
        item["target_rank"]["value"],
        item["target_suit"]["name"],
        item["mode"] == "substituted",
    )


def declaration_from_canonical(value: dict) -> PublicDeclaration:
    return PublicDeclaration(
        family=value["family"],
        declared_ranks=tuple(item["value"] for item in value["declared_ranks"]),
        comparison_rank=value["comparison_rank"]["value"],
        suit=None if value["declared_suit"] is None else value["declared_suit"]["name"],
        length=len(value["declaration_card_ids"]),
        canonical_cards=tuple(guandan_card_id_to_wire(card_id)
                              for card_id in value["declaration_card_ids"]),
        wild_assignments=tuple(assignment_from_fixture(item)
                               for item in value["wild_assignments"]),
        canonical_action_cards=tuple(guandan_card_id_to_wire(card_id)
                                     for card_id in value["canonical_action_card_ids"]),
        protocol_length=value["declared_length"],
    )


def positive_declaration(vector: dict) -> PublicDeclaration:
    return declaration_from_canonical(vector["expected"]["canonical_declaration"])


def declaration_raw(value: PublicDeclaration) -> dict:
    return {
        "declaration_schema": value.declaration_schema,
        "rank_encoding": value.rank_encoding,
        "declaration_ruleset": value.declaration_ruleset,
        "rules_semantic_sha256": value.rules_semantic_sha256,
        "family": value.family,
        "declared_ranks": list(value.declared_ranks),
        "comparison_rank": value.comparison_rank,
        "suit": value.suit,
        "length": value.length,
        "protocol_length": value.protocol_length,
        "canonical_cards": list(value.canonical_cards),
        "canonical_action_cards": list(value.canonical_action_cards),
        "wild_assignments": [
            {"physical_card": item.physical_card,
             "assigned_rank": item.assigned_rank,
             "assigned_suit": item.assigned_suit,
             "substituted": item.substituted}
            for item in value.wild_assignments
        ],
    }


def _effective_faces(card_ids, assignments) -> tuple[tuple[int, int, int], ...]:
    by_card = {item["card_id"]: item for item in assignments}
    result = []
    for card_id in card_ids:
        rank, suit, _deck = card_face(card_id)
        assignment = by_card.get(card_id)
        if assignment is not None:
            rank = assignment["target_rank"]["value"]
            suit = assignment["target_suit"]["value"]
        result.append((rank, suit, card_id))
    return tuple(result)


def _window(family: str, ranks) -> dict:
    wanted = Counter(ranks)
    matches = [item for item in RULES["families"][family]["sequence_windows"]
               if Counter(rank["value"] for rank in item["ranks"]) == wanted]
    if len(matches) != 1:
        raise ValueError("candidate sequence window is not authoritative")
    return matches[0]


def canonical_from_matrix(level: dict, case: dict) -> dict:
    family = case["family"]
    card_ids = tuple(case["physical_card_ids"])
    assignments = tuple(case["wild_assignments"])
    faces = _effective_faces(card_ids, assignments)
    counts = Counter(rank for rank, _suit, _card_id in faces)
    suit = None
    protocol_length = RULES["families"][family]["length_token"].get("value")
    if family == "FULL_HOUSE":
        triple = next(rank for rank, count in counts.items() if count == 3)
        pair = next(rank for rank, count in counts.items() if count == 2)
        declared_ranks, comparison, order = (triple, pair), triple, (triple, pair)
    elif family in {"STRAIGHT", "STRAIGHT_FLUSH", "PAIR_SEQUENCE", "TRIPLE_SEQUENCE"}:
        window = _window(family, counts.keys())
        declared_ranks = (window["declared_rank"]["value"],)
        comparison = window["comparison_rank"]["value"]
        order = tuple(item["value"] for item in window["ranks"])
        if family == "STRAIGHT_FLUSH":
            suit = faces[0][1]
    else:
        declared_ranks, comparison, order = (), 17, ()
    by_rank = {rank: [] for rank in order}
    for rank, _suit, card_id in faces:
        if rank in by_rank:
            by_rank[rank].append(card_id)
    action_ids = (tuple(card_id for rank in order for card_id in sorted(by_rank[rank]))
                  if order else (52, 53, 106, 107))
    return {
        "canonical_action_card_ids": list(action_ids),
        "comparison_key": case["comparison_key"],
        "comparison_rank": {"name": RANK_NAMES[comparison], "value": comparison},
        "declaration_card_ids": list(storage_ids(card_ids)),
        "declared_length": protocol_length,
        "declared_rank_count": len(declared_ranks),
        "declared_ranks": [{"name": RANK_NAMES[rank], "value": rank}
                           for rank in declared_ranks],
        "declared_suit": None if suit is None else {"name": SUIT_NAMES[suit], "value": suit},
        "family": family,
        "family_wire_value": family.lower(),
        "level_rank": level["level"],
        "natural_wild_ids": [item["card_id"] for item in assignments
                             if item["mode"] == "natural"],
        "wild_assignments": list(assignments),
    }


def canonical_sha256(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def matrix_declaration(level: dict, case: dict) -> PublicDeclaration:
    return declaration_from_canonical(canonical_from_matrix(level, case))


def _ordered_action_ids(faces, rank_order) -> tuple[int, ...]:
    by_rank = {rank: [] for rank in rank_order}
    for rank, _suit, card_id in faces:
        if rank in by_rank:
            by_rank[rank].append(card_id)
    return tuple(card_id for rank in rank_order for card_id in sorted(by_rank[rank]))


def _sequence_by_declared_rank(family: str, declared_rank: int) -> dict | None:
    matches = [item for item in RULES["families"][family]["sequence_windows"]
               if item["declared_rank"]["value"] == declared_rank]
    return matches[0] if len(matches) == 1 else None


def _candidate_metadata(family: str, faces, explicit_ranks=()):
    counts = Counter(rank for rank, _suit, _card_id in faces)
    if family == "FULL_HOUSE":
        triples = [rank for rank, count in counts.items() if count == 3]
        pairs = [rank for rank, count in counts.items() if count == 2]
        if triples and pairs:
            ranks, comparison, order = (triples[0], pairs[0]), triples[0], (triples[0], pairs[0])
        else:
            values = list(counts) or [3, 4]
            while len(values) < 2:
                values.append(4 if values[0] != 4 else 5)
            ranks, comparison, order = tuple(values[:2]), values[0], tuple(values[:2])
        return ranks, comparison, None, None, order
    if family in {"STRAIGHT", "STRAIGHT_FLUSH", "PAIR_SEQUENCE", "TRIPLE_SEQUENCE"}:
        window = None
        if explicit_ranks:
            window = _sequence_by_declared_rank(family, explicit_ranks[0])
        if window is None:
            try:
                window = _window(family, counts.keys())
            except ValueError:
                window = None
        if window is not None:
            ranks = (window["declared_rank"]["value"],)
            comparison = window["comparison_rank"]["value"]
            order = tuple(item["value"] for item in window["ranks"])
        else:
            values = tuple(counts) or (3,)
            ranks = tuple(explicit_ranks[:1]) or (values[0],)
            comparison, order = values[-1], values
        suit = None
        if family == "STRAIGHT_FLUSH":
            suits = {suit for _rank, suit, _card_id in faces}
            suit = SUIT_NAMES[next(iter(suits))] if suits else "CLUBS"
        protocol_length = RULES["families"][family]["length_token"].get("value")
        return ranks, comparison, suit, protocol_length, order
    return (), 17, None, None, ()


def _parse_candidate_tokens(vector: dict) -> PublicDeclaration:
    candidate = vector["candidate"]
    family = vector["family"]
    card_ids = tuple(item["card_id"] for item in candidate.get("physical_cards", ()))
    tokens = candidate.get("complete_token_sequence")
    level_rank = vector["level_rank"]["value"]
    if tokens is not None:
        ranks = tuple(token - 18 + 3 for token in tokens if 18 <= token <= 32)
        suits = tuple(token - 33 for token in tokens if 33 <= token <= 37)
        lengths = tuple(token - 38 for token in tokens if 38 <= token <= 63)
        action_ids = tuple(token - 64 for token in tokens if 64 <= token <= 171)
        wild_tokens = tuple(token for token in tokens if 172 <= token <= 223)
        physical_wilds = sorted(card_id for card_id in card_ids
                                if card_face(card_id)[:2] == (level_rank, 2))
        pool = physical_wilds + [card_id for card_id in card_ids if card_id not in physical_wilds]
        assignment_items = []
        assignments = []
        for index, token in enumerate(wild_tokens):
            card_id = pool[index] if index < len(pool) else card_ids[0]
            rank, suit = 3 + (token - 172) // 4, (token - 172) % 4
            assignment_items.append({"card_id": card_id,
                                     "target_rank": {"value": rank},
                                     "target_suit": {"value": suit}})
            assignments.append(WildAssignment(
                guandan_card_id_to_wire(card_id), rank, SUIT_NAMES[suit],
                not (rank == level_rank and suit == 2),
            ))
        faces = _effective_faces(card_ids, assignment_items)
        _derived_ranks, comparison, derived_suit, derived_length, _order = _candidate_metadata(
            family, faces, ranks)
        comparison = candidate.get("forged_comparison_rank", {}).get("value", comparison)
        declared_suit = SUIT_NAMES[suits[0]] if suits else None
        return PublicDeclaration(
            family, ranks, comparison, declared_suit, len(card_ids),
            tuple(guandan_card_id_to_wire(card_id) for card_id in storage_ids(card_ids)),
            tuple(assignments),
            tuple(guandan_card_id_to_wire(card_id) for card_id in action_ids),
            lengths[0] if lengths else None,
        )
    faces = _effective_faces(card_ids, ())
    ranks, comparison, suit, protocol_length, order = _candidate_metadata(family, faces)
    comparison = candidate.get("forged_comparison_rank", {}).get("value", comparison)
    action_ids = (_ordered_action_ids(faces, order) if order else
                  ((52, 53, 106, 107) if family == "FOUR_KINGS" else card_ids))
    return PublicDeclaration(
        family, ranks, comparison, suit, len(card_ids),
        tuple(guandan_card_id_to_wire(card_id) for card_id in storage_ids(card_ids)),
        (), tuple(guandan_card_id_to_wire(card_id) for card_id in action_ids),
        protocol_length,
    )


def negative_declaration(vector: dict) -> PublicDeclaration:
    return _parse_candidate_tokens(vector)


def wire_level(vector_or_level: dict) -> int:
    value = (vector_or_level["level_rank"]["value"] if "level_rank" in vector_or_level
             else vector_or_level["level"]["value"])
    return guandan_rank_to_wire_rank(value)
