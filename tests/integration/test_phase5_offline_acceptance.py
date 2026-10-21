"""Phase 5 offline acceptance: fixed-input gate, strict eligibility, fail-closed.

This module covers the fast, deterministic part of the Phase 5 acceptance (no
model load): the fixed-input gate, the strict provider/validator eligibility
split for every confirmed decision from the fixed capture, fail-closed rejection
of the strict-ineligible decisions at the production projection entry and through
the controller submission path, adjacent malformed confirmed-decision payloads,
and the privacy contract of the bounded evidence.

The real iter2000 / FableDan recommendation run and the real-worker negative and
tamper matrix live in ``scripts/run_phase5_offline_acceptance.py`` so that the
whole-repository test run does not pay the multi-minute model cost; the script
uses the same production classes and writes bounded evidence to
``--evidence-root``.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from guance.contracts import EngineUpdate  # noqa: E402
from guance.infrastructure.model.errors import ModelBackendError  # noqa: E402
from guance.infrastructure.model.marvel_v2.decision_request import (  # noqa: E402
    decision_identity_sha256, parse_confirmed_decision, project_confirmed_decision,
)
from scripts.run_phase5_offline_acceptance import (  # noqa: E402
    EXPECTED_CAPTURE_SHA256, EXPECTED_PROVIDER_ELIGIBLE, EXPECTED_STRICT_ELIGIBLE,
    EXPECTED_STRICT_INELIGIBLE, MODEL_VERSION, EXPECTED_CHECKPOINT_SHA256,
    classify_decisions, replay_capture, validate_safe_output, verify_inputs,
)

_ALLOWED_REJECTION_CODES = {"MODEL_INPUT_INVALID", "MODEL_INPUT_UNSUPPORTED"}


@pytest.fixture(scope="module")
def fixed_acceptance() -> dict:
    gate = verify_inputs()
    replay = replay_capture()
    classification = classify_decisions(replay["decisions"])
    return {"gate": gate, "replay": replay, "classification": classification}


def test_phase5_input_gate_matches_pinned_fixed_inputs(fixed_acceptance):
    gate = fixed_acceptance["gate"]
    assert gate["capture_sha256"] == EXPECTED_CAPTURE_SHA256
    assert gate["capture_sidecar_state"] == "closed"
    assert gate["runtime_id"] == "marvel-e5c82e7c27fb-d7acbc8d76c9"
    assert gate["runtime_sha256sums_verified"] is True
    assert gate["runtime_sha256sums_entries"] == 189
    assert gate["runtime_pycache_directories"] == 0
    assert gate["deployment_sha256"] == \
        "44c69bd301c9883e5dc1b25268b26037a61c949d79d9d529cef7beca6fdf2f22"
    assert gate["checkpoint_sha256"] == EXPECTED_CHECKPOINT_SHA256
    assert gate["model_version"] == MODEL_VERSION
    assert gate["field_synthesis_used"] is False
    assert gate["live_input_used"] is False
    assert fixed_acceptance["replay"]["capture_unchanged"] is True


def test_phase5_strict_eligibility_split_is_exact(fixed_acceptance):
    replay = fixed_acceptance["replay"]
    classification = fixed_acceptance["classification"]
    assert replay["provider_eligible"] == EXPECTED_PROVIDER_ELIGIBLE
    assert len(classification["eligible"]) == EXPECTED_STRICT_ELIGIBLE
    assert len(classification["ineligible"]) == EXPECTED_STRICT_INELIGIBLE
    # Every ineligible decision is a conservative topology rejection of a FOLLOW
    # whose claimed current winner already emptied; never a hash/format accident.
    for record in classification["ineligible"]:
        assert record["phase"] == "FOLLOW_PLAY"
        assert record["code"] == "MODEL_INPUT_UNSUPPORTED"
        assert record["reason"] == "CURRENT_WINNER_EMPTIED"
        assert isinstance(record["decision_identity"], str)
        assert len(record["decision_identity"]) == 64
    identities = {record["context"].decision_identity for record in classification["eligible"]}
    identities |= {record["decision_identity"] for record in classification["ineligible"]}
    assert len(identities) == EXPECTED_PROVIDER_ELIGIBLE


def test_phase5_strict_ineligible_decisions_fail_closed_at_production_projection(fixed_acceptance):
    ineligible = fixed_acceptance["classification"]["ineligible_decisions"]
    assert len(ineligible) == EXPECTED_STRICT_INELIGIBLE
    for decision in ineligible:
        with pytest.raises(ModelBackendError) as exc:
            project_confirmed_decision(decision, artifact_id=MODEL_VERSION,
                                       artifact_sha256=EXPECTED_CHECKPOINT_SHA256)
        assert exc.value.code == "MODEL_INPUT_UNSUPPORTED"


def test_phase5_strict_ineligible_decision_clears_controller_without_submission(
        fixed_acceptance, tmp_path, monkeypatch):
    """The controller must clear advice/invalidate and never submit on a rejected projection."""
    from tests.integration.test_phase3_controller_wiring import (
        _decision, _observation, _switch_harness,
    )
    ineligible_decision = fixed_acceptance["classification"]["ineligible_decisions"][0]
    controller, vm, clients, state, app = _switch_harness(
        tmp_path, monkeypatch, initial_provider="guandan_marvel_v2")
    try:
        controller._decision_projector = lambda decision, artifact_id, artifact_sha256: (
            project_confirmed_decision(ineligible_decision, artifact_id=artifact_id,
                                       artifact_sha256=artifact_sha256))
        controller._handle_update(EngineUpdate("observation", observation=_observation()))
        assert controller._pending_confirmed_key is not None
        controller._maybe_submit_confirmed(_decision())
        assert clients[0].confirmed == []
        assert clients[0].submitted == []
        assert clients[0].invalidated >= 1
        assert not vm.adviceCandidates
    finally:
        controller.close()
        app.processEvents()


def test_phase5_adjacent_malformed_confirmed_decisions_fail_closed(fixed_acceptance):
    eligible = fixed_acceptance["classification"]["eligible"]
    base = copy.deepcopy(eligible[0]["payload"])
    follow = next(item for item in eligible if item["context"].phase == "FOLLOW_PLAY")["payload"]
    counterexamples = {}

    missing_history = copy.deepcopy(base)
    missing_history["history"]["steps"] = []
    counterexamples["missing_history"] = missing_history

    missing_rows = copy.deepcopy(base)
    missing_rows["session"]["legal_row_count"] = 0
    counterexamples["missing_legal_rows"] = missing_rows

    cache_mismatch = copy.deepcopy(base)
    cache_mismatch["cache_request"]["history"]["state_id"] = "phase5-tampered"
    counterexamples["cache_history_mismatch"] = cache_mismatch

    counter_mismatch = copy.deepcopy(base)
    counter_mismatch["session"]["token_step"] = int(base["session"]["token_step"]) + 1
    counterexamples["token_counter_mismatch"] = counter_mismatch

    missing_declaration = copy.deepcopy(follow)
    missing_declaration["current_winning_declaration"] = None
    state = missing_declaration["marvel_state"]
    state.update({"current_winner_seat": None, "current_family": None,
                  "current_comparison_rank": None, "current_winning_cards": [],
                  "current_winning_size": 0})
    counterexamples["missing_declaration"] = missing_declaration

    for name, payload in counterexamples.items():
        payload["decision_identity_sha256"] = decision_identity_sha256(payload)
        with pytest.raises(ModelBackendError) as exc:
            parse_confirmed_decision(payload)
        assert exc.value.code in _ALLOWED_REJECTION_CODES, name


def test_phase5_evidence_privacy_contract_rejects_content():
    safe = {"schema": "x", "count": 3, "decision_identity": "a" * 64,
            "records": [{"index": 1, "family": "bomb", "cards_count": 4,
                         "path_score": -1.25, "latency_ms": 10.0}]}
    validate_safe_output(safe)
    with pytest.raises(RuntimeError):
        validate_safe_output({"hidden_opponent_hands": [[1, 2]]})
    with pytest.raises(RuntimeError):
        validate_safe_output({"note": r"C:\Users\private\frames.jsonl"})
    with pytest.raises(RuntimeError):
        validate_safe_output({"history": [{"rows": [1, 2, 3]}]})
