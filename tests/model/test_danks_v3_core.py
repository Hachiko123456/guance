"""D02 core-path tests for the DanKS V3 inference core.

The layer-by-layer reference comparison (legal list -> Top10 -> features ->
logits -> final action) runs against the phase 1 frozen baseline when the
real checkpoint is present; thread count is pinned to one so float
reduction order matches the frozen reference.  Wrong-source negatives and
row-normalization semantics are self-contained.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pytest
import torch

from guance.infrastructure.model.danks_v3.loader import DanksV3LoaderError, load_selector
from guance.infrastructure.model.danks_v3.core import (
    DanksV3CoreError, RecommendationEngine, engine_label, normalize_engine_rows,
    three_field_key, to_danks_label,
)
from guance.infrastructure.model.danks_v3.vendor.danks.retrieval.models import (
    ActionCandidate,
)
from guance.infrastructure.model.danks_v3.vendor.danks.retrieval.cards import ALL_CARDS

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "tests/fixtures/danks_v3" / \
    "baseline_opening_seed_20261120.json"
CHECKPOINT_PATH = Path(os.environ.get(
    "GUANCE_DANKS_CHECKPOINT", r"E:\Downloads\top10_selector_v3_online.pt"))
_real_checkpoint = pytest.mark.skipif(
    not CHECKPOINT_PATH.is_file() or not BASELINE.is_file(),
    reason="frozen checkpoint or baseline not on this host")


@pytest.fixture(scope="module")
def _pinned_threads():
    previous = torch.get_num_threads()
    torch.set_num_threads(1)
    yield
    torch.set_num_threads(previous)


@pytest.fixture(scope="module")
def engine(_pinned_threads, tmp_path_factory):
    if CHECKPOINT_PATH.is_file():
        loaded = load_selector(CHECKPOINT_PATH)
    else:
        from test_danks_v3_loader import _base_payload, _save_checkpoint

        checkpoint, digest = _save_checkpoint(_base_payload(), tmp_path_factory.mktemp("selector"))
        loaded = load_selector(checkpoint, expected_sha256=digest)
    return RecommendationEngine(loaded)


def test_normalize_engine_rows_preserves_index_and_card_order():
    rows = [["Single", "3", ["S3"]], ["ThreeWithTwo", "5", ["S7", "S8", "S9", "H3", "D3"]]]
    candidates = normalize_engine_rows(rows)
    assert [c.index for c in candidates] == [0, 1]
    assert candidates[1].cards == ("S7", "S8", "S9", "H3", "D3")  # original order
    assert three_field_key(candidates[1]) == ("TriplePlus", "5",
                                              ("D3", "H3", "S7", "S8", "S9"))


def test_normalize_engine_rows_jokers_and_pass():
    rows = [["PASS", "PASS", "PASS"], ["Single", "B", ["SB"]], ["Single", "R", ["HR"]]]
    candidates = normalize_engine_rows(rows)
    assert candidates[0].kind == "PASS" and candidates[0].cards == ()
    assert candidates[1].kind == "Single" and candidates[1].cards == ("BJ",)
    assert candidates[2].kind == "Single" and candidates[2].cards == ("RJ",)


def test_normalize_engine_rows_rejects_bad_rows():
    with pytest.raises(DanksV3CoreError) as empty:
        normalize_engine_rows([])
    assert empty.value.code == "LEGAL_ACTIONS_EMPTY"
    for rows, code in (
        ([["Boom", "PASS", ["S3"]]], None),  # non-pass normalizing to PASS
        ([["Single", None, ["S3"]]], None),  # missing rank
        ([["Single", "3", []]], None),       # no cards
        ([["Single", "3", ["XX"]]], None),   # unnormalized label
        ([["Single", "3"]], None),           # wrong arity
    ):
        with pytest.raises(DanksV3CoreError):
            normalize_engine_rows(rows)


def test_recommend_rejects_missing_and_empty_action_sources(engine):
    hand = ["S3", "H3", "S4"]
    ctx = {"my_seat": 0, "curRank": "2", "current_kind": "Lead",
           "known_hand_cards": {"0": hand}, "public_counts": [27] * 4}
    with pytest.raises(DanksV3CoreError) as missing:
        engine.recommend(hand, ctx, None)
    assert missing.value.code == "LEGAL_ACTIONS_MISSING"
    with pytest.raises(DanksV3CoreError) as empty:
        engine.recommend(hand, ctx, [])
    assert empty.value.code == "LEGAL_ACTIONS_EMPTY"


def test_recommend_rejects_actions_outside_hand(engine):
    hand = ["S3", "H3", "S4"]
    ctx = {"my_seat": 0, "curRank": "2", "current_kind": "Lead",
           "known_hand_cards": {"0": hand}, "public_counts": [27] * 4}
    rogue = ActionCandidate(index=0, kind="Single", cards=("S5",), rank="5")
    with pytest.raises(DanksV3CoreError) as excinfo:
        engine.recommend(hand, ctx, [rogue])
    assert excinfo.value.code == "LEGAL_ACTION_NOT_IN_HAND"


def test_ranker_internal_generator_never_used_as_source(engine):
    """The deployment path must pass the engine list; the ranker's own
    ActionGenerator is a different action source and must stay unreachable."""
    from guance.infrastructure.model.danks_v3.vendor.danks.retrieval import ranker

    hand = ["S3", "H3", "S4", "H4", "S5"]
    ctx = {"my_seat": 0, "curRank": "2", "current_kind": "Lead",
           "known_hand_cards": {"0": hand}, "public_counts": [27] * 4}
    moves_rows = [["Single", "3", ["S3"]], ["Single", "3", ["H3"]],
                  ["Pair", "4", ["S4", "H4"]], ["Single", "5", ["S5"]]]
    legal = normalize_engine_rows(moves_rows)
    original = ranker.ActionGenerator.generate
    calls = []

    def spy(self, *args, **kwargs):
        calls.append(args)
        return original(self, *args, **kwargs)

    ranker.ActionGenerator.generate = spy
    try:
        result = engine.recommend(hand, ctx, legal)
    finally:
        ranker.ActionGenerator.generate = original
    assert calls == []  # the internal generator was never invoked
    assert 1 <= len(result.top10) <= 4


def test_recommend_works_with_fewer_legal_actions_than_topk(engine):
    hand = ["S3", "H3", "S4", "H4", "S5"]
    ctx = {"my_seat": 0, "curRank": "2", "current_kind": "Lead",
           "known_hand_cards": {"0": hand}, "public_counts": [27] * 4}
    legal = normalize_engine_rows([["Single", "3", ["S3"]]])
    result = engine.recommend(hand, ctx, legal)
    assert len(result.top10) == 1
    assert result.selected.kind == "Single" and result.selected.cards == ("S3",)


def test_candidate_mask_marks_slots_beyond_the_legal_list():
    """Fewer legal actions than TOPK: the mask must mark the unused slots
    invalid so the policy cannot select an empty slot."""
    from guance.infrastructure.model.danks_v3.vendor.danks.retrieval.context import (
        build_context,
    )
    from guance.infrastructure.model.danks_v3.vendor.danks.retrieval.ranker import (
        StructuralCandidateRanker,
    )
    from guance.infrastructure.model.danks_v3.vendor.danks.training.featurizer import (
        featurize_topk,
    )
    from guance.infrastructure.model.danks_v3.vendor.danks.training.schema import TOPK

    hand = ["S3", "H3", "S4", "H4", "S5"]
    ctx = build_context({"my_seat": 0, "curRank": "2", "current_kind": "Lead",
                         "known_hand_cards": {"0": hand},
                         "public_counts": [27] * 4})
    legal = normalize_engine_rows([["Single", "3", ["S3"]],
                                   ["Single", "3", ["H3"]],
                                   ["Pair", "4", ["S4", "H4"]],
                                   ["Single", "5", ["S5"]]])
    ranked = StructuralCandidateRanker().rank(hand, legal, ctx, top_k=TOPK)
    _state, _candidates, mask = featurize_topk(hand, ctx, ranked, top_k=TOPK)
    assert mask.shape == (TOPK,)
    assert mask.tolist() == [1.0] * len(ranked) + [0.0] * (TOPK - len(ranked))


@_real_checkpoint
def test_core_path_matches_the_frozen_baseline(engine):
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    hand = list(baseline["hand_danks_labels"])
    ctx = {"my_seat": 0, "curRank": "2", "current_kind": "Lead",
           "known_hand_cards": {"0": hand}, "public_counts": [27] * 4,
           "history": [], "played_cards": []}
    engine_rows = baseline["engine_raw_action_rows"]
    legal = normalize_engine_rows(engine_rows)
    assert len(legal) == baseline["engine_legal_action_count"]
    # Layer 1: legal list identity (index, kind, rank, original card order).
    frozen_legal = baseline["engine_legal_actions"]
    for candidate, frozen in zip(legal, frozen_legal):
        assert candidate.index == frozen["index"]
        assert candidate.kind == frozen["kind"]
        assert candidate.rank == frozen["rank"]
        assert list(candidate.cards) == frozen["cards"]
    # Layer 2/3: Top10 rows and features.
    result = engine.recommend(hand, ctx, legal)
    # The phase 1 baseline recorded Top10 card labels in sorted order; the
    # legal-list layer above already freezes the engine's original order.
    frozen_top = [(row["kind"], row["rank"], row["cards"], row["retrieval_score"])
                  for row in baseline["top10"]]
    actual_top = [(row.kind, row.rank, sorted(row.cards), row.retrieval_score)
                  for row in result.top10]
    for (kind_a, rank_a, cards_a, score_a), (kind_b, rank_b, cards_b, score_b) \
            in zip(actual_top, frozen_top):
        assert (kind_a, rank_a, cards_a) == (kind_b, rank_b, cards_b)
        assert math.isclose(score_a, score_b, abs_tol=1e-9)
    assert list(result.feature_sha256) == baseline["feature_sha256"]
    # Layer 4: logits within the frozen tolerance, argmax and final action.
    max_logit_diff = max(abs(a - b) for a, b in zip(result.logits,
                                                    baseline["logits"]))
    assert max_logit_diff <= 1e-5
    frozen_selected = baseline["selected_first_recommendation"]
    assert [result.selected.kind, result.selected.rank,
            sorted(result.selected.cards)] == frozen_selected


@_real_checkpoint
def test_first_query_flag_and_repeat_ranking(engine):
    hand = ["S3", "H3", "S4", "H4", "S5"]
    ctx = {"my_seat": 0, "curRank": "2", "current_kind": "Lead",
           "known_hand_cards": {"0": hand}, "public_counts": [27] * 4}
    legal = normalize_engine_rows([["Single", "3", ["S3"]],
                                   ["Single", "3", ["H3"]],
                                   ["Pair", "4", ["S4", "H4"]],
                                   ["Single", "5", ["S5"]]])
    first = engine.recommend(hand, ctx, legal)
    repeat = engine.recommend(hand, ctx, legal)
    assert first.first_query is False  # engine fixture already ran a query
    assert repeat.first_query is False
    assert repeat.rank_ms > 0.0


def test_engine_danks_joker_roundtrip():
    assert engine_label("BJ") == "SB" and engine_label("RJ") == "HR"
    assert to_danks_label("SB") == "BJ" and to_danks_label("HR") == "RJ"
    assert engine_label("S3") == "S3" and to_danks_label("D9") == "D9"


def test_full_declaration_identity_beats_the_three_field_key():
    """Same three-field key, different wildcard assignments: the candidate
    ranking path is three-field, declaration identity is not."""
    from guance.public_state_contract import PublicDeclaration, WildAssignment

    cards = (0x35, 0x45, 0x25, 0x39, 0x49)  # C5 D5 H5 C9 D9
    natural = PublicDeclaration("FULL_HOUSE", (5, 9), 5, None, 5, cards,
        (WildAssignment(0x25, 5, "HEARTS", False),),
        canonical_action_cards=(0x35, 0x45, 0x25, 0x39, 0x49))
    club_sub = PublicDeclaration("FULL_HOUSE", (5, 9), 5, None, 5, cards,
        (WildAssignment(0x25, 5, "CLUBS", True),),
        canonical_action_cards=(0x35, 0x45, 0x25, 0x39, 0x49))
    nine_sub = PublicDeclaration("FULL_HOUSE", (9, 5), 9, None, 5, cards,
        (WildAssignment(0x25, 9, "CLUBS", True),),
        canonical_action_cards=(0x25, 0x39, 0x49, 0x35, 0x45))
    for declaration in (natural, club_sub, nine_sub):
        declaration.validate_for_level(5)
    assert natural.comparison_rank == club_sub.comparison_rank == 5
    assert natural.canonical_cards == club_sub.canonical_cards
    assert natural.wild_assignments != club_sub.wild_assignments
    assert natural.to_dict() != club_sub.to_dict()
    assert natural.comparison_rank != nine_sub.comparison_rank
    # The ranked-candidate three-field key cannot carry the assignment
    # difference; the declaration channel (phase 3 contract) must.
    candidate = ActionCandidate(index=0, kind="TriplePlus", cards=cards, rank="5")
    assert three_field_key(candidate) == ("TriplePlus", "5",
        (0x25, 0x35, 0x39, 0x45, 0x49))
    candidate_b = ActionCandidate(index=1, kind="TriplePlus", cards=cards, rank="9")
    assert three_field_key(candidate) != three_field_key(candidate_b)
