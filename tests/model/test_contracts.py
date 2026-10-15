"""MBA-P0-002/P0-003, MBA-P1-002/P1-003: explicit privacy and compatibility ACL."""
from dataclasses import replace
import json

import pytest

from guance.contracts import (AdviceCandidate, AdviceResult, CanonicalAdviceResult,
    CanonicalPublicGameView, GameObservation, ModelContractError, PublicAction,
    CANONICAL_ADVICE_RESULT_SCHEMA_VERSION,
    decode_canonical_advice, decode_legacy_advice_result, validate_advice_for_view)


FORBIDDEN = ("post_state", "settlement", "reward", "future_actions", "opponent_hands",
             "cookies", "authorization", "payload", "ca_private_key")


def test_observation_whitelist_ignores_override_and_preserves_legacy(observation):
    class ExtendedObservation(GameObservation):
        def to_dict(self):
            return {name: "SENSITIVE_SENTINEL" for name in FORBIDDEN}
    extended = ExtendedObservation(**{field: getattr(observation, field)
                                     for field in GameObservation.__dataclass_fields__})
    canonical = CanonicalPublicGameView.from_observation(extended)
    wire = canonical.to_dict()
    assert wire["visibility"] == "public_plus_self"
    assert wire["completeness"] == "public_play"
    assert wire["phase"] == "play"
    assert not set(FORBIDDEN) & set(wire)
    assert "SENSITIVE_SENTINEL" not in json.dumps(wire)
    assert "captured_at" not in wire
    assert CanonicalPublicGameView.from_dict(wire) == canonical
    assert replace(canonical.to_observation(), captured_at=123.0) == observation
    assert GameObservation.from_dict(observation.to_dict()) == observation


@pytest.mark.parametrize("field", FORBIDDEN)
def test_worker_contract_rejects_extra_fields(view, field):
    data = view.to_dict()
    data[field] = "SENSITIVE_SENTINEL"
    with pytest.raises(ModelContractError) as exc:
        CanonicalPublicGameView.from_dict(data)
    assert "SENSITIVE_SENTINEL" not in str(exc.value)


@pytest.mark.parametrize("changes", [
    {"hand": (0x11, 0x11)}, {"hand": (True, 0x13)}, {"hand": (0x500, 0x13)},
    {"remaining": (3, 27, 27, 27)}, {"remaining": (2, 28, 27, 27)},
    {"my_seat": 4}, {"current_seat": 1}, {"level": 0}, {"state_version": True},
    {"free_lead": False}, {"partner_seat": 1}, {"phase": "declaration"},
    {"visibility": "omniscient"}, {"completeness": "unknown"},
    {"ambiguities": ("wildcard",)}, {"schema_version": True}, {"ruleset_id": "other"},
])
def test_invalid_or_unsupported_public_states_fail_closed(view, changes):
    with pytest.raises(ModelContractError):
        replace(view, **changes).to_dict()


def test_public_action_is_whitelisted_and_order_checked(view):
    class ExtendedAction(PublicAction):
        def to_dict(self):
            return {"opponent_hands": "SENSITIVE_SENTINEL"}
    action = ExtendedAction(1, 1, "play", (0x22,), 1, 0, 0)
    current = replace(view, public_history=(action,), lead_action=action, free_lead=False)
    assert current.to_dict()["public_history"][0]["cards"] == [0x22]
    assert "SENSITIVE_SENTINEL" not in json.dumps(current.to_dict())
    with pytest.raises(ModelContractError):
        replace(current, public_history=(action, action)).to_dict()
    with pytest.raises(ModelContractError):
        replace(current, lead_action=replace(action, cards=(0x23,))).to_dict()
    data = current.to_dict()
    data["public_history"][0]["payload"] = "SENSITIVE_SENTINEL"
    with pytest.raises(ModelContractError):
        CanonicalPublicGameView.from_dict(data)


@pytest.mark.parametrize("score_kind,score", [("fabledan_q", -200.0), ("guandan_token_score", 0.9)])
def test_advice_additions_preserve_raw_scale_claim_and_safe_metadata(view, score_kind, score):
    candidate = AdviceCandidate((0x11,), "play", "single", score, (1,),
                                metadata={"strategy": "top_n", "opponent_hands": "SENSITIVE_SENTINEL"})
    legacy = AdviceResult(*view.key, "req", candidates=(candidate,))
    assert legacy.to_dict()["candidates"][0]["claim"] == [1]
    canonical = CanonicalAdviceResult.from_legacy(legacy, provider="test", model_version="v1",
                                                 score_kind=score_kind)
    wire = canonical.to_dict()
    assert wire["candidates"][0]["score"] == score
    assert wire["candidates"][0]["score_kind"] == score_kind
    assert wire["candidates"][0]["metadata"] == {"strategy": "top_n"}
    assert CanonicalAdviceResult.from_dict(wire) == canonical
    assert "SENSITIVE_SENTINEL" not in json.dumps(wire)


def test_nonfinite_score_and_unknown_output_rejected(view):
    result = CanonicalAdviceResult(*view.key, "req", provider="test", model_version="v1",
        score_kind="test_score", candidates=(AdviceCandidate((0x11,), "play", "single", 0.1,
                                                            score_kind="test_score"),))
    data = result.to_dict()
    data["candidates"][0]["score"] = float("nan")
    with pytest.raises(ModelContractError):
        CanonicalAdviceResult.from_dict(data)
    data = result.to_dict()
    data["opponent_hands"] = []
    with pytest.raises(ModelContractError):
        CanonicalAdviceResult.from_dict(data)


