"""Version 1, Qt-free own-view training-data contract.

``snapshot_update`` is the only live producer copy boundary. It copies bounded,
explicitly typed whitelisted fields, not arbitrary nested mappings. Card codes
are validated with the existing physical-card codec (suit 2 is hearts, joker
ranks 14/15); cards are not decoded into model tokens or wildcard claims.

Canonical decisions are semantic source data, not model-ready tensors. No
legal-action snapshot, reward mapping, oracle hands or model scores are labels.
Rules/encoder-based legal-action regeneration is a future derived view. Separate
schema/card/action versions are recorded; unknown supplied schema versions and
nonfinite numbers are rejected. ``accepted_candidate`` is a collection quality
status, not a claim of training suitability or an inferred reward.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from guance.contracts import EngineUpdate
from guance.domain.cards import card_parts

SCHEMA_VERSION = 1
ACTION_SCHEMA_VERSION = 1
CARD_ENCODING = "guance-physical-v1"
PRIVACY_SCOPE = "own_hand_and_public_actions_only"
MAX_HISTORY = 4096
MESSAGES = frozenset({
    "deal", "start", "action", "settlement", "information",
    "seat-evidence", "manual-seat-confirmation",
})
SAFE_TABLE_FIELDS = frozenset({
    "session_id", "game_id", "state_version", "captured_at", "phase", "level",
    "my_seat", "first_seat", "current_seat", "hand", "remaining", "history",
    "free_lead", "trick", "seat_confirmed", "seat_confirmation_source",
    "finish_ranks_by_seat", "lead_action",
})


class SchemaError(ValueError):
    """A fixed reason code only: never interpolates potentially private input."""


def integer(value: Any, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise SchemaError("invalid_integer")
    return value


def finite_number(value: Any) -> float:
    if type(value) not in (int, float):
        raise SchemaError("invalid_number")
    try:
        number = float(value)
    except OverflowError:
        raise SchemaError("nonfinite_number") from None
    if not math.isfinite(number) or number < 0:
        raise SchemaError("nonfinite_number")
    return number


def identifier(value: Any) -> str:
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value):
        raise SchemaError("invalid_identifier")
    return value


def _list(value: Any, maximum: int) -> list | tuple:
    if type(value) not in (list, tuple) or len(value) > maximum:
        raise SchemaError("invalid_sequence")
    return value


def _cards(value: Any) -> list[int]:
    cards = _list(value, 27)
    for code in cards:
        try:
            card_parts(code)
        except ValueError:
            raise SchemaError("invalid_physical_card") from None
    if len(set(cards)) != len(cards):
        raise SchemaError("duplicate_physical_card")
    return sorted(cards)


def canonicalize_action(value: Any) -> dict[str, Any]:
    """Copy only PublicAction fields; sort physical codes for stable identity."""
    if type(value) is not dict:
        raise SchemaError("invalid_public_action")
    kind = value.get("kind")
    if kind not in ("play", "pass"):
        raise SchemaError("unknown_action_kind")
    cards = _cards(value.get("cards", ()))
    if (kind == "pass" and cards) or (kind == "play" and not cards):
        raise SchemaError("action_cards_mismatch")
    combo = value.get("combo_type")
    next_player = value.get("next_player")
    return {
        "index": integer(value.get("index"), 1, MAX_HISTORY),
        "player": integer(value.get("player"), 0, 3),
        "kind": kind,
        "cards": cards,
        "combo_type": None if combo is None else integer(combo, 0, 2**31 - 1),
        "next_player": None if next_player is None else integer(next_player, 0, 3),
        "trick": integer(value.get("trick", 0), 0, MAX_HISTORY),
    }


def canonicalize_table(table: Any) -> dict[str, Any]:
    """Return a fully detached safe table; no caller-owned containers survive.

    Redundant display counters and arbitrary nested fields are intentionally
    omitted. ``lead_action`` is optional in the actual engine table; it can be
    regenerated from public history/free_lead. Partial public finish ranks are
    retained as public state, not treated as terminal labels.
    """
    if type(table) is not dict:
        raise SchemaError("invalid_table")
    if type(table.get("schema_version", 1)) is not int or table.get("schema_version", 1) != 1:
        raise SchemaError("unsupported_state_schema")
    phase = table.get("phase")
    if phase not in ("waiting_deal", "waiting_start", "active", "ended", "settled"):
        raise SchemaError("unknown_phase")
    remaining = [integer(x, 0, 27) for x in _list(table.get("remaining"), 4)]
    if len(remaining) != 4:
        raise SchemaError("invalid_remaining")
    my_seat = integer(table.get("my_seat", -1), -1, 3)
    confirmed = table.get("seat_confirmed", my_seat in range(4))
    free_lead = table.get("free_lead", False)
    if type(confirmed) is not bool or type(free_lead) is not bool:
        raise SchemaError("invalid_boolean")
    if confirmed != (my_seat in range(4)):
        raise SchemaError("seat_confirmation_mismatch")
    # Unknown evidence names are not copied (they could be free-form text).
    evidence = table.get("seat_confirmation_source")
    if evidence not in (None, "manual", "inferred", "3019"):
        evidence = None
    history = [canonicalize_action(x) for x in _list(table.get("history"), MAX_HISTORY)]
    if any(action["index"] != i for i, action in enumerate(history, 1)):
        raise SchemaError("history_index_gap")
    result = {
        "session_id": identifier(table.get("session_id")),
        "game_id": identifier(table.get("game_id")),
        "state_version": integer(table.get("state_version"), 0, 2**63 - 1),
        "captured_at": finite_number(table.get("captured_at", 0.0)),
        "phase": phase,
        "level": integer(table.get("level"), 1, 13),
        "my_seat": my_seat,
        "first_seat": integer(table.get("first_seat", -1), -1, 3),
        "current_seat": integer(table.get("current_seat", -1), -1, 3),
        "hand": _cards(table.get("hand")),
        "remaining": remaining,
        "history": history,
        "free_lead": free_lead,
        "trick": integer(table.get("trick", 0), 0, MAX_HISTORY),
        "seat_confirmed": confirmed,
        "seat_confirmation_source": evidence,
    }
    if "finish_ranks_by_seat" in table:
        ranks = [integer(x, -1, 4) for x in _list(table["finish_ranks_by_seat"], 4)]
        if len(ranks) != 4 or 0 in ranks:
            raise SchemaError("invalid_finish_ranks")
        result["finish_ranks_by_seat"] = ranks
    if "lead_action" in table:
        result["lead_action"] = (
            None if table["lead_action"] is None else canonicalize_action(table["lead_action"])
        )
    return result


def snapshot_update(update: EngineUpdate) -> dict[str, Any] | None:
    """Bounded whitelist copy only; no JSON encoding, hashing, disk or model.

    Non-state updates (including advice/candidate/error payloads) are ignored.
    Main should call ``recorder.invalidate_current()`` on a capture/state error
    or reconnect; that explicit hook accepts only an enumerated reason code.
    """
    if update.kind != "state":
        return None
    if type(update.data) is not dict:
        raise SchemaError("invalid_update_data")
    sequence = update.data.get("source_sequence")
    message = update.message if update.message in MESSAGES else "unknown"
    return {
        "message": message,
        "source_sequence": None if sequence is None else integer(sequence, 0, 2**63 - 1),
        "table": canonicalize_table(update.data.get("table")),
    }


def canonical_json(value: Any) -> str:
    """Writer/offline-only deterministic JSON; never called by the live producer."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def stable_id(*parts: Any) -> str:
    return hashlib.sha256(canonical_json(parts).encode("utf-8")).hexdigest()


