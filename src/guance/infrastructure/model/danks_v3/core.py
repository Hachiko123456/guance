"""Offline inference core for the DanKS V3 top10 selector (D02).

Implements the phase 1 frozen action path exactly:

1. raw engine rows (``[kind, rank, cards]`` in engine label space, produced
   by the vendored ``Moves``/``python_rules``) are normalized row-by-row into
   ``ActionCandidate`` objects - a non-pass row that normalizes to PASS, a
   missing rank, missing cards or an empty list are hard errors;
2. the non-empty engine legal list is what reaches ``ranker.rank`` - the
   ranker's internal ``ActionGenerator`` is never a legal source here (the
   deployment never passes ``None``/empty, which would trigger it);
3. ``featurize_topk`` + ``history_features`` encode the frozen 444/121/15x61
   inputs; the policy forward runs under ``torch.inference_mode`` with a
   finiteness check;
4. the final action is the argmax-ranked candidate; scores are retrieval
   ranks and raw logits, never win rates, and never comparable across
   models.

Card labels are the DanKS label space (``S3``..``SK``/``SA``/``S2`` per suit
plus ``BJ``/``RJ``); wire-code translation is a later-phase adapter concern.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import time
from typing import Any, Iterable

import numpy as np
import torch

from .vendor.danks.retrieval.cards import normalize_cards
from .vendor.danks.retrieval.context import build_context
from .vendor.danks.retrieval.models import ActionCandidate
from .vendor.danks.retrieval.ranker import StructuralCandidateRanker
from .vendor.danks.retrieval.rules import normalize_kind, normalize_rank
from .vendor.danks.training.featurizer import featurize_topk, history_features
from .vendor.danks.training.schema import (
    CANDIDATE_DIM, FEATURE_VERSION, HISTORY_EVENT_DIM, HISTORY_LENGTH,
    STATE_DIM, TOPK,
)

# Engine card-label space uses SB (black) / HR (red) for jokers; DanKS uses
# BJ/RJ (python_rules emits SB/HR; cf. train_online._ENGINE_CARD).
ENGINE_TO_DANKS_JOKER = {"SB": "BJ", "HR": "RJ"}
DANKS_TO_ENGINE_JOKER = {"BJ": "SB", "RJ": "HR"}


class DanksV3CoreError(ValueError):
    """Core-path violation; carries a stable code, never echoes inputs."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class RankedCandidate:
    index: int  # slot in the Top10
    kind: str
    rank: str
    cards: tuple[str, ...]  # DanKS labels, original action order
    retrieval_score: float


@dataclass(frozen=True)
class Recommendation:
    hand: tuple[str, ...]
    legal_action_count: int
    top10: tuple[RankedCandidate, ...]
    selected: RankedCandidate
    logits: tuple[float, ...]
    feature_shapes: tuple[tuple[int, ...], ...]
    feature_sha256: tuple[str, ...]
    rank_ms: float
    featurize_ms: float
    forward_ms: float
    first_query: bool = False
    details: dict[str, Any] = field(default_factory=dict)


def engine_label(card: str) -> str:
    """DanKS label -> engine label (jokers only)."""
    return DANKS_TO_ENGINE_JOKER.get(card, card)


def to_danks_label(card: str) -> str:
    """Engine label -> DanKS label (jokers only)."""
    return ENGINE_TO_DANKS_JOKER.get(card, card)


def normalize_engine_rows(raw_rows: Iterable[Iterable[Any]]) -> list[ActionCandidate]:
    """Normalize raw engine action rows (phase 1 frozen semantics).

    Each row is ``[kind, rank, cards]`` from the vendored engine
    (``Moves.action_list``).  Mirrors ``_engine_legal_actions``: a PASS row
    stays a PASS candidate; a non-pass row that normalizes to PASS, a missing
    rank, empty cards or an empty row list are hard errors.  Card order is
    preserved as produced by the engine; the three-field comparison key is a
    caller-side projection (``three_field_key``).
    """
    rows = list(raw_rows)
    if not rows:
        raise DanksV3CoreError("LEGAL_ACTIONS_EMPTY",
                               "engine legal action list is empty")
    candidates: list[ActionCandidate] = []
    for index, row in enumerate(rows):
        if not isinstance(row, (list, tuple)) or len(row) != 3:
            raise DanksV3CoreError("LEGAL_ACTION_ROW_INVALID",
                                   f"illegal engine action row at {index}")
        kind_raw, rank_raw, cards_raw = row
        if str(kind_raw) == "PASS":
            candidates.append(ActionCandidate(index=index, kind="PASS",
                                              cards=(), rank="PASS"))
            continue
        kind = normalize_kind(str(kind_raw))
        if kind == "PASS":
            raise DanksV3CoreError("LEGAL_ACTION_ROW_INVALID",
                                   f"non-pass row normalized to PASS at {index}")
        rank = normalize_rank(rank_raw)
        if rank is None:
            raise DanksV3CoreError("LEGAL_ACTION_ROW_INVALID",
                                   f"engine action has no rank at {index}")
        try:
            cards = tuple(normalize_cards(
                to_danks_label(str(card)) for card in cards_raw))
        except ValueError as exc:
            raise DanksV3CoreError("LEGAL_ACTION_ROW_INVALID",
                                   f"unnormalized card label at {index}") from exc
        if not cards:
            raise DanksV3CoreError("LEGAL_ACTION_ROW_INVALID",
                                   f"engine action has no cards at {index}")
        candidates.append(ActionCandidate(index=index, kind=kind,
                                          cards=cards, rank=rank))
    return candidates


