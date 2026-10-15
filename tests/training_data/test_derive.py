"""TD-02/03/04/07/08: synthetic collector-to-current-encoder verification.

No model weights, real captures, Qt, network, or production files are used.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math

import pytest

from guance.contracts import EngineUpdate, GameObservation
from guance.domain.cards import encode_card
from guance.infrastructure.model.advisor import ModelError, build_model_observation, to_fable
from guance.infrastructure.model.fabledan_vendor.encode import encode_decision
from guance.infrastructure.training_data import TrainingDataCollector
from guance.infrastructure.training_data.derive import derive_fabledan, observation_from_record
from guance.infrastructure.training_data.schema import action_signature, stable_id
from guance.infrastructure.training_data.validation import validate_decision


ENCODER_REVISION = "fabledan-7cc5e311b9860bc44f76c082d9c1b21fc8b2d3ec"
REWARD_REVISION = "fabledan-team-rank-normalized-v1"


def synthetic_updates(*, follow=False, passing=False, settled=True):
    """Near-terminal safe tables, not a claim of a fully replayed 108-card game."""
    own_card = encode_card(1, 7)
    table = {
        "session_id": "derive-synthetic", "game_id": "game-0001",
        "state_version": 1, "captured_at": 1.0, "phase": "waiting_start",
        "level": 5, "my_seat": -1, "first_seat": -1, "current_seat": -1,
        "hand": [own_card], "remaining": [1, 2, 0, 2], "history": [],
        "free_lead": True, "trick": 0, "seat_confirmed": False,
        "seat_confirmation_source": None, "finish_ranks_by_seat": [-1, -1, 1, -1],
    }
    updates = []

    def append(message):
        updates.append(EngineUpdate("state", message, data={
            "table": deepcopy(table), "source_sequence": len(updates) + 1,
            "phase": table["phase"], "game_id": table["game_id"],
            "state_version": table["state_version"], "my_seat": table["my_seat"],
        }))

    def advance(**changes):
        table.update(state_version=table["state_version"] + 1,
                     captured_at=table["captured_at"] + 1, **changes)

    append("deal")
    advance(phase="active", first_seat=1 if follow else 0, current_seat=1 if follow else 0)
    append("start")
    advance(my_seat=0, seat_confirmed=True, seat_confirmation_source="manual")
    append("manual-seat-confirmation")
    if follow:
        lead = {"index": 1, "player": 1, "kind": "play", "cards": [encode_card(1, 3)],
                "combo_type": 1, "next_player": 0, "trick": 0}
        advance(history=[lead], current_seat=0, remaining=[1, 1, 0, 2], free_lead=False)
        append("action")
    action = {
        "index": len(table["history"]) + 1, "player": 0,
        "kind": "pass" if passing else "play", "cards": [] if passing else [own_card],
        "combo_type": None if passing else 1, "next_player": 3 if passing else None,
        "trick": 0,
    }
    remaining = list(table["remaining"])
    if not passing:
        remaining[0] = 0
    advance(history=table["history"] + [action], hand=[own_card] if passing else [],
            remaining=remaining, phase="active" if passing else "ended", free_lead=False,
            current_seat=3 if passing else -1,
            finish_ranks_by_seat=[-1, -1, 1, -1] if passing else [2, -1, 1, -1])
    append("action")
    if settled:
        if passing:
            raise ValueError("synthetic pass fixture cannot settle")
        advance(phase="settled", finish_ranks_by_seat=[2, 3, 1, 4])
        append("settlement")
    return updates


def collected_record(*, follow=False, passing=False, settled=True, quarantine=False):
    collector = TrainingDataCollector(source="synthetic", source_id="independent-derive",
                                      rule_profile="tencent_daguandan",
                                      rule_revision="guance-observed-v1")
    latest = {}
    for update in synthetic_updates(follow=follow, passing=passing, settled=settled):
        for episode in collector.observe_update(update):
            latest[episode["episode_id"]] = episode
    if quarantine:
        for episode in collector.invalidate_current("external_loss"):
            latest[episode["episode_id"]] = episode
    for episode in collector.close():
        latest[episode["episode_id"]] = episode
    records = [record for episode in latest.values() for record in episode["records"]]
    assert len(records) == 1
    validate_decision(records[0])
    return records[0]


@pytest.mark.parametrize("follow,passing", [(False, False), (True, False), (True, True)],
                         ids=["free-lead-single", "follow-single", "follow-pass"])
def test_derived_inputs_match_current_encode_decision(follow, passing):
    record = collected_record(follow=follow, passing=passing, settled=not passing)
    original = deepcopy(record)
    # Build the comparison from the original pre-state, not the derive adapter.
    pre = deepcopy(record["pre_state"])
    pre["lead_action"] = None if pre["free_lead"] else pre["history"][-1]
    observation = GameObservation.from_dict(pre)
    current = build_model_observation(observation)
    tokens, features = encode_decision(current)
    desired_cards = sorted(to_fable(card) for card in record["observed_action"]["cards"])
    indexes = [index for index, move in enumerate(current["legal"])
               if sorted(move.cards) == desired_cards]
    assert len(indexes) == 1
    index = indexes[0]

    derived = derive_fabledan(record)

    assert derived["encoder"] == {
        "revision": ENCODER_REVISION, "vocab_size": 48, "feature_dim": 80, "max_tokens": 512,
    }
    assert derived["adapter_version"] == 1
    assert derived["canonical_record_id"] == record["record_id"]
    expected_hash = hashlib.sha256(json.dumps((record,), ensure_ascii=False, sort_keys=True,
                                             separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    assert derived["canonical_sha256"] == expected_hash
    assert derived["episode_group"] == record["provenance"]["episode_id"]
    assert derived["action"]["signature"] == record["observed_action"]["signature"]
    assert derived["action"]["model_combo_type"] == current["legal"][index].type
    assert derived["action"]["claim_ranks_zero_based"] == list(current["legal"][index].claim_ranks)
    assert derived["action"]["generated_legal_count"] == len(current["legal"])
    assert derived["action"]["exact_generated_index"] == index
    assert derived["inputs"]["tokens"] == tokens
    assert derived["inputs"]["chosen_action_features"] == features[index].tolist()
    assert all(0 <= token < 48 for token in derived["inputs"]["tokens"])
    assert len(derived["inputs"]["chosen_action_features"]) == 80
    assert all(math.isfinite(value) for value in derived["inputs"]["chosen_action_features"])
    assert record == original
    assert derive_fabledan(record) == derived


@pytest.mark.parametrize("settled", [False, True], ids=["partial", "verified-settlement"])
def test_reward_is_missing_without_settlement_and_oracle_is_never_fabricated(settled):
    record = collected_record(settled=settled)
    derived = derive_fabledan(record)
    assert record["reward"] is None
    assert derived["targets"] == {
        "terminal_return": 1.0 if settled else None,
        "reward_revision": REWARD_REVISION,
        "masks": {"terminal_return": settled, "belief_oracle": False},
    }
    assert set(derived["inputs"]) == {"tokens", "chosen_action_features"}
    assert set(derived["targets"]) == {"terminal_return", "reward_revision", "masks"}
    assert "oracle_hands" not in json.dumps(derived)
    assert "belief_targets" not in json.dumps(derived)
    if not settled:
        assert record["status"] == "partial"
        assert record["outcome"] is None


@pytest.mark.parametrize("field,value", [
    ("schema_version", 99), ("action_schema_version", 99),
    ("card_encoding", "unsupported"), ("record_id", "0" * 64),
    ("reward", 0), ("legal_actions", []),
])
def test_invalid_records_are_rejected_by_validator_and_adapter(field, value):
    record = collected_record()
    record[field] = value
    for operation in (validate_decision, derive_fabledan):
        with pytest.raises(ValueError):
            operation(record)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")],
                         ids=["nan", "positive-infinity", "negative-infinity"])
def test_nonfinite_input_is_rejected_without_synthesizing_features(value):
    record = collected_record()
    record["pre_state"]["captured_at"] = value
    for operation in (validate_decision, derive_fabledan):
        with pytest.raises(ValueError):
            operation(record)


def test_quarantine_is_schema_valid_but_not_eligible_for_derivation():
    record = collected_record(quarantine=True)
    assert record["status"] == "quarantine"
    assert record["outcome"] is None
    validate_decision(record)
    with pytest.raises(ValueError, match="^quarantined_record$"):
        derive_fabledan(record)


def test_reconstructed_public_lead_and_own_view_do_not_use_post_action_information():
    record = collected_record(follow=True)
    observation = observation_from_record(record)
    assert observation.current_seat == observation.my_seat == 0
    assert observation.lead_action is not None
    assert observation.lead_action.index == 1
    assert len(observation.history) == 1
    assert len(record["post_state"]["history"]) == 2
    assert tuple(observation.hand) == tuple(record["pre_state"]["hand"])
    assert record["post_state"]["hand"] == []


def test_ambiguous_wildcard_declaration_rejects_instead_of_zero_features():
    record = collected_record()
    wild = encode_card(2, record["pre_state"]["level"])
    record["pre_state"]["hand"] = [wild]
    record["observed_action"]["cards"] = [wild]
    record["observed_action"]["combo_type"] = None
    public = {key: record["observed_action"][key]
              for key in ("index", "player", "kind", "cards", "combo_type", "next_player", "trick")}
    record["post_state"]["history"] = [public]
    signature = action_signature(public)
    record["observed_action"]["signature"] = signature
    record["record_id"] = stable_id(record["provenance"]["episode_id"], public["index"], signature)
    validate_decision(record)
    # A wild single may have a unique convention; ambiguous multi-card shapes
    # are owned by the advisor. This fixture is only a rejection test if its
    # current generator exposes more than one semantic interpretation.
    current = build_model_observation(observation_from_record(record))
    semantic = {(move.type, move.key, tuple(sorted(move.claim_ranks)))
                for move in current["legal"] if sorted(move.cards) == [to_fable(wild)]}
    assert semantic
    if len(semantic) > 1:
        with pytest.raises(ModelError):
            derive_fabledan(record)
    else:
        derived = derive_fabledan(record)
        assert len(derived["inputs"]["chosen_action_features"]) == 80