def action_signature(action: dict[str, Any]) -> str:
    normalized = canonicalize_action(action)
    # Action identity is not its event index, seat, trick, or next-player ordering.
    return stable_id(ACTION_SCHEMA_VERSION, {
        k: normalized[k] for k in ("kind", "cards", "combo_type")
    })


def build_decision_record(
    *, source: str, source_id: str, episode_id: str,
    pre_state: dict[str, Any], action: dict[str, Any], post_state: dict[str, Any],
    source_sequence: int | None, rule_profile: str | None = None,
    rule_revision: str | None = None,
) -> dict[str, Any]:
    """Synchronous worker/offline builder. No settlement data enters pre/post."""
    action = canonicalize_action(action)
    signature = action_signature(action)
    return {
        "record_type": "decision",
        "schema_version": SCHEMA_VERSION,
        "action_schema_version": ACTION_SCHEMA_VERSION,
        "card_encoding": CARD_ENCODING,
        "record_id": stable_id(episode_id, action["index"], signature),
        "provenance": {
            "source": source, "source_id": source_id, "episode_id": episode_id,
            "session_id": pre_state["session_id"], "game_id": pre_state["game_id"],
            "source_sequence": source_sequence, "captured_at": post_state["captured_at"],
            "rule_profile": rule_profile, "rule_revision": rule_revision,
        },
        "pre_state": canonicalize_table(pre_state),
        "observed_action": {**action, "signature": signature, "event_index": action["index"]},
        "post_state": canonicalize_table(post_state),
        "status": "partial",
        "quality": {"status": "partial", "reasons": ["awaiting_settlement"], "lost": False},
        "outcome": None,
        "reward": None,
        "privacy_scope": PRIVACY_SCOPE,
        "legal_actions": None,
        "legal_actions_status": "not_recorded_regenerate_from_rules",
    }