def test_canonical_output_does_not_invoke_candidate_serializer(view):
    class ExtendedCandidate(AdviceCandidate):
        def to_dict(self):
            return {"metadata": {}, "opponent_hands": "SECRET_SENTINEL"}
    candidate = ExtendedCandidate((0x11,), "play", "single", 0.3, score_kind="test_score")
    result = CanonicalAdviceResult(*view.key, "request", provider="test", model_version="v1",
                                   score_kind="test_score", candidates=(candidate,))
    assert "SECRET_SENTINEL" not in json.dumps(result.to_dict())
    assert result.to_dict()["candidates"][0]["cards"] == [0x11]


def test_optional_claim_is_serialized_as_empty():
    candidate = AdviceCandidate((0x11,), "play", "single", claim=None)
    assert candidate.to_dict()["claim"] == []


@pytest.mark.parametrize("bad", [None, "", {}])
def test_history_wire_type_must_be_array(view, bad):
    data = view.to_dict()
    data["public_history"] = bad
    with pytest.raises(ModelContractError):
        CanonicalPublicGameView.from_dict(data)


@pytest.mark.parametrize("field,bad", [("cards", ""), ("claim", ""), ("cards", {})])
def test_advice_wire_card_fields_must_be_arrays(view, field, bad):
    result = CanonicalAdviceResult(*view.key, "request", provider="test", model_version="v1",
        score_kind="test_score", candidates=(AdviceCandidate((0x11,), "play", "single", 0.5,
                                                            score_kind="test_score"),))
    data = result.to_dict()
    data["candidates"][0][field] = bad
    with pytest.raises(ModelContractError):
        CanonicalAdviceResult.from_dict(data)

@pytest.mark.parametrize("cards,kind", [((0x11,), "pass"), ((), "play")])
def test_advice_candidate_rejects_pass_play_shape_mismatch(view, cards, kind):
    result = CanonicalAdviceResult(*view.key, "request", provider="test", model_version="v1",
        score_kind="test_score", candidates=(AdviceCandidate(cards, kind, "action", 0.5,
                                                            score_kind="test_score"),))
    with pytest.raises(ModelContractError):
        result.to_dict()


def test_legacy_metadata_serializer_is_bounded():
    result = AdviceResult("session", "game", 1, "request",
        candidates=(AdviceCandidate((0x11,), "play", "single", metadata={
            "opponent_hands": "SECRET", "strategy": "top_n"}),),
        metadata={"cookies": "SECRET", "provider": "test"})
    wire = result.to_dict()
    assert wire["metadata"] == {"provider": "test"}
    assert wire["candidates"][0]["metadata"] == {"strategy": "top_n"}
    assert "SECRET" not in json.dumps(wire)


def test_legacy_advice_wire_shape_is_accepted_with_safe_defaults(view):
    result = CanonicalAdviceResult(*view.key, "request", provider="fabledan", model_version="legacy",
        score_kind="fabledan_q", candidates=(AdviceCandidate((0x11,), "1", "single", 0.5,
                                                            score_kind="fabledan_q"),))
    wire = result.to_dict()
    for item in (wire, wire["candidates"][0]):
        item.pop("score_kind", None)
        item.pop("metadata", None)
    wire.pop("provider", None)
    wire.pop("model_version", None)
    with pytest.raises(ModelContractError):
        CanonicalAdviceResult.from_dict(wire)
    parsed = decode_legacy_advice_result(wire)
    assert parsed.provider == "fabledan"
    assert parsed.score_kind == "unknown"


def test_legacy_advice_requires_explicit_decoder_boundary(view):
    result = CanonicalAdviceResult(*view.key, "request", provider="fabledan", model_version="legacy",
        score_kind="fabledan_q", candidates=(AdviceCandidate((0x11,), "1", "single", 0.5,
                                                             score_kind="fabledan_q"),))
    wire = result.to_dict()
    wire.pop("provider")
    wire.pop("model_version")
    with pytest.raises(ModelContractError):
        decode_canonical_advice(wire)
    parsed = decode_legacy_advice_result(wire)
    assert parsed.provider == "fabledan"
    assert parsed.model_version == "legacy"



def test_provider_output_validator_rejects_illegal_shape_and_free_lead_pass(view):
    bad_pass = CanonicalAdviceResult(*view.key, "request", provider="test", model_version="v1",
        score_kind="test_score", candidates=(AdviceCandidate((), "pass", "pass", 0.1,
                                                               score_kind="test_score"),))
    with pytest.raises(ModelContractError):
        validate_advice_for_view(view, bad_pass)

    bad_pair = CanonicalAdviceResult(*view.key, "request", provider="test", model_version="v1",
        score_kind="test_score", candidates=(AdviceCandidate((0x11,), "pair", "pair", 0.1,
                                                               score_kind="test_score"),))
    with pytest.raises(ModelContractError):
        validate_advice_for_view(view, bad_pair)

    valid = CanonicalAdviceResult(*view.key, "request", provider="test", model_version="v1",
        score_kind="test_score", candidates=(AdviceCandidate((0x11,), "single", "single", 0.1,
                                                               score_kind="test_score"),))
    validate_advice_for_view(view, valid)


def test_canonical_advice_uses_its_own_schema_version(view):
    result = CanonicalAdviceResult(*view.key, "request", provider="test", model_version="v1",
        score_kind="test_score", candidates=(AdviceCandidate((0x11,), "play", "single",
                                                               0.1, score_kind="test_score"),))
    assert result.to_dict()["schema_version"] == CANONICAL_ADVICE_RESULT_SCHEMA_VERSION
