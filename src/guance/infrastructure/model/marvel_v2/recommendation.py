"""Real-checkpoint confirmed-decision recommendation (offline, evaluation-only).

Runs the loaded MARVEL V2 runtime over one strictly parsed confirmed decision:
the exact legal rows are regenerated from the frozen GD-RULES oracle, the real
autoregressive/cache path selects rows, the complete action is decoded and then
re-validated against own hand, legal set, declaration/wildcard semantics and the
rules oracle. No fallback, no synthesis, no partial action on failure.

The reported ``score`` is ``marvel_v2_policy_path_score``: the sum of natural-log
probabilities of ALL selected tokens (including the final COMMIT token) for the
complete action path. It is an unnormalized log-likelihood (<= 0, larger is
better); it is NOT a win rate, NOT a Q value and never a strength claim.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from time import perf_counter
from typing import Any

from guance.ports.model import ModelBackendError
from guance.public_state_contract import guandan_card_id_to_wire
from .action_protocol import ActionProtocolSession
from .contracts import COMMIT_TOKEN, PASS_TOKEN, MarvelEvent
from .decision_request import (
    ParsedConfirmedDecision, candidate_sequences_sha256, validate_confirmed_topology,
)
from .encoder import encode_channels
from .rules_oracle import VerifiedGuandanRulesOracle

SCORE_KIND = "marvel_v2_policy_path_score"
MAX_ACTION_STEPS = 33

_FAMILY_KIND = {
    "SINGLE": "single", "PAIR": "pair", "TRIPLE": "triple",
    "FULL_HOUSE": "full", "STRAIGHT": "straight",
    "STRAIGHT_FLUSH": "sflush", "PAIR_SEQUENCE": "serial_pair",
    "TRIPLE_SEQUENCE": "serial_triple", "RANK_BOMB": "bomb",
    "FOUR_KINGS": "rocket",
}


@dataclass(frozen=True, slots=True)
class RecommendationOutcome:
    kind: str
    family: str | None
    label: str
    cards: tuple[int, ...]
    declared_ranks: tuple[int, ...]
    comparison_rank: int | None
    declared_suit: str | None
    physical_length: int
    selected_row_index: int
    path_score: float
    selected_token_count: int
    elapsed_ms: float


def _step_fields(step_rows: Any, *, observer: int, channels: tuple[int, ...], legal_rows: Any,
                 torch: Any, device: Any, current: bool) -> dict[str, Any]:
    tokens = torch.tensor(step_rows, dtype=torch.long, device=device).reshape(1, 1, 4, 48)
    state = torch.zeros((1, 1, 4, 512), dtype=torch.uint8, device=device)
    if current:
        state[0, 0, observer] = torch.tensor(channels, dtype=torch.uint8, device=device)
    tokens_rows = torch.zeros((1, 1, 256, 8), dtype=torch.long, device=device)
    weights = torch.zeros((1, 1, 256), dtype=torch.float32, device=device)
    for index, (row, weight) in enumerate(zip(legal_rows.rows, legal_rows.weights)):
        tokens_rows[0, 0, index] = torch.tensor(row.values, dtype=torch.long, device=device)
        weights[0, 0, index] = float(weight)
    return {
        "num_steps": torch.tensor([1], dtype=torch.long, device=device),
        "player_ids": torch.tensor([[observer]], dtype=torch.long, device=device),
        "obs_tokens": tokens, "obs_channels": state,
        "avail_tokens": tokens_rows, "avail_weights": weights,
    }


def run_confirmed_recommendation(runtime: Any, parsed: ParsedConfirmedDecision, *,
                                 rules_root: Any, timeout_seconds: float | None = None,
                                 ) -> RecommendationOutcome:
    started = perf_counter()
    torch = runtime._torch
    device = runtime.device
    validate_confirmed_topology(parsed.marvel_state, parsed.current_declaration,
                                parsed.phase, parsed.history)
    oracle = VerifiedGuandanRulesOracle(rules_root)
    own_hand_wire = tuple(guandan_card_id_to_wire(card) for card in parsed.own_hand)
    candidates = oracle.legal_action_candidates(
        own_hand_wire, wire_level=parsed.level, phase=parsed.phase,
        current=parsed.current_declaration)
    if candidate_sequences_sha256(candidates) != parsed.candidates_sha256:
        raise ModelBackendError("MODEL_INPUT_UNSUPPORTED")
    session = ActionProtocolSession(
        parsed.session_id, parsed.state_version, parsed.phase, parsed.own_hand, candidates,
        confirmed_domain=None, prefix=(), token_step=parsed.token_step,
        committed_step=parsed.committed_step)
    rows = session.legal_rows
    if (rows.rows[0].generation != parsed.legal_generation
            or len(rows.rows) != parsed.legal_row_count):
        raise ModelBackendError("MODEL_INPUT_UNSUPPORTED")
    observer = parsed.marvel_state.observer_seat
    channels = encode_channels(parsed.marvel_state)
    history = parsed.history
    fields_sequence = [
        _step_fields(step.rows, observer=observer, channels=channels, legal_rows=rows,
                     torch=torch, device=device, current=index + 1 == len(history.steps))
        for index, step in enumerate(history.steps)
    ]
    probabilities, cache = runtime.forward_confirmed_stepwise(
        fields_sequence, timeout_seconds=timeout_seconds)
    speculative_cache = runtime.clone_confirmed_cache(cache)
    speculative_history = history
    selected_tokens: list[int] = []
    selected_probabilities: list[float] = []
    decoded = None
    selected_row_index = -1
    for _ in range(MAX_ACTION_STEPS):
        selected_row_index = int(probabilities.argmax().item())
        if selected_row_index >= len(rows.rows) or rows.weights[selected_row_index] <= 0:
            raise ModelBackendError("MODEL_OUTPUT_INVALID")
        row = rows.rows[selected_row_index]
        probability = float(probabilities[selected_row_index].item())
        if not 0.0 < probability <= 1.0:
            raise ModelBackendError("MODEL_OUTPUT_INVALID")
        selected_tokens.append(row.token)
        selected_probabilities.append(probability)
        try:
            step = session.select(row)
        except ModelBackendError:
            raise
        except Exception:
            raise ModelBackendError("MODEL_INFERENCE_FAILED") from None
        if step.committed_action is not None:
            decoded = step.committed_action
            break
        try:
            speculative_history = speculative_history.append(MarvelEvent(
                "PRIVATE_PREFIX", session.phase, observer, tokens=(row.token,)))
            session = step.session
            rows = session.legal_rows
        except ModelBackendError:
            raise
        except Exception:
            raise ModelBackendError("MODEL_INFERENCE_FAILED") from None
        probabilities = runtime.forward_confirmed_cached_step(
            speculative_cache,
            _step_fields(speculative_history.steps[-1].rows, observer=observer,
                         channels=channels, legal_rows=rows, torch=torch, device=device,
                         current=True),
            timeout_seconds=timeout_seconds)
    if decoded is None:
        raise ModelBackendError("MODEL_INFERENCE_FAILED")

    # Full semantic validation of the selected complete action.
    decoded.validate()
    if tuple(decoded.tokens) != tuple(selected_tokens):
        raise ModelBackendError("MODEL_OUTPUT_INVALID")
    own_hand = set(parsed.own_hand)
    if not set(decoded.physical_cards) <= own_hand:
        raise ModelBackendError("MODEL_OUTPUT_INVALID")
    if decoded.kind == "play":
        declaration = decoded.declaration
        if declaration is None:
            raise ModelBackendError("MODEL_OUTPUT_INVALID")
        declaration.validate(parsed.level)
        oracle.validate_resolved_play(declaration, wire_level=parsed.level,
                                      current=parsed.current_declaration)
        kind = _FAMILY_KIND.get(decoded.family or "")
        if kind is None:
            raise ModelBackendError("MODEL_OUTPUT_INVALID")
        cards = tuple(declaration.canonical_cards)
        declared_ranks = tuple(decoded.declared_ranks)
        label = str(decoded.family)
    elif decoded.kind == "pass":
        if parsed.phase != "FOLLOW_PLAY" or parsed.current_declaration is None:
            raise ModelBackendError("MODEL_OUTPUT_INVALID")
        kind, cards, declared_ranks, label = "pass", (), (), "PASS"
    else:
        # Exchange-phase recommendations are not enabled in Phase 3.
        raise ModelBackendError("MODEL_INPUT_UNSUPPORTED")
    elapsed_ms = (perf_counter() - started) * 1000.0
    # Complete-path policy score: the sum of natural-log probabilities of every
    # selected token (family/rank/card/wild tokens and the final COMMIT token).
    # It is an unnormalized log-likelihood (<= 0, larger is better), NOT a win
    # rate, NOT a Q value and never a strength claim.
    path_score = sum(math.log(probability) for probability in selected_probabilities)
    return RecommendationOutcome(
        kind=kind, family=decoded.family, label=label, cards=tuple(cards),
        declared_ranks=declared_ranks, comparison_rank=decoded.comparison_rank,
        declared_suit=decoded.declared_suit, physical_length=decoded.physical_length,
        selected_row_index=selected_row_index, path_score=path_score,
        selected_token_count=len(selected_tokens), elapsed_ms=elapsed_ms)


__all__ = ["MAX_ACTION_STEPS", "SCORE_KIND", "RecommendationOutcome",
           "run_confirmed_recommendation"]
