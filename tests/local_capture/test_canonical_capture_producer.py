from __future__ import annotations

from dataclasses import fields, replace
import json

import pytest

from guance.contracts import SourceFrame
from guance.infrastructure.capture import canonical_capture as module
from guance.infrastructure.capture.canonical_capture import (
    CanonicalCaptureError, CanonicalCaptureConverter, ConfirmedActionTransition,
    ConfirmedDecisionInput, strict_json_loads, validate_decision_record,
)
from guance.public_state_contract import (
    CommittedActionV2, EvidenceRef, HistoryEvidence, Knowledge, unsupported_exchange,
)
from tests.local_capture.canonical_capture_fixture import (
    create_capture, decision_input, finish_capture, record_confirmed,
)


def test_real_producer_writes_eligible_v2_from_confirmed_objects(tmp_path):
    recorder, converter, source, capture_id = create_capture(tmp_path)
    record_confirmed(converter, decision_input(capture_id))
    finish_capture(recorder)
    result = converter.close()
    assert result.status == "eligible" and result.decision_count == 1
    assert result.missing_fact_codes == () and len(result.manifest_sha256) == 64
    manifest = json.loads((source.parent / "phase7_acceptance_input.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == "CanonicalLocalCaptureAcceptanceInput.v2"
    assert manifest["producer"] == "guance.CanonicalCaptureConverter"
    assert manifest["producer_version"] == "2"
    assert manifest["eligibility"] == "eligible"
    assert manifest["capabilities"] == {
        "committed_step": "verified", "declaration": "verified",
        "exact_four_observer_history": "verified", "legal_action_counters": "verified",
        "private_prefix": "verified", "public_self_canonical": "verified",
        "token_step": "verified", "tribute_return": "not_applicable",
    }
    assert set(manifest["quality"].values()) == {"verified"}
    assert not (source.parent / "phase7_acceptance_status.json").exists()
    record = strict_json_loads((source.parent / "phase7_canonical_decisions.jsonl").read_text(
        encoding="utf-8").splitlines()[0])
    validate_decision_record(record)
    assert record["public_self_state_contract"] == "CanonicalGuandanPublicState.v2"
    assert record["history_branch_id"] == "confirmed"
    assert record["quality"]["no_gap"] == "unknown"
    assert "hidden_opponent_hands" not in json.dumps(record, sort_keys=True)


def test_multiple_decisions_allow_explained_nondecreasing_action_counters(tmp_path):
    recorder, converter, _source, capture_id = create_capture(tmp_path)
    record_confirmed(converter, decision_input(capture_id))
    recorder.append(SourceFrame(
        host="hlxyxws.huanle.qq.com", payload=b"synthetic-frame-2", received_at=2.0,
        connection_id="connection-1", sequence=2, direction="server"))
    record_confirmed(converter, decision_input(
        capture_id, frame_sequence=2, state_version_delta=1, request_sequence=2))
    finish_capture(recorder, 2)
    result = converter.close()
    assert result.status == "eligible" and result.decision_count == 2


def test_bool_as_int_and_counter_rollback_fail_closed(tmp_path):
    recorder, converter, _source, capture_id = create_capture(tmp_path)
    with pytest.raises(CanonicalCaptureError, match="^FRAME_SEQUENCE_INVALID$"):
        converter.record_decision(replace(decision_input(capture_id), frame_sequence=True))
    finish_capture(recorder)
    result = converter.close()
    assert result.status == "blocked"
    assert "FRAME_SEQUENCE_INVALID" in result.missing_fact_codes


def test_observer_identity_missing_or_untrusted_fails_closed(tmp_path):
    recorder, converter, _source, capture_id = create_capture(tmp_path)
    item = decision_input(capture_id)
    missing = replace(item.public_state, observer_seat=Knowledge.unknown("observer_missing"))
    with pytest.raises(CanonicalCaptureError, match="^OBSERVER_IDENTITY_UNAVAILABLE$"):
        converter.record_decision(replace(item, public_state=missing))
    finish_capture(recorder)
    assert converter.close().status == "blocked"
    untrusted = replace(item.public_state, observer_seat=Knowledge.known(
        item.public_state.observer_seat.value, EvidenceRef("fake.source")))
    recorder2, converter2, _source2, capture_id2 = create_capture(tmp_path / "second")
    with pytest.raises(CanonicalCaptureError, match="^OBSERVER_EVIDENCE_UNTRUSTED$"):
        converter2.record_decision(replace(decision_input(capture_id2), public_state=untrusted))
    finish_capture(recorder2)
    assert converter2.close().status == "blocked"


def test_transition_requires_next_actor_and_pass_reset():
    evidence = EvidenceRef("semantic.action")
    missing_next = CommittedActionV2(
        1, 0, "FOLLOW_PLAY", "pass", (), Knowledge.absent(evidence),
        Knowledge.unknown("next_missing"), Knowledge.known(27, evidence), evidence,
    )
    with pytest.raises(CanonicalCaptureError, match="^NEXT_ACTOR_UNAVAILABLE$"):
        ConfirmedActionTransition(1, 1, 0, evidence).validate(missing_next)
    known_next = replace(missing_next, next_player=Knowledge.known(1, evidence))
    with pytest.raises(CanonicalCaptureError, match="^PASS_RESET_UNAVAILABLE$"):
        ConfirmedActionTransition(1, 1, None, evidence).validate(known_next)
    with pytest.raises(CanonicalCaptureError, match="^TRANSITION_EVIDENCE_UNTRUSTED$"):
        ConfirmedActionTransition(1, 1, 0, EvidenceRef("fake.source")).validate(known_next)


def test_callers_cannot_supply_capability_quality_or_eligibility():
    names = {field.name for field in fields(ConfirmedDecisionInput)}
    assert not ({"capabilities", "quality", "eligibility"} & names)


def test_declaration_and_exchange_capabilities_are_derived(tmp_path):
    _recorder, _converter, _source, capture_id = create_capture(tmp_path)
    item = decision_input(capture_id)
    states = module._derive_capability_states(item.public_state, item.action_session)
    assert states["declaration"] == "verified"
    assert states["tribute_return"] == "not_applicable"


def test_strict_record_rejects_extra_duplicate_and_nonfinite_json(tmp_path):
    recorder, converter, source, capture_id = create_capture(tmp_path)
    record_confirmed(converter, decision_input(capture_id)); finish_capture(recorder); converter.close()
    record = strict_json_loads((source.parent / "phase7_canonical_decisions.jsonl").read_text(
        encoding="utf-8").splitlines()[0])
    record["extra"] = 1
    with pytest.raises(CanonicalCaptureError, match="^DECISION_FIELDS_INVALID$"):
        validate_decision_record(record)
    with pytest.raises(CanonicalCaptureError, match="^JSON_DUPLICATE_MEMBER$"):
        strict_json_loads('{"schema":1,"schema":2}')
    with pytest.raises(CanonicalCaptureError, match="^JSON_NONFINITE_NUMBER$"):
        strict_json_loads('{"value":NaN}')

def test_capability_derivation_rejects_unsupported_declaration_and_exchange(tmp_path):
    _recorder, _converter, _source, capture_id = create_capture(tmp_path)
    item = decision_input(capture_id)
    evidence = EvidenceRef("semantic.action")
    action = CommittedActionV2(
        1, 0, "LEAD_PLAY", "play", (item.public_state.own_hand.value[0],),
        Knowledge.unsupported("declaration_missing"), Knowledge.known(3, evidence),
        Knowledge.known(26, evidence), evidence,
    )
    history = replace(item.public_state.history, actions=(action,), observed_action_count=1)
    state = replace(item.public_state, history=history)
    derived = module._derive_capability_states(state, item.action_session)
    assert derived["declaration"] == "unsupported"
    state = replace(item.public_state, exchange=unsupported_exchange())
    assert module._derive_capability_states(state, item.action_session)["tribute_return"] == "unsupported"


def test_source_sequence_gap_derives_failed_quality_and_blocks_manifest(tmp_path):
    from guance.contracts import SourceFrame
    recorder, converter, source, capture_id = create_capture(tmp_path)
    record_confirmed(converter, decision_input(capture_id))
    recorder.append(SourceFrame(
        host="hlxyxws.huanle.qq.com", payload=b"gap", received_at=3.0,
        connection_id="connection-1", sequence=3, direction="server"))
    finish_capture(recorder, 3)
    result = converter.close()
    assert result.status == "blocked"
    assert "NO_GAP_FAILED" in result.missing_fact_codes
    assert not (source.parent / "phase7_acceptance_input.json").exists()


def test_dropped_marker_derives_failed_no_loss_quality(tmp_path):
    recorder, converter, source, capture_id = create_capture(tmp_path)
    record_confirmed(converter, decision_input(capture_id)); finish_capture(recorder)
    row = json.loads(source.read_text(encoding="utf-8").splitlines()[0]); row["dropped"] = True
    source.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
    sidecar_path = source.parent / "capture_status.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    raw = source.read_bytes(); sidecar["source_sha256"] = __import__("hashlib").sha256(raw).hexdigest()
    sidecar["source_bytes"] = sidecar["bytes"] = len(raw)
    sidecar_path.write_text(json.dumps(sidecar, sort_keys=True) + "\n", encoding="utf-8")
    result = converter.close()
    assert result.status == "blocked"
    assert "NO_LOSS_FAILED" in result.missing_fact_codes

def test_speculative_or_foreign_cache_identity_never_enters_artifact(tmp_path):
    recorder, converter, _source, capture_id = create_capture(tmp_path)
    item = decision_input(capture_id)
    speculative = item.history.clone_branch("speculative").snapshot
    with pytest.raises(CanonicalCaptureError, match="^SPECULATIVE_HISTORY_REJECTED$"):
        converter.record_decision(replace(item, history=speculative))
    finish_capture(recorder)
    assert converter.close().status == "blocked"

def test_second_decision_counter_rollback_and_request_gap_are_rejected(tmp_path):
    from guance.contracts import SourceFrame
    recorder, converter, _source, capture_id = create_capture(tmp_path)
    first = decision_input(capture_id)
    record_confirmed(converter, first)
    recorder.append(SourceFrame(
        host="hlxyxws.huanle.qq.com", payload=b"second", received_at=2.0,
        connection_id="connection-1", sequence=2, direction="server"))
    with pytest.raises(CanonicalCaptureError, match="^DECISION_OPPORTUNITY_NOT_MONOTONIC$"):
        converter.observe_decision_opportunity(
            frame_sequence=2, state_version=first.public_state.identity.state_version,
            public_action_index=len(first.public_state.history.actions),
        )
    finish_capture(recorder, 2)
    assert converter.close().status == "blocked"


def test_private_prefix_and_cache_request_mismatch_fail_closed(tmp_path):
    recorder, converter, _source, capture_id = create_capture(tmp_path)
    item = decision_input(capture_id)
    token = item.action_session.candidates[0].tokens[0]
    mismatched_session = replace(item.action_session, prefix=(token,))
    with pytest.raises(CanonicalCaptureError, match="^COUNTER_OR_SESSION_MISMATCH$"):
        converter.record_decision(replace(item, action_session=mismatched_session))
    finish_capture(recorder)
    assert converter.close().status == "blocked"


def test_input_type_has_no_future_payload_hidden_or_model_fields():
    names = {field.name for field in fields(ConfirmedDecisionInput)}
    forbidden = {"payload", "payload_b64", "client_payload", "hidden_opponent_hands",
                 "outcome", "reward", "model_output", "recommendation"}
    assert not (names & forbidden)

def test_cache_request_sequence_gap_is_rejected(tmp_path):
    from guance.contracts import SourceFrame
    recorder, converter, _source, capture_id = create_capture(tmp_path)
    record_confirmed(converter, decision_input(capture_id))
    recorder.append(SourceFrame(
        host="hlxyxws.huanle.qq.com", payload=b"second", received_at=2.0,
        connection_id="connection-1", sequence=2, direction="server"))
    with pytest.raises(CanonicalCaptureError, match="^CACHE_REQUEST_SEQUENCE_GAP$"):
        record_confirmed(converter, decision_input(
            capture_id, frame_sequence=2, state_version_delta=1, request_sequence=3))
    finish_capture(recorder, 2)
    assert converter.close().status == "blocked"

def test_decision_source_sequence_must_exist_in_bound_capture(tmp_path):
    recorder, converter, _source, capture_id = create_capture(tmp_path)
    record_confirmed(converter, decision_input(capture_id, frame_sequence=2))
    finish_capture(recorder, 1)
    result = converter.close()
    assert result.status == "blocked"
    assert "SOURCE_DECISION_BINDING_MISMATCH" in result.missing_fact_codes

def test_engine_history_and_cache_invalidations_derive_blocked_quality(tmp_path):
    from guance.contracts import EngineUpdate
    recorder, converter, _source, _capture_id = create_capture(tmp_path)
    converter.observe_engine_update(EngineUpdate(kind="error", error_code="state_invalid"))
    converter.observe_history_invalidation()
    converter.observe_cache_invalidation()
    finish_capture(recorder)
    result = converter.close()
    assert result.status == "blocked"
    assert {"ENGINE_ERROR_OBSERVED", "HISTORY_INVALIDATED",
            "CACHE_GENERATION_INVALIDATED"} <= set(result.missing_fact_codes)

def test_unconsumed_decision_opportunity_derives_partial_block(tmp_path):
    recorder, converter, _source, capture_id = create_capture(tmp_path)
    item = decision_input(capture_id)
    converter.observe_decision_opportunity(
        frame_sequence=item.frame_sequence,
        state_version=item.public_state.identity.state_version,
        public_action_index=len(item.public_state.history.actions),
    )
    finish_capture(recorder)
    result = converter.close()
    assert result.status == "blocked"
    assert {"CANONICAL_DECISIONS_MISSING", "PARTIAL_DECISIONS"} <= set(result.missing_fact_codes)
