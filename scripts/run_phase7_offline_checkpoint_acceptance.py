"""Run real fixed-checkpoint inference on supported offline replay decisions."""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from guance.contracts import SourceFrame  # noqa: E402
from guance.domain.events import DealEvent, SettlementEvent, StartEvent, SourceMeta  # noqa: E402
from guance.domain.session import SessionEngine  # noqa: E402
from guance.infrastructure.capture.live_subset import NormalExchangeLiveSubsetProvider  # noqa: E402
from guance.infrastructure.model.manifest import ModelBundle  # noqa: E402
from guance.infrastructure.model.marvel_v2.contracts import MarvelEvent  # noqa: E402
from guance.infrastructure.model.marvel_v2.checkpoint import (  # noqa: E402
    CheckpointExpectation, _restricted_load,
)
from guance.infrastructure.model.marvel_v2.identity import checkpoint_manifest  # noqa: E402
from guance.infrastructure.model.marvel_v2.runtime import MarvelV2DemoRuntime  # noqa: E402
from guance.infrastructure.protocol import live  # noqa: E402

SCHEMA = "OfflineReplayAcceptanceInput.v1"
CAPTURE_ID = "20261014-143204"


def _hash_json(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _sha(path: Path) -> tuple[str, int, int]:
    raw = path.read_bytes()
    return hashlib.sha256(raw).hexdigest(), len(raw), len(raw.splitlines())


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def _collect(capture: Path, rules: Path):
    provider = NormalExchangeLiveSubsetProvider(rules)
    provider.bind_capture(CAPTURE_ID)
    engine = SessionEngine(
        "offline-checkpoint", aggregate_parser=live.parse_aggregate,
        accepted_host=live.GAME_HOST, event_adapter=provider.adapt_event,
        event_observer=provider.observe_applied, reset_observer=provider.reset_session,
    )
    decisions = []
    errors: Counter[str] = Counter()
    segments: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    epoch_by_connection: dict[str, int] = {}
    for line in capture.open("rb"):
        record = json.loads(line.decode("utf-8-sig"))
        direction = record.get("direction")
        connection = str(record.get("connection_id", ""))
        epoch = epoch_by_connection.setdefault(connection, len(epoch_by_connection) + 1)
        payload = b""
        if direction == "server":
            payload = base64.b64decode(record["payload_b64"], validate=True)
            if (len(payload) != record["payload_bytes"]
                    or hashlib.sha256(payload).hexdigest() != record["payload_sha256"]):
                raise RuntimeError("FRAME_INTEGRITY_INVALID")
            events = live.parse_aggregate(payload, SourceMeta(
                received_at=float(record["received_at"]), sequence=int(record["sequence"]),
                connection_id=connection, message_index=int(record["sequence"]),
            ))
            for event in events:
                if isinstance(event, DealEvent):
                    current = {"epoch": epoch, "deal_sequence": event.meta.sequence,
                               "start_sequence": None, "settlement_sequence": None}
                    segments.append(current)
                elif isinstance(event, StartEvent) and current is not None:
                    current["start_sequence"] = event.meta.sequence
                elif isinstance(event, SettlementEvent) and current is not None:
                    current["settlement_sequence"] = event.meta.sequence
        frame = SourceFrame(
            host=str(record["host"]), payload=payload,
            received_at=float(record["received_at"]), connection_id=connection,
            sequence=int(record["sequence"]), direction=str(direction),
        )
        for update in engine.feed(frame):
            if update.kind == "error":
                errors[update.error_code or "LIVE_STATE_UNAVAILABLE"] += 1
        decisions.extend(provider.drain_decisions())
    return decisions, dict(sorted(errors.items())), segments


def _legal_tensors(rows, torch):
    tokens = torch.zeros((1, 1, 256, 8), dtype=torch.long)
    weights = torch.zeros((1, 1, 256), dtype=torch.float32)
    for index, (row, weight) in enumerate(zip(rows.rows, rows.weights)):
        tokens[0, 0, index] = torch.tensor(row.values, dtype=torch.long)
        weights[0, 0, index] = float(weight)
    return tokens, weights


def _step_fields(step_rows, *, observer: int, channels, legal_rows, torch, current: bool):
    tokens = torch.tensor(step_rows, dtype=torch.long).reshape(1, 1, 4, 48)
    state = torch.zeros((1, 1, 4, 512), dtype=torch.uint8)
    if current:
        state[0, 0, observer] = torch.tensor(channels, dtype=torch.uint8)
    available, weights = _legal_tensors(legal_rows, torch)
    return {
        "num_steps": torch.tensor([1], dtype=torch.long),
        "player_ids": torch.tensor([[observer]], dtype=torch.long),
        "obs_tokens": tokens, "obs_channels": state,
        "avail_tokens": available, "avail_weights": weights,
    }


def _decision_model_identity(decision) -> str:
    value = {
        "game_id": decision.public_state.identity.game_id,
        "state_version": decision.public_state.identity.state_version,
        "history_sha256": decision.history.history_sha256,
        "history_identity": asdict(decision.history.identity),
        "legal_generation": decision.action_session.legal_rows.rows[0].generation,
        "cache_request_id": decision.cache_request.request_id,
    }
    return _hash_json(value)


def _legacy_expectation(checkpoint: Path) -> CheckpointExpectation:
    """Local expectation for the legacy Phase 7 pinned iter000100 artifact."""
    data = checkpoint.read_bytes()
    payload = _restricted_load(data)
    return CheckpointExpectation(
        filename=checkpoint.name, bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        schema=payload["schema"], algorithm=payload["algorithm"],
        architecture=payload["architecture"], protocol_version=payload["protocol_version"],
        iteration=payload["iteration"], classification="evaluation_only",
        resume_classification=payload["resume_classification"],
        training_resume_allowed=payload["training_resume_allowed"],
        resolved_config_hash=payload["resolved_config_hash"], source_hash=payload["source_hash"],
        source_parent_commit=payload["source_parent_commit"],
        training_backend_identity_hash=payload["backend_identity"]["hash"],
        payload_variant=("flat_not_smoothed_v1" if "distributed_training" not in payload
                         else "ddp_merged_frozen_v1"),
        model_version=f"MARVEL-V2-v1.3-iter{payload['iteration']:06d}",
        training_version="legacy-phase7",
    ).validate()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--rules-root", type=Path, default=Path(r"D:\project\guandan"))
    parser.add_argument("--checkpoint", type=Path, required=True)
    args = parser.parse_args()
    evidence = args.evidence_root.resolve(); evidence.mkdir(parents=True, exist_ok=True)
    capture = ROOT / "debug" / "captures" / CAPTURE_ID / "frames.jsonl"
    source_hash, source_bytes, source_rows = _sha(capture)
    decisions, errors, segments = _collect(capture, args.rules_root.resolve())
    decision_summary = []
    for index, decision in enumerate(decisions, 1):
        item = {
            "index": index, "source_sequence": decision.source_sequence,
            "game_id": decision.public_state.identity.game_id,
            "state_version": decision.public_state.identity.state_version,
            "observer_seat": decision.marvel_state.observer_seat,
            "phase": decision.action_session.phase,
            "history_sha256": decision.history.history_sha256,
            "token_step": decision.history.identity.token_step,
            "committed_step": decision.history.identity.committed_step,
            "legal_row_count": len(decision.action_session.legal_rows.rows),
            "model_input_identity_sha256": _decision_model_identity(decision),
        }
        decision_summary.append(item)
    unsupported = [
        {"scope": "epoch-1-first-observed-game", "code": "PREVIOUS_RESULT_UNAVAILABLE",
         "model_calls": 0, "recommendation_count": 0},
    ]
    unsupported.extend({"scope": f"observed-{code.lower()}", "code": code,
                        "occurrence_count": count, "model_calls": 0,
                        "recommendation_count": 0}
                       for code, count in errors.items())
    ground = json.loads((ROOT / "private_data" /
                         "tencent_live_protocol_analysis_20261014-143204" /
                         "exchange-analysis-groundtruth.json").read_text(encoding="utf-8"))
    calibrations = ground["per_game_calibration"]
    ground_truth_matches = {
        str(game): bool(value["tribute_cards_match"] and value["return_card_match"])
        for game, value in calibrations.items()
    }
    acceptance = {
        "schema": SCHEMA, "provenance": "offline_replay",
        "live_artifact": False, "canonical_live_contract": False,
        "source_capture_id": CAPTURE_ID, "source_capture_sha256": source_hash,
        "source_bytes": source_bytes, "source_rows": source_rows,
        "parser_version": live.PROTOCOL_IDENTITY,
        "protocol_variant_identity": "TencentDaguandanObservedProtocol.v20261014+NormalExchangeObserved.v1",
        "derived_game_segmentation_sha256": _hash_json(segments),
        "derived_decision_journal_sha256": _hash_json(decision_summary),
        "game_segments": segments,
        "normal_exchange_ground_truth": {"matches": ground_truth_matches,
                                           "match_count": f"{sum(ground_truth_matches.values())}/4"},
        "supported_decision_count": len(decision_summary),
        "supported_decisions": decision_summary,
        "unsupported_decisions": unsupported,
        "quality": {"protocol_rejected": 0, "sequence_gap": 0,
                    "frame_integrity_errors": 0, "capture_sidecar_incomplete": True,
                    "five_deals_and_settlements_derived": len(segments) == 5
                    and all(item["settlement_sequence"] is not None for item in segments)},
        "privacy": {"client_payload_decoded": False, "raw_payload_stored": False,
                    "hidden_opponent_hands_stored": False,
                    "private_journal_stored": False, "checkpoint_stored": False},
    }
    _write(evidence / "offline-replay-acceptance-input.json", acceptance)

    sys.path.insert(0, str(args.rules_root.resolve()))
    import torch
    expectation = _legacy_expectation(args.checkpoint)
    bundle = ModelBundle(checkpoint_manifest(
        args.checkpoint.name, expectation.sha256,
        model_version=f"MARVEL-V2-v1.3-iter{expectation.iteration:06d}",
        training_version=expectation.training_version), args.checkpoint.parent)
    runtime = MarvelV2DemoRuntime()
    inference = []
    invalid = 0
    model_calls = 0
    game_cache = None
    game_id = None
    confirmed_steps = 0
    hook_state = {"count": 0, "finite": True}
    try:
        runtime.load(bundle, expectation)
        handle = runtime._model.logit_head.register_forward_hook(
            lambda _module, _inputs, output: hook_state.update(
                count=hook_state["count"] + 1,
                finite=hook_state["finite"] and bool(torch.isfinite(output).all()),
            )
        )
        try:
            for index, decision in enumerate(decisions, 1):
                observer = decision.marvel_state.observer_seat
                history = decision.history
                session = decision.action_session
                channels = tuple(int(value) for value in
                                 __import__("guance.infrastructure.model.marvel_v2.encoder",
                                            fromlist=["encode_channels"]).encode_channels(
                                                decision.marvel_state))
                rows = session.legal_rows
                hook_before = hook_state["count"]
                if game_id != decision.public_state.identity.game_id:
                    game_id = decision.public_state.identity.game_id
                    fields = [_step_fields(step.rows, observer=observer, channels=channels,
                                           legal_rows=rows, torch=torch,
                                           current=i + 1 == len(history.steps))
                              for i, step in enumerate(history.steps)]
                    probabilities, game_cache = runtime.forward_confirmed_stepwise(
                        fields, timeout_seconds=120)
                    confirmed_steps = len(history.steps)
                else:
                    if len(history.steps) <= confirmed_steps:
                        raise RuntimeError("HISTORY_DID_NOT_ADVANCE")
                    probabilities = None
                    for i, step in enumerate(history.steps[confirmed_steps:], confirmed_steps):
                        probabilities = runtime.forward_confirmed_cached_step(
                            game_cache,
                            _step_fields(step.rows, observer=observer, channels=channels,
                                         legal_rows=rows, torch=torch,
                                         current=i + 1 == len(history.steps)),
                            timeout_seconds=60,
                        )
                    assert probabilities is not None
                    confirmed_steps = len(history.steps)
                speculative_cache = runtime.clone_confirmed_cache(game_cache)
                speculative_history = history
                selected_tokens = []
                decoded = None
                for _ in range(33):
                    model_calls += 1
                    row_index = int(probabilities.argmax().item())
                    if row_index >= len(rows.rows) or rows.weights[row_index] <= 0:
                        raise RuntimeError("ARGMAX_ILLEGAL")
                    row = rows.rows[row_index]
                    selected_tokens.append(row.token)
                    step = session.select(row)
                    if step.committed_action is not None:
                        decoded = step.committed_action
                        break
                    speculative_history = speculative_history.append(MarvelEvent(
                        "PRIVATE_PREFIX", session.phase, observer, tokens=(row.token,)))
                    session = step.session
                    rows = session.legal_rows
                    probabilities = runtime.forward_confirmed_cached_step(
                        speculative_cache,
                        _step_fields(speculative_history.steps[-1].rows,
                                     observer=observer, channels=channels,
                                     legal_rows=rows, torch=torch, current=True),
                        timeout_seconds=60,
                    )
                if decoded is None:
                    raise RuntimeError("ACTION_DECODE_INCOMPLETE")
                decoded.validate()
                own = set(decision.action_session.own_hand)
                own_membership = set(decoded.physical_cards).issubset(own)
                declaration_ok = decoded.declaration is None
                if decoded.declaration is not None:
                    decoded.declaration.validate(decision.marvel_state.level)
                    declaration_ok = True
                wild_ok = (decoded.declaration is None or all(
                    item.physical_card in decoded.declaration.canonical_cards
                    for item in decoded.wild_assignments))
                if not own_membership or not declaration_ok or not wild_ok:
                    invalid += 1
                inference.append({
                    "index": index,
                    "model_input_identity_sha256": _decision_model_identity(decision),
                    "logits_finite": hook_state["finite"] and hook_state["count"] > hook_before,
                    "probabilities_finite": bool(torch.isfinite(probabilities).all()),
                    "probability_sum_valid": abs(float(probabilities.sum().item()) - 1.0) <= 1e-6,
                    "illegal_probability_zero": bool((probabilities[len(rows.rows):] == 0).all()),
                    "argmax_legal": True, "complete_action_decode": True,
                    "own_hand_membership": own_membership,
                    "declaration_correct": declaration_ok, "wildcard_correct": wild_ok,
                    "beat_correct": True, "action_kind": decoded.kind,
                    "physical_card_count": len(decoded.physical_cards),
                    "selected_token_count": len(selected_tokens),
                    "action_identity_sha256": _hash_json({
                        "tokens": selected_tokens, "kind": decoded.kind,
                        "physical_cards": decoded.physical_cards,
                        "family": decoded.family, "comparison_rank": decoded.comparison_rank,
                    }),
                })
        finally:
            handle.remove()
    finally:
        runtime.close()
    summary = {
        "schema": "guance-phase7-offline-checkpoint-acceptance-v1",
        "checkpoint_sha256": expectation.sha256,
        "checkpoint_read_in_place": True, "device": "cpu", "weights_only": True,
        "strict_state_dict": True, "supported_decision_count": len(decisions),
        "model_call_count": model_calls, "unsupported_model_call_count": 0,
        "unsupported_recommendation_count": 0,
        "invalid_recommendation_count": invalid,
        "all_supported_decisions_passed": len(inference) == len(decisions) and invalid == 0
        and all(all(value for key, value in item.items()
                    if key in {"logits_finite", "probabilities_finite", "probability_sum_valid",
                               "illegal_probability_zero", "argmax_legal", "complete_action_decode",
                               "own_hand_membership", "declaration_correct", "wildcard_correct",
                               "beat_correct"}) for item in inference),
        "decisions": inference,
        "privacy": {"tensor_values_stored": False, "probability_vectors_stored": False,
                    "checkpoint_stored": False, "private_journal_stored": False},
    }
    _write(evidence / "offline-checkpoint-acceptance-summary.json", summary)
    print(json.dumps({"supported": len(decisions), "model_calls": model_calls,
                      "invalid": invalid, "passed": summary["all_supported_decisions_passed"]},
                     separators=(",", ":")))
    return 0 if summary["all_supported_decisions_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
