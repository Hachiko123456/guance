"""Frame-equivalence tests: the data actually fed into the policy core.

The deployment adapter must hand the vendored featurizer/ranker exactly the
frame the reference training/inference data contract defines (train_online
``_retrieval_context``): GAME-ABSOLUTE wire seats for ``my_seat``,
``public_counts``, ``known_hand_cards``, ``last_player`` and history ``pos``;
the current-trick fields derived from the CONFIRMED declaration's comparison
rank; the vendored featurizer and card memory relativize seats internally by
``my_seat``.  These tests build the reference-shaped context independently
from the train_online recipe and require byte-identical tensors through
``featurize_topk`` and identical policy logits.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest
import torch

from guance.contracts import (CanonicalPublicGameView,
                              CurrentWinningDeclarationFact, DeclaredFacts)
from guance.public_state_contract import PublicDeclaration
from guance.infrastructure.model.danks_v3.adapter import (
    declaration_to_follow_target, observation_context, wire_cards_to_labels,
)
from guance.infrastructure.model.danks_v3.core import (
    RecommendationEngine, normalize_engine_rows, engine_label)
from guance.infrastructure.model.danks_v3.loader import load_selector
from guance.infrastructure.model.danks_v3.vendor.danks.retrieval.cards import ALL_CARDS
from guance.infrastructure.model.danks_v3.vendor.danks.retrieval.context import (
    build_context,
)
from guance.infrastructure.model.danks_v3.vendor.danks.training.featurizer import (
    featurize_topk, history_features,
)
from guance.infrastructure.model.danks_v3.vendor.danks.training.schema import TOPK

ROOT = Path(__file__).resolve().parents[2]
PHASE1 = ROOT / "tests/fixtures/danks_v3"
CHECKPOINT_PATH = Path(os.environ.get(
    "GUANCE_DANKS_CHECKPOINT", "E:/Downloads/top10_selector_v3_online.pt"))
_real_checkpoint = pytest.mark.skipif(
    not CHECKPOINT_PATH.is_file() or not (PHASE1 / "samples_32.json").is_file(),
    reason="frozen checkpoint or samples not on this host")


def _wild03_view_and_declaration():
    samples = json.loads((PHASE1 / "samples_32.json").read_text(encoding="utf-8"))
    sample = next(row for row in samples["samples"]
                  if row["sample_id"] == "danks-p1-wild-03")
    view = CanonicalPublicGameView.from_dict(sample["observation"])
    declaration = PublicDeclaration.from_dict(
        sample["current_winning_declaration_fact"]["declaration"])
    return view, declaration


def _reference_recipe_context(view: CanonicalPublicGameView,
                              declaration: PublicDeclaration,
                              hand_labels) -> dict:
    """The train_online ``_retrieval_context`` recipe, applied to the same
    game position in the DANKS reflected seat frame (the engine kind is
    normalized through the same ``normalize_kind`` the reference applies)."""
    from guance.infrastructure.model.danks_v3.adapter import danks_seat
    from guance.infrastructure.model.danks_v3.vendor.danks.retrieval.rules import (
        normalize_kind)
    kind, rank, cards = declaration_to_follow_target(declaration)
    kind = normalize_kind(kind)
    my_danks = danks_seat(view.my_seat)
    counts_danks = [view.remaining[(-s) % 4] for s in range(4)]
    history = []
    for action in view.public_history:
        if action.kind == "pass":
            history.append({"pos": danks_seat(action.player), "kind": "PASS",
                            "rank": None, "cards": []})
        else:
            history.append({"pos": danks_seat(action.player),
                            "kind": {"1": "Single", "2": "Pair", "3": "Trips",
                                     "4": "Straight", "5": "TriplePlus",
                                     "6": "StraightPair", "7": "StraightTriple",
                                     "8": "Bomb", "9": "StraightFlush",
                                     "10": "FourKings"}.get(
                                str(action.combo_type), "Unknown"),
                            "rank": None,
                            "cards": wire_cards_to_labels(action.cards)})
    return {"my_seat": my_danks,
            "curRank": {1: "A", 2: "2", 10: "T", 11: "J", 12: "Q", 13: "K"}[
                view.level],
            "public_counts": counts_danks,
            "current_kind": kind, "current_rank": rank,
            "current_size": len(cards),
            "last_player": danks_seat(view.lead_action.player) if view.lead_action else None,
            "played_cards": [label for action in view.public_history
                             for label in wire_cards_to_labels(action.cards)],
            "known_hand_cards": {str(my_danks): list(hand_labels)},
            "history": history}


def test_adapter_context_matches_the_reference_recipe():
    view, declaration = _wild03_view_and_declaration()
    hand_labels = wire_cards_to_labels(view.hand)
    ours = observation_context(view, declaration)
    reference = _reference_recipe_context(view, declaration, hand_labels)
    for key, value in reference.items():
        assert ours[key] == value, key


@_real_checkpoint
def test_policy_core_tensors_are_frame_identical():
    """The state/candidates/mask tensors fed to policy_forward must be byte
    identical whether the context comes from the train_online recipe or the
    deployment adapter - this is the data the acceptance agent re-verifies
    from the real worker entry."""
    view, declaration = _wild03_view_and_declaration()
    hand_labels = wire_cards_to_labels(view.hand)
    reference_ctx = build_context(_reference_recipe_context(view, declaration,
                                                            hand_labels))
    ours_ctx = build_context(observation_context(view, declaration))
    moves_rows = None
    from guance.infrastructure.model.danks_v3.adapter import legal_follow_candidates
    legal = legal_follow_candidates(hand_labels, view.level, declaration)
    ranker_a = __import__("guance.infrastructure.model.danks_v3.vendor.danks.retrieval.ranker",
                          fromlist=["StructuralCandidateRanker"]
                          ).StructuralCandidateRanker()
    ranked_a = ranker_a.rank(hand_labels, legal, reference_ctx, top_k=TOPK)
    state_a, candidates_a, mask_a = featurize_topk(hand_labels, reference_ctx,
                                                   ranked_a, top_k=TOPK)
    history_a = history_features(_reference_recipe_context(view, declaration,
                                                           hand_labels)["history"],
                                 my_seat=view.my_seat)
    engine = RecommendationEngine(load_selector(CHECKPOINT_PATH))
    ours_ctx_dict = observation_context(view, declaration)
    result = engine.recommend(hand_labels, ours_ctx_dict, legal)
    assert result.feature_sha256[0] == __import__("hashlib").sha256(
        np.ascontiguousarray(state_a).tobytes()).hexdigest()
    assert result.feature_sha256[1] == __import__("hashlib").sha256(
        np.ascontiguousarray(candidates_a).tobytes()).hexdigest()
    assert result.feature_sha256[2] == __import__("hashlib").sha256(
        np.ascontiguousarray(mask_a).tobytes()).hexdigest()
    assert result.feature_sha256[3] == __import__("hashlib").sha256(
        np.ascontiguousarray(history_a).tobytes()).hexdigest()
    with torch.inference_mode():
        logits_ref = load_selector(CHECKPOINT_PATH).model.policy_forward(
            *(torch.from_numpy(value).unsqueeze(0)
              for value in (state_a, candidates_a, mask_a, history_a)))
    assert max(abs(a - b) for a, b in zip(result.logits,
                                          logits_ref[0].tolist())) <= 1e-5


@_real_checkpoint
def test_confirmed_comparison_rank_reaches_the_policy_core():
    """Fix (b): the current-trick rank fed into the state tensor must come
    from the CONFIRMED declaration, not the raw-lead best fit.  The wild-03
    declaration's comparison rank (K, engine 13) differs from the raw-lead
    best fit, so the trick-kind/rank one-hots differ between the two frames."""
    view, declaration = _wild03_view_and_declaration()
    hand_labels = wire_cards_to_labels(view.hand)
    ours = observation_context(view, declaration)
    assert ours["current_rank"] == "K"
    assert ours["current_kind"] == "TriplePlus"
    # The raw-lead best fit would have been the nine: prove the state tensor
    # one-hots carry the declared rank by comparing against a nine-target
    # context.
    from guance.infrastructure.model.danks_v3.vendor.danks.training.featurizer import (
        state_features)
    from guance.infrastructure.model.danks_v3.vendor.danks.retrieval.context import (
        build_context as _bc)
    declared_state = state_features(hand_labels, _bc(ours))
    nine_ctx = dict(ours)
    nine_ctx["current_rank"] = "9"
    nine_state = state_features(hand_labels, _bc(nine_ctx))
    from guance.infrastructure.model.danks_v3.vendor.danks.training.schema import (
        STATE_TRICK_KIND_OFFSET, RANK_DIM, LEVEL_RANK_DIM, CARD_DIM)
    start = CARD_DIM + LEVEL_RANK_DIM + STATE_TRICK_KIND_OFFSET - CARD_DIM - LEVEL_RANK_DIM
    trick_block_start = CARD_DIM + LEVEL_RANK_DIM
    rank_block_start = trick_block_start + 11  # TRICK_KIND_DIM
    assert list(declared_state[rank_block_start:rank_block_start + RANK_DIM]) \
        != list(nine_state[rank_block_start:rank_block_start + RANK_DIM])