def three_field_key(action: ActionCandidate) -> tuple[str, str, tuple[str, ...]]:
    """Phase 1 comparison identity: kind, rank and sorted card labels.

    This key deliberately drops wildcard assignment identity and intra-action
    card order; callers that need the full action identity use ``cards`` and
    a declaration-side comparison instead.
    """
    return (action.kind, action.rank, tuple(sorted(action.cards)))


class RecommendationEngine:
    """One loaded model plus its retrieval ranker; CPU, eval, inference_mode."""

    def __init__(self, loaded: Any, *, top_k: int = TOPK) -> None:
        self._model = loaded.model
        self._loaded = loaded
        if loaded.feature_version != FEATURE_VERSION or \
                loaded.state_dim != STATE_DIM or \
                loaded.candidate_dim != CANDIDATE_DIM or \
                loaded.history_length != HISTORY_LENGTH or \
                loaded.history_event_dim != HISTORY_EVENT_DIM:
            raise DanksV3CoreError("MODEL_VERSION_UNSUPPORTED",
                                   "loaded contract differs from vendored schema")
        self._ranker = StructuralCandidateRanker()
        self._top_k = top_k
        self._warmed = False
        # Deployment-side reversible optimization (受限 A): parallel partition
        # precompute around the vendor ranker; identical outputs by
        # construction, vendor serial path stays the fallback.
        from .optimizations import install_parallel_partitions
        install_parallel_partitions(self._ranker)

    def recommend(self, hand_labels: Iterable[str],
                  context_state: dict[str, Any],
                  legal_actions: list[ActionCandidate] | None) -> Recommendation:
        hand = normalize_cards(hand_labels)
        # Fail-closed on the action source: None or empty would make the
        # ranker fall back to its internal ActionGenerator, which is a
        # different action source than the deployment contract allows.
        if legal_actions is None:
            raise DanksV3CoreError(
                "LEGAL_ACTIONS_MISSING",
                "engine legal action list is required; internal generation "
                "is not a permitted source")
        if not legal_actions:
            raise DanksV3CoreError("LEGAL_ACTIONS_EMPTY",
                                   "engine legal action list is empty")
        for candidate in legal_actions:
            if not isinstance(candidate, ActionCandidate):
                raise DanksV3CoreError("LEGAL_ACTION_ROW_INVALID",
                                       "legal list must contain ActionCandidate")
            if any(card not in set(hand) for card in candidate.cards):
                raise DanksV3CoreError(
                    "LEGAL_ACTION_NOT_IN_HAND",
                    "legal action uses cards outside the hand")
        ctx = build_context(dict(context_state))
        started = time.perf_counter()
        ranked = self._ranker.rank(hand, list(legal_actions), ctx, top_k=self._top_k)
        rank_ms = (time.perf_counter() - started) * 1000
        if not ranked:
            raise DanksV3CoreError("RANKER_EMPTY", "ranker returned no candidates")
        if len(ranked) > self._top_k:
            raise DanksV3CoreError("RANKER_OVERFLOW",
                                   f"ranker returned {len(ranked)} rows")
        started = time.perf_counter()
        state, candidates, mask = featurize_topk(hand, ctx, ranked,
                                                 top_k=self._top_k)
        history = history_features(context_state.get("history") or (),
                                   my_seat=int(context_state.get("my_seat", 0)))
        featurize_ms = (time.perf_counter() - started) * 1000
        shapes = tuple(tuple(value.shape) for value in (state, candidates, mask,
                                                        history))
        if shapes != ((STATE_DIM,), (self._top_k, CANDIDATE_DIM),
                      (self._top_k,), (HISTORY_LENGTH, HISTORY_EVENT_DIM)):
            raise DanksV3CoreError("FEATURE_SHAPE_INVALID",
                                   f"unexpected feature shapes {shapes}")
        started = time.perf_counter()
        with torch.inference_mode():
            logits = self._model.policy_forward(
                *(torch.from_numpy(value).unsqueeze(0)
                  for value in (state, candidates, mask, history)))
        forward_ms = (time.perf_counter() - started) * 1000
        if not torch.isfinite(logits).all().item():
            raise DanksV3CoreError("INFERENCE_NON_FINITE",
                                   "policy forward produced non-finite logits")
        selected_index = int(logits.argmax(-1).item())  # 0-based Top10 slot
        top = tuple(
            RankedCandidate(index=slot, kind=row.action.kind,
                            rank=row.action.rank, cards=tuple(row.action.cards),
                            retrieval_score=float(row.score))
            for slot, row in enumerate(ranked, 1))
        hashes = tuple(
            hashlib.sha256(np.ascontiguousarray(value).tobytes()).hexdigest()
            for value in (state, candidates, mask, history))
        first_query = not self._warmed
        self._warmed = True
        return Recommendation(
            hand=tuple(hand), legal_action_count=len(legal_actions), top10=top,
            selected=top[selected_index],
            logits=tuple(float(value) for value in logits[0].tolist()),
            feature_shapes=shapes, feature_sha256=hashes,
            rank_ms=rank_ms, featurize_ms=featurize_ms, forward_ms=forward_ms,
            first_query=first_query,
        )
