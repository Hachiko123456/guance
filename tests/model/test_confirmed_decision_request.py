"""Phase 3: typed request union, strict confirmed-decision wire DTO, dispatch.

Positive model behavior is covered by the real iter2000 acceptance test; this
module covers strictness/fail-closed boundaries:
* the pure request union and its identity coupling;
* the bounded parser rejecting unknown/injected/malformed/mismatched fields;
* client-side rejection before the wire;
* worker request-kind dispatch with unknown/mixed kinds and unknown fields.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from guance.contracts import (  # noqa: E402
    ConfirmedDecisionRecommendationRequest, ModelContractError, RequestIdentity,
)
from guance.infrastructure.model.errors import ModelBackendError  # noqa: E402
from guance.infrastructure.model.marvel_v2.decision_request import (  # noqa: E402
    MAX_CONFIRMED_HISTORY_STEPS, parse_confirmed_decision,
)
from guance.infrastructure.model.worker import dumps, envelope  # noqa: E402

import tests.integration.test_phase3_confirmed_recommendation as acceptance  # noqa: E402
from tests.model.test_worker import IDS, execute  # noqa: E402


@pytest.fixture(scope="module")
def baseline_payload():
    fixtures = acceptance._synthetic_fixtures()
    return copy.deepcopy(fixtures[0][0])


@pytest.fixture(scope="module")
def follow_payload():
    fixtures = acceptance._synthetic_fixtures()
    return copy.deepcopy(fixtures[9][0])  # follow-single (FOLLOW_PLAY)


@pytest.mark.parametrize("mutate", [
    lambda data: data.update({"current_winning_declaration": None}),
    lambda data: data["marvel_state"].update({"current_winner_seat": None}),
    lambda data: data["marvel_state"].update({"current_family": "PAIR"}),
    lambda data: data["marvel_state"].update({"current_comparison_rank": 12}),
    lambda data: data["marvel_state"].update({"current_winning_size": 2}),
    lambda data: data["marvel_state"].update(
        {"current_winning_cards": data["marvel_state"]["current_winning_cards"][:-1]}),
    lambda data: data.update({"current_winning_declaration": None,
                              "marvel_state": {**data["marvel_state"],
                                               "current_family": None,
                                               "current_comparison_rank": None,
                                               "current_winning_cards": [],
                                               "current_winning_size": 0,
                                               "current_winner_seat": None}}),
])
def test_follow_play_current_winning_consistency_fails_closed(follow_payload, mutate):
    payload = copy.deepcopy(follow_payload)
    mutate(payload)
    with pytest.raises(ModelBackendError):
        parse_confirmed_decision(payload)


def test_lead_play_rejects_current_winning_metadata_and_declaration(
        baseline_payload, follow_payload):
    declaration = follow_payload["current_winning_declaration"]
    payload = copy.deepcopy(baseline_payload)
    payload["current_winning_declaration"] = copy.deepcopy(declaration)
    with pytest.raises(ModelBackendError) as exc:
        parse_confirmed_decision(payload)
    assert exc.value.code == "MODEL_INPUT_UNSUPPORTED"
    payload = copy.deepcopy(baseline_payload)
    payload["marvel_state"]["current_winning_cards"] = [
        follow_payload["marvel_state"]["current_winning_cards"][0]]
    payload["marvel_state"]["current_family"] = "SINGLE"
    payload["marvel_state"]["current_comparison_rank"] = 6
    payload["marvel_state"]["current_winning_size"] = 1
    with pytest.raises(ModelBackendError):
        parse_confirmed_decision(payload)


def test_oracle_rejects_follow_without_current_winning():
    from guance.infrastructure.model.marvel_v2.rules_oracle import (
        RulesOracleError, VerifiedGuandanRulesOracle,
    )
    from guance.public_state_contract import wire_card_to_guandan_id

    oracle = VerifiedGuandanRulesOracle(acceptance.RUNTIME / "source")
    hand = (wire_card_to_guandan_id((2 << 4) | 8), wire_card_to_guandan_id((2 << 4) | 9))
    with pytest.raises(RulesOracleError) as exc:
        oracle.legal_action_candidates(hand, wire_level=2, phase="FOLLOW_PLAY", current=None)
    assert exc.value.code == "LIVE_CURRENT_WINNING_REQUIRED"
    winner = oracle.legal_action_candidates(
        (wire_card_to_guandan_id((1 << 4) | 6),), wire_level=2,
        phase="LEAD_PLAY", current=None)[0]
    from guance.infrastructure.model.marvel_v2.rules_oracle import _public_declaration
    with pytest.raises(RulesOracleError) as exc:
        oracle.legal_action_candidates(hand, wire_level=2, phase="LEAD_PLAY",
                                       current=_public_declaration(winner.declaration))
    assert exc.value.code == "LIVE_CURRENT_WINNING_UNEXPECTED"


def _reject(payload, code="MODEL_INPUT_INVALID"):
    with pytest.raises(ModelBackendError) as exc:
        parse_confirmed_decision(payload)
    assert exc.value.code == code


def test_confirmed_decision_payload_roundtrip(baseline_payload):
    parsed = parse_confirmed_decision(baseline_payload)
    assert parsed.session_id == "phase3-fixture"
    assert parsed.phase == "LEAD_PLAY"
    assert parsed.level == 2
    assert parsed.artifact_sha256 == acceptance.CHECKPOINT_SHA256
    assert parsed.legal_row_count >= 1 and parsed.own_hand
    assert parsed.decision_identity_sha256 == baseline_payload["decision_identity_sha256"]


def _tamper_history_row(data):
    data["history"]["steps"][0][1][0] = 999


@pytest.mark.parametrize("mutate", [
    lambda data: data.pop("session"),
    lambda data: data.update({"opponent_hands": [[1, 2, 3]]}),
    lambda data: data.update({"schema": "Unknown.v1"}),
    lambda data: data.update({"capture_secret": "x"}),
    lambda data: data.update({"frame_sequence": True}),
    lambda data: data["session"].update({"unknown": 1}),
    lambda data: data["session"].update({"own_hand": [999]}),
    lambda data: data["session"].update({"prefix": [8]}),
    lambda data: data["session"].update({"phase": "TRIBUTE"}),
    lambda data: data["session"].update({"legal_row_count": 0}),
    lambda data: data["model"].update({"artifact_sha256": "0" * 64}),
    lambda data: data["history"]["identity"].update({"session_id": "other"}),
    _tamper_history_row,
    lambda data: data["cache_request"].update({"request_id": "0" * 64}),
    lambda data: data["cache_request"].update({"sequence": 0}),
    lambda data: data.update({"decision_identity_sha256": "0" * 64}),
    lambda data: data["marvel_state"].update({"own_hand": [50, 51]}),
    lambda data: data["marvel_state"].update({"phase": "FOLLOW_PLAY"}),
])
def test_tampered_confirmed_decision_payloads_fail_closed(baseline_payload, mutate):
    payload = copy.deepcopy(baseline_payload)
    mutate(payload)
    with pytest.raises(ModelBackendError):
        parse_confirmed_decision(payload)


def test_oversized_history_is_rejected(baseline_payload):
    payload = copy.deepcopy(baseline_payload)
    step = payload["history"]["steps"][-1]
    payload["history"]["steps"] = [copy.deepcopy(step)] * (MAX_CONFIRMED_HISTORY_STEPS + 1)
    _reject(payload)


def test_request_union_identity_is_explicitly_coupled(baseline_payload):
    identity = RequestIdentity("", "phase3-fixture", "phase3-fixture-lead",
                               baseline_payload["state_version"])
    request = ConfirmedDecisionRecommendationRequest(identity, baseline_payload)
    request.validate()
    wrong = ConfirmedDecisionRecommendationRequest(
        RequestIdentity("", "different", "phase3-fixture-lead",
                        baseline_payload["state_version"]), baseline_payload)
    with pytest.raises(ModelContractError):
        wrong.validate()
    with pytest.raises(ModelContractError):
        ConfirmedDecisionRecommendationRequest(identity, {"schema": "only"}).validate()


def test_client_rejects_malformed_confirmed_payload_before_the_wire(tmp_path, baseline_payload):
    from tests.model.test_client import ControlledClient
    results, statuses = [], []
    client = ControlledClient(tmp_path, results, statuses)
    malformed = copy.deepcopy(baseline_payload)
    malformed["session"]["phase"] = "TRIBUTE"
    with pytest.raises(ModelBackendError):
        client.submit(ConfirmedDecisionRecommendationRequest(
            RequestIdentity("", "phase3-fixture", "phase3-fixture-lead",
                            malformed["state_version"]), malformed))
    assert client.sent == []
    client.close()


class _Advisor:
    def __init__(self, *, confirmed_error=None):
        self.confirmed_error = confirmed_error
        self.confirmed_calls = []

    def load(self):
        from guance.contracts import ModelMetadata
        return ModelMetadata(status="ready", provider="test", backend="synthetic",
                             runtime="test", model_version="v1")

    def recommend(self, view, request_id):
        raise AssertionError("public request not expected")

    def recommend_confirmed(self, decision, request_id):
        self.confirmed_calls.append(decision)
        if self.confirmed_error is not None:
            raise ModelBackendError(self.confirmed_error)
        raise ModelBackendError("MODEL_INFERENCE_FAILED")

    def close(self):
        return None


class _Factory:
    def __init__(self, advisor):
        self.advisor = advisor

    def validate(self, manifest):
        return None

    def create(self, bundle):
        return self.advisor


def _decision_ids(payload):
    return {**IDS, "session_id": payload["session_id"],
            "game_id": payload["game_id"], "state_version": payload["state_version"]}


def _worker_recommend(kind_fields, advisor, *, ids=None, load=True):
    messages = []
    if load:
        messages.append(dumps(envelope("load", IDS, source="synthetic")))
    messages.append(dumps(envelope("recommend", ids or IDS, **kind_fields)))
    messages.append(dumps(envelope("stop", IDS)))
    return execute(messages, factory=_Factory(advisor))


def test_worker_rejects_unknown_and_mixed_request_kind(baseline_payload):
    advisor = _Advisor()
    code, replies = _worker_recommend({"request_kind": "guess", "observation": {}}, advisor)
    assert any(item.get("type") == "error" and item.get("error_code") == "PROTOCOL_INVALID"
               for item in replies)
    assert advisor.confirmed_calls == []
    code, replies = _worker_recommend(
        {"request_kind": "public", "observation": {}, "decision": baseline_payload}, advisor)
    assert any(item.get("type") == "error" and item.get("error_code") == "PROTOCOL_INVALID"
               for item in replies)
    assert advisor.confirmed_calls == []


def test_worker_rejects_unknown_fields_on_confirmed_request(baseline_payload):
    advisor = _Advisor()
    code, replies = _worker_recommend({
        "request_kind": "confirmed_decision", "decision": baseline_payload,
        "opponent_hands": [[1, 2]]}, advisor)
    assert any(item.get("type") == "error" and item.get("error_code") == "PROTOCOL_INVALID"
               for item in replies)
    assert advisor.confirmed_calls == []


def test_worker_fableDan_family_rejects_confirmed_kind(baseline_payload):
    advisor = _Advisor(confirmed_error="REQUEST_KIND_UNSUPPORTED")
    code, replies = _worker_recommend({
        "request_kind": "confirmed_decision", "decision": baseline_payload},
        advisor, ids=_decision_ids(baseline_payload))
    errors = [item for item in replies if item.get("type") == "error"]
    assert errors and errors[0].get("error_code") == "REQUEST_KIND_UNSUPPORTED"
    assert len(advisor.confirmed_calls) == 1
    # The worker survives a bounded provider failure and still stops cleanly.
    assert any(item.get("type") == "status" and item.get("model_status") == "stopped"
               for item in replies)


def test_worker_known_kind_identity_mismatch_is_rejected(baseline_payload):
    advisor = _Advisor()
    code, replies = _worker_recommend({
        "request_kind": "confirmed_decision", "decision": baseline_payload}, advisor)
    assert any(item.get("type") == "error" and item.get("error_code") == "RESPONSE_MISMATCH"
               for item in replies)
    assert advisor.confirmed_calls == []


def test_follow_topology_seat_and_history_consistency(follow_payload):
    payload = copy.deepcopy(follow_payload)
    payload["marvel_state"]["current_winner_seat"] = payload["marvel_state"]["active_seat"]
    with pytest.raises(ModelBackendError) as exc:
        parse_confirmed_decision(payload)
    assert exc.value.code == "MODEL_INPUT_UNSUPPORTED"

    payload = copy.deepcopy(follow_payload)
    other = (payload["marvel_state"]["leader_seat"] + 1) % 4
    payload["marvel_state"]["leader_seat"] = other
    with pytest.raises(ModelBackendError) as exc:
        parse_confirmed_decision(payload)
    assert exc.value.code == "MODEL_INPUT_UNSUPPORTED"

    payload = copy.deepcopy(follow_payload)
    payload["marvel_state"]["consecutive_passes"] = 3
    with pytest.raises(ModelBackendError):
        parse_confirmed_decision(payload)


def test_lead_topology_requires_leader_equals_active(baseline_payload):
    payload = copy.deepcopy(baseline_payload)
    payload["marvel_state"]["leader_seat"] = (payload["marvel_state"]["active_seat"] + 1) % 4
    with pytest.raises(ModelBackendError) as exc:
        parse_confirmed_decision(payload)
    assert exc.value.code == "MODEL_INPUT_UNSUPPORTED"
    payload = copy.deepcopy(baseline_payload)
    payload["marvel_state"]["consecutive_passes"] = 1
    with pytest.raises(ModelBackendError):
        parse_confirmed_decision(payload)


def test_identity_binds_full_state_and_declaration(follow_payload):
    # Any state/declaration change without recalculation invalidates identity.
    payload = copy.deepcopy(follow_payload)
    payload["marvel_state"]["remaining_counts"][1] = 26
    with pytest.raises(ModelBackendError):
        parse_confirmed_decision(payload)
    payload = copy.deepcopy(follow_payload)
    payload["current_winning_declaration"]["declared_ranks"] = [7]
    with pytest.raises(ModelBackendError):
        parse_confirmed_decision(payload)


def _recompute_identity(payload):
    from guance.infrastructure.model.marvel_v2.decision_request import (
        decision_identity_sha256,
    )
    payload["decision_identity_sha256"] = decision_identity_sha256(payload)
    return payload


@pytest.mark.parametrize("mutate", [
    lambda data: data["marvel_state"].update({"leader_seat": 2}),
    lambda data: data["marvel_state"].update({"current_winner_seat": 2}),
    lambda data: data["marvel_state"].update({"consecutive_passes": 1}),
    lambda data: data.update({"frame_sequence": data["frame_sequence"] + 1}),
    lambda data: data["cache_request"].update({"request_key": "other-key"}),
])
def test_identity_rejects_unrecomputed_model_input_changes(follow_payload, mutate):
    """Every model-input field is bound by decision_identity_sha256."""
    payload = copy.deepcopy(follow_payload)
    mutate(payload)
    with pytest.raises(ModelBackendError):
        parse_confirmed_decision(payload)


def test_follow_rejects_emptied_winner_and_active(follow_payload):
    payload = copy.deepcopy(follow_payload)
    payload["marvel_state"]["emptied_seats"] = [
        payload["marvel_state"]["current_winner_seat"]]
    _recompute_identity(payload)
    with pytest.raises(ModelBackendError) as exc:
        parse_confirmed_decision(payload)
    assert exc.value.code == "MODEL_INPUT_UNSUPPORTED"

    payload = copy.deepcopy(follow_payload)
    payload["marvel_state"]["emptied_seats"] = [payload["marvel_state"]["active_seat"]]
    _recompute_identity(payload)
    with pytest.raises(ModelBackendError) as exc:
        parse_confirmed_decision(payload)
    assert exc.value.code == "MODEL_INPUT_UNSUPPORTED"


def test_follow_rejects_winner_not_the_latest_play_actor(follow_payload):
    payload = copy.deepcopy(follow_payload)
    # The exact history's latest play belongs to seat 1; seat 2 is not emptied.
    payload["marvel_state"]["current_winner_seat"] = 2
    _recompute_identity(payload)
    with pytest.raises(ModelBackendError) as exc:
        parse_confirmed_decision(payload)
    assert exc.value.code == "MODEL_INPUT_UNSUPPORTED"


def test_follow_rejects_trailing_pass_count_mismatch(follow_payload):
    # The follow fixture's history has zero passes after the winning play.
    payload = copy.deepcopy(follow_payload)
    payload["marvel_state"]["consecutive_passes"] = 1
    _recompute_identity(payload)
    with pytest.raises(ModelBackendError) as exc:
        parse_confirmed_decision(payload)
    assert exc.value.code == "MODEL_INPUT_UNSUPPORTED"


@pytest.mark.parametrize("mutate", [
    lambda data: data["marvel_state"].update(
        {"current_winner_seat": data["marvel_state"]["active_seat"]}),
    lambda data: data["marvel_state"].update(
        {"leader_seat": (data["marvel_state"]["active_seat"] + 2) % 4}),
    lambda data: data["marvel_state"].update(
        {"emptied_seats": [data["marvel_state"]["current_winner_seat"]]}),
    lambda data: data["marvel_state"].update({"consecutive_passes": 1}),
])
def test_recomputed_identity_does_not_legalize_impossible_topology(
        follow_payload, mutate):
    payload = copy.deepcopy(follow_payload)
    mutate(payload)
    _recompute_identity(payload)
    with pytest.raises(ModelBackendError) as exc:
        parse_confirmed_decision(payload)
    assert exc.value.code == "MODEL_INPUT_UNSUPPORTED"


def test_lead_rejects_emptied_active_seat(baseline_payload):
    payload = copy.deepcopy(baseline_payload)
    payload["marvel_state"]["emptied_seats"] = [payload["marvel_state"]["active_seat"]]
    _recompute_identity(payload)
    with pytest.raises(ModelBackendError) as exc:
        parse_confirmed_decision(payload)
    assert exc.value.code == "MODEL_INPUT_UNSUPPORTED"


def test_lead_rejects_open_previous_trick_history():
    """A LEAD_PLAY state is impossible while the exact history's last play is open."""
    from guance.infrastructure.model.marvel_v2.rules_oracle import (
        VerifiedGuandanRulesOracle,
    )
    from guance.public_state_contract import guandan_card_id_to_wire, wire_card_to_guandan_id

    def wc(suit, rank):
        return wire_card_to_guandan_id((suit << 4) | rank)

    oracle = VerifiedGuandanRulesOracle(acceptance.RUNTIME / "source")
    winning = next(
        candidate for candidate in oracle.legal_action_candidates(
            tuple(guandan_card_id_to_wire(card) for card in (wc(1, 6),)),
            wire_level=2, phase="LEAD_PLAY", current=None)
        if candidate.declaration is not None and candidate.declaration.family == "SINGLE")
    payload, _context = acceptance._synthetic_payload(
        fixture_id="lead-open-trick", game_id="phase3-fixture-follow",
        state_version=900, observer=1, level=2, phase="FOLLOW_PLAY",
        own_hand_ids=(wc(2, 8), wc(2, 9)), winning_candidate=winning,
        sequence=954, fixture_index=900)
    payload["session"]["phase"] = "LEAD_PLAY"
    state = payload["marvel_state"]
    state.update({
        "phase": "LEAD_PLAY", "current_winner_seat": None,
        "current_family": None, "current_comparison_rank": None,
        "current_winning_cards": [], "current_winning_size": 0,
    })
    payload["current_winning_declaration"] = None
    _recompute_identity(payload)
    with pytest.raises(ModelBackendError) as exc:
        parse_confirmed_decision(payload)
    assert exc.value.code == "MODEL_INPUT_UNSUPPORTED"


