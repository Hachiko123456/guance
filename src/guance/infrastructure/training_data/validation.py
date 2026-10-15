"""Pure structural/temporal validation for canonical decision records (v1)."""
from __future__ import annotations

from typing import Any

from .schema import (
    ACTION_SCHEMA_VERSION, CARD_ENCODING, PRIVACY_SCOPE, SCHEMA_VERSION,
    SchemaError, action_signature, canonical_json, canonicalize_action,
    canonicalize_table, stable_id, identifier, finite_number, integer,
)

RECORD_FIELDS = frozenset({
    "record_type", "schema_version", "action_schema_version", "card_encoding",
    "record_id", "provenance", "pre_state", "observed_action", "post_state",
    "status", "quality", "outcome", "reward", "privacy_scope", "legal_actions",
    "legal_actions_status",
})
PROVENANCE_FIELDS = frozenset({
    "source", "source_id", "episode_id", "session_id", "game_id", "source_sequence",
    "captured_at", "rule_profile", "rule_revision",
})


def validate_decision(record: dict[str, Any]) -> None:
    """Raise fixed-code SchemaError. Never echo private input in exceptions.

    This checks representation, identity and temporal/action consistency. It is
    not an alternative full Guandan rules engine, reward adapter or model eval.
    """
    if type(record) is not dict or set(record) != RECORD_FIELDS:
        raise SchemaError("record_fields_mismatch")
    if (record["record_type"] != "decision" or type(record["schema_version"]) is not int
            or record["schema_version"] != SCHEMA_VERSION
            or record["action_schema_version"] != ACTION_SCHEMA_VERSION
            or record["card_encoding"] != CARD_ENCODING):
        raise SchemaError("unsupported_record_version")
    if (record["privacy_scope"] != PRIVACY_SCOPE or record["legal_actions"] is not None
            or record["legal_actions_status"] != "not_recorded_regenerate_from_rules"):
        raise SchemaError("unsupported_record_scope")
    if record["reward"] is not None:
        raise SchemaError("unversioned_reward")
    canonical_json(record)  # Reject NaN/Infinity anywhere, without rendering data.
    pre, post = canonicalize_table(record["pre_state"]), canonicalize_table(record["post_state"])
    if pre != record["pre_state"] or post != record["post_state"]:
        raise SchemaError("noncanonical_or_unknown_state_fields")
    raw = record["observed_action"]
    action = canonicalize_action(raw)
    if set(raw) != set(action) | {"signature", "event_index"}:
        raise SchemaError("action_fields_mismatch")
    if raw["signature"] != action_signature(action) or raw["event_index"] != action["index"]:
        raise SchemaError("action_identity_mismatch")
    if pre["phase"] != "active" or not pre["seat_confirmed"] or pre["current_seat"] != pre["my_seat"]:
        raise SchemaError("not_confirmed_own_decision")
    if action["player"] != pre["my_seat"] or len(pre["hand"]) != pre["remaining"][pre["my_seat"]]:
        raise SchemaError("observer_hand_mismatch")
    if (post["state_version"] != pre["state_version"] + 1
            or post["history"] != pre["history"] + [action]
            or post["session_id"] != pre["session_id"] or post["game_id"] != pre["game_id"]):
        raise SchemaError("temporal_link_mismatch")
    if not set(action["cards"]) <= set(pre["hand"]):
        raise SchemaError("action_not_in_hand")
    if post["hand"] != sorted(set(pre["hand"]) - set(action["cards"])):
        raise SchemaError("post_hand_mismatch")
    remaining = list(pre["remaining"])
    remaining[action["player"]] -= len(action["cards"])
    if remaining != post["remaining"]:
        raise SchemaError("remaining_mismatch")
    provenance = record["provenance"]
    if type(provenance) is not dict or set(provenance) != PROVENANCE_FIELDS:
        raise SchemaError("provenance_fields_mismatch")
    if provenance["source"] not in ("real", "legacy", "synthetic"):
        raise SchemaError("unknown_provenance_source")
    for key in ("source_id", "episode_id", "session_id", "game_id"):
        identifier(provenance[key])
    for key in ("rule_profile", "rule_revision"):
        if provenance[key] is not None:
            identifier(provenance[key])
    if provenance["source_sequence"] is not None:
        integer(provenance["source_sequence"], 0, 2**63 - 1)
    if finite_number(provenance["captured_at"]) != post["captured_at"]:
        raise SchemaError("capture_time_mismatch")
    if provenance["episode_id"] != stable_id(provenance["source"], provenance["source_id"], pre["session_id"], pre["game_id"]):
        raise SchemaError("episode_identity_mismatch")
    if (provenance["session_id"] != pre["session_id"] or provenance["game_id"] != pre["game_id"]
            or record["record_id"] != stable_id(provenance["episode_id"], action["index"], raw["signature"])):
        raise SchemaError("record_identity_mismatch")
    status = record["status"]
    quality = record["quality"]
    if (status not in ("accepted_candidate", "partial", "quarantine") or type(quality) is not dict
            or set(quality) != {"status", "reasons", "lost"} or quality["status"] != status
            or type(quality["lost"]) is not bool or type(quality["reasons"]) is not list):
        raise SchemaError("quality_mismatch")
    outcome = record["outcome"]
    if status == "accepted_candidate":
        if (quality["lost"] or quality["reasons"] or type(outcome) is not dict
                or set(outcome) != {"phase", "finish_ranks_by_seat"}
                or outcome["phase"] != "settled"
                or type(outcome["finish_ranks_by_seat"]) is not list
                or any(type(x) is not int for x in outcome["finish_ranks_by_seat"])
                or sorted(outcome["finish_ranks_by_seat"]) != [1, 2, 3, 4]):
            raise SchemaError("unverified_outcome")
    elif outcome is not None:
        raise SchemaError("outcome_on_unqualified_record")