# ---------------------------------------------------------------------------
# Phase 3 Repair-3: fixed exact-history trick-topology acceptance tests.
#
# Every payload is built with the production serializer path (real exact-history
# journal, real rules-oracle candidates, recomputed history/candidates/cache/
# decision identities). Only the claimed topology is impossible; rejection must
# therefore come from the topology validator, never from a hash mismatch.
# ---------------------------------------------------------------------------

def _identity_bound(payload):
    from guance.infrastructure.model.marvel_v2.decision_request import (
        decision_identity_sha256,
    )
    assert payload["decision_identity_sha256"] == decision_identity_sha256(payload)
    return payload


def _reject_topology(payload):
    with pytest.raises(ModelBackendError) as exc:
        parse_confirmed_decision(payload)
    assert exc.value.code == "MODEL_INPUT_UNSUPPORTED"
    return exc.value


def test_r1_lead_requires_fully_closed_previous_trick():
    """R1: previous trick's last play followed by only one pass is not closed."""
    _reject_topology(_identity_bound(acceptance._trick_counterexample_r1()))


def test_r2_lead_requires_winner_or_inherited_leader():
    """R2: a new LEAD must be the previous winner or the inherited partner."""
    _reject_topology(_identity_bound(acceptance._trick_counterexample_r2()))


def test_r3_follow_rejects_just_passed_active_actor():
    """R3: FOLLOW active must be the exact-history next legal actor."""
    _reject_topology(_identity_bound(acceptance._trick_counterexample_r3()))


@pytest.mark.parametrize(
    "builder", [builder for _label, builder in acceptance.TRICK_COUNTEREXAMPLE_BUILDERS],
    ids=[label for label, _builder in acceptance.TRICK_COUNTEREXAMPLE_BUILDERS])
def test_fixed_trick_counterexamples_rejected_with_recomputed_identity(builder):
    """Counterexample 12: fully recomputed identities never legalize topology."""
    _reject_topology(_identity_bound(builder()))


@pytest.mark.parametrize(
    "builder", [builder for _label, builder in acceptance.TRICK_POSITIVE_BUILDERS],
    ids=[label for label, _builder in acceptance.TRICK_POSITIVE_BUILDERS])
def test_fixed_trick_positive_controls_are_accepted(builder):
    parsed = parse_confirmed_decision(_identity_bound(builder()))
    assert parsed.phase in {"LEAD_PLAY", "FOLLOW_PLAY"}
