"""Fail-closed producer for private Phase 7 canonical capture artifacts.

The journal lives beside an ignored local capture. Public evidence contains only
hashes/counts/statuses; this module never opens sockets, replays traffic, imports
Torch, or emits model recommendations.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
import math
import os
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping

from guance.public_state_contract import (
    CanonicalGuandanPublicState, CommittedActionV2, EvidenceRef, KnowledgeStatus,
    wire_card_to_guandan_id,
)
from guance.infrastructure.model.marvel_v2.action_protocol import ActionProtocolSession
from guance.infrastructure.model.marvel_v2.contracts import ActionRow, LegalActionRows, MarvelState
from guance.infrastructure.model.marvel_v2.encoder import encode_channels
from guance.infrastructure.model.marvel_v2.history import ExactHistoryJournal
from guance.infrastructure.model.marvel_v2.lifecycle_contracts import CacheRequestIdentity

PRODUCER = "guance.CanonicalCaptureConverter"
PRODUCER_VERSION = "2"
DECISION_SCHEMA = "CanonicalLocalCaptureDecision.v1"
MANIFEST_SCHEMA = "CanonicalLocalCaptureAcceptanceInput.v2"
STATUS_SCHEMA = "CanonicalLocalCaptureAcceptanceStatus.v1"
SIDECAR_SCHEMA = "guance-capture-closed-sidecar-v1"
CAPTURE_SCHEMA = "guance-debug-frame-jsonl-v1"
PROTOCOL_IDENTITY = "TencentDaguandanObservedProtocol.v20261014"
TARGET_HOST = "hlxyxws.huanle.qq.com"
JOURNAL_FILENAME = "phase7_canonical_decisions.jsonl"
MANIFEST_FILENAME = "phase7_acceptance_input.json"
STATUS_FILENAME = "phase7_acceptance_status.json"
CAPABILITY_NAMES = (
    "public_self_canonical", "exact_four_observer_history", "private_prefix",
    "declaration", "tribute_return", "token_step", "committed_step",
    "legal_action_counters",
)
QUALITY_NAMES = ("no_loss", "no_gap", "no_partial", "no_quarantine")
GAME_SCOPED_EXCLUSIONS = frozenset({
    "LIVE_DECLARATION_AMBIGUOUS", "LIVE_EXCHANGE_VARIANT_UNSUPPORTED",
    "PREVIOUS_RESULT_ASSOCIATION_UNSUPPORTED", "LIVE_PASS_RESET_VARIANT_UNSUPPORTED",
    "LIVE_RULES_PROVIDER_UNAVAILABLE", "LIVE_PROTOCOL_VARIANT_UNSUPPORTED",
    "LIVE_STATE_UNAVAILABLE",
})
FACT_STATES = frozenset({"verified", "not_applicable", "missing", "unsupported"})
QUALITY_STATES = frozenset({"verified", "failed", "unknown", "not_applicable"})
COUNTER_CONTRACT = {
    "frame": "source_sequence", "state": "canonical_state_version",
    "action": "public_action_index", "token": "marvel_token_step",
    "commit": "marvel_committed_step",
}


class CanonicalCaptureError(ValueError):
    """Bounded producer/contract error; never contains source values."""

    def __init__(self, code: str) -> None:
        self.code = code if _safe_code(code) else "CANONICAL_CAPTURE_REJECTED"
        super().__init__(self.code)


def _safe_code(value: object) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= 96
            and all(char.isupper() or char.isdigit() or char == "_" for char in value))


def _fail(code: str) -> None:
    raise CanonicalCaptureError(code)


def _strict_int(value: object, low: int, high: int, code: str) -> int:
    if type(value) is not int or not low <= value <= high:
        _fail(code)
    return value


def _identifier(value: object, code: str, maximum: int = 128) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= maximum
            or any(not (char.isalnum() or char in "._:-") for char in value)):
        _fail(code)
    return value


def _sha(value: object, code: str) -> str:
    if (not isinstance(value, str) or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)):
        _fail(code)
    return value


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("JSON_DUPLICATE_MEMBER")
        result[key] = value
    return result


def strict_json_loads(text: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_strict_object,
                          parse_constant=lambda _value: _fail("JSON_NONFINITE_NUMBER"))
    except CanonicalCaptureError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError):
        _fail("JSON_INVALID")


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        _fail("CANONICAL_JSON_INVALID")


def _json_hash(value: object) -> str:
    return sha256(_canonical_bytes(value)).hexdigest()


def _file_hash(path: Path) -> tuple[str, int]:
    digest = sha256(); size = 0
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk); digest.update(chunk)
    except OSError:
        _fail("CAPTURE_FILE_UNAVAILABLE")
    return digest.hexdigest(), size


def _atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2,
                                    sort_keys=True, allow_nan=False) + "\n")
            stream.flush(); os.fsync(stream.fileno())
        if path.exists():
            _fail("OUTPUT_ALREADY_EXISTS")
        temporary.replace(path)
    except CanonicalCaptureError:
        temporary.unlink(missing_ok=True)
        raise
    except (OSError, TypeError, ValueError, OverflowError):
        temporary.unlink(missing_ok=True)
        _fail("ATOMIC_WRITE_FAILED")


def _safe_capture_directory(path: Path, capture_id: str) -> Path:
    capture_id = _identifier(capture_id, "CAPTURE_ID_INVALID", 64)
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
    if capture_id.split(".", 1)[0].upper() in reserved:
        _fail("CAPTURE_ID_INVALID")
    raw = Path(path)
    try:
        absolute = raw.absolute()
        resolved = raw.resolve(strict=True)
    except OSError:
        _fail("CAPTURE_DIRECTORY_UNSAFE")
    if absolute != resolved or resolved.name != capture_id or resolved.is_symlink() or not resolved.is_dir():
        _fail("CAPTURE_DIRECTORY_UNSAFE")
    path = resolved
    if hasattr(path, "is_junction") and path.is_junction():
        _fail("CAPTURE_DIRECTORY_UNSAFE")
    for name in ("frames.jsonl", "capture_status.json", JOURNAL_FILENAME,
                 MANIFEST_FILENAME, STATUS_FILENAME, JOURNAL_FILENAME + ".tmp",
                 MANIFEST_FILENAME + ".tmp", STATUS_FILENAME + ".tmp"):
        child = path / name
        if child.is_symlink() or (hasattr(child, "is_junction") and child.is_junction()):
            _fail("CAPTURE_FILE_UNSAFE")
    return path


def _source_metadata(path: Path) -> dict[str, object]:
    rows = 0; directions: Counter[str] = Counter(); hosts: Counter[str] = Counter()
    epochs: list[dict[str, object]] = []; current_connection = ""; no_loss = True
    try:
        with path.open("rb") as stream:
            for raw in stream:
                rows += 1
                record = strict_json_loads(raw.decode("utf-8-sig"))
                if not isinstance(record, dict):
                    _fail("FRAME_METADATA_INVALID")
                required = {"schema_version", "host", "received_at", "connection_id", "sequence",
                            "direction", "payload_bytes", "payload_sha256", "payload_b64"}
                allowed = required | {"dropped", "injected"}
                if not required <= set(record) or not set(record) <= allowed:
                    _fail("FRAME_METADATA_INVALID")
                direction = record["direction"]; connection = record["connection_id"]
                sequence = record["sequence"]; host = record["host"]
                received_at = record["received_at"]
                if (record["schema_version"] != 1 or direction not in {"client", "server"}
                        or not isinstance(connection, str) or not connection
                        or type(sequence) is not int or not 0 <= sequence < 2**63
                        or type(received_at) not in {int, float} or not math.isfinite(float(received_at))
                        or not isinstance(host, str) or not host
                        or type(record["payload_bytes"]) is not int or record["payload_bytes"] < 0
                        or not isinstance(record["payload_sha256"], str)
                        or len(record["payload_sha256"]) != 64
                        or any(char not in "0123456789abcdef" for char in record["payload_sha256"])):
                    _fail("FRAME_METADATA_INVALID")
                if record.get("dropped") or record.get("injected"):
                    no_loss = False
                directions[direction] += 1; hosts[host] += 1
                if not epochs or connection != current_connection:
                    current_connection = connection
                    epochs.append({"ordinal": len(epochs) + 1, "start_sequence": sequence,
                                   "end_sequence": sequence, "frames": 0,
                                   "directions": Counter(), "contiguous": True})
                epoch = epochs[-1]
                if epoch["frames"] and sequence != int(epoch["end_sequence"]) + 1:
                    epoch["contiguous"] = False
                epoch["end_sequence"] = sequence; epoch["frames"] = int(epoch["frames"]) + 1
                counts = Counter(epoch["directions"]); counts[direction] += 1
                epoch["directions"] = counts
    except UnicodeError:
        _fail("FRAME_METADATA_INVALID")
    if rows <= 0 or not epochs:
        _fail("CAPTURE_EMPTY")
    public_epochs = [{"ordinal": item["ordinal"], "start_sequence": item["start_sequence"],
                      "end_sequence": item["end_sequence"], "frames": item["frames"],
                      "directions": dict(sorted(Counter(item["directions"]).items()))}
                     for item in epochs]
    return {"rows": rows, "directions": dict(sorted(directions.items())),
            "hosts": dict(sorted(hosts.items())), "connection_epochs": public_epochs,
            "final_sequence": public_epochs[-1]["end_sequence"],
            "no_gap": all(bool(item["contiguous"]) for item in epochs),
            "no_loss": no_loss}


def _strict_counts(value: object, code: str) -> dict[str, int]:
    if (not isinstance(value, dict) or not value
            or any(not isinstance(key, str) or not key or type(count) is not int or count < 0
                   for key, count in value.items())):
        _fail(code)
    return value


def _strict_epochs(value: object, code: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or not value:
        _fail(code)
    for expected, item in enumerate(value, 1):
        if (not isinstance(item, dict)
                or set(item) != {"ordinal", "start_sequence", "end_sequence", "frames", "directions"}
                or type(item["ordinal"]) is not int or item["ordinal"] != expected
                or type(item["start_sequence"]) is not int or item["start_sequence"] < 0
                or type(item["end_sequence"]) is not int or item["end_sequence"] < item["start_sequence"]
                or type(item["frames"]) is not int or item["frames"] <= 0):
            _fail(code)
        _strict_counts(item["directions"], code)
    return value


@dataclass(frozen=True, slots=True)
class CaptureBinding:
    capture_id: str
    source_sha256: str
    source_bytes: int
    source_rows: int
    sidecar_sha256: str
    direction_counts: Mapping[str, int]
    connection_epochs: tuple[Mapping[str, object], ...]
    games: tuple[Mapping[str, object], ...]
    no_gap: bool
    no_loss: bool


def inspect_closed_capture(capture_dir: Path, capture_id: str) -> CaptureBinding:
    capture_dir = _safe_capture_directory(Path(capture_dir).resolve(), capture_id)
    source = capture_dir / "frames.jsonl"; sidecar_path = capture_dir / "capture_status.json"
    if not source.is_file() or not sidecar_path.is_file():
        _fail("CAPTURE_INPUT_MISSING")
    source_hash, source_bytes = _file_hash(source); metadata = _source_metadata(source)
    try:
        sidecar = strict_json_loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        _fail("SIDECAR_INVALID")
    expected = {"schema", "schema_version", "capture_schema", "producer", "producer_version",
                "protocol_identity", "capture_id", "source_filename", "source_sha256",
                "source_bytes", "state", "started_at", "closed_at", "close_reason", "frames",
                "bytes", "directions", "hosts", "connection_epochs", "final_sequence",
                "current_game_id", "games"}
    if isinstance(sidecar, dict):
        _strict_counts(sidecar.get("directions"), "SIDECAR_IDENTITY_MISMATCH")
        _strict_counts(sidecar.get("hosts"), "SIDECAR_IDENTITY_MISMATCH")
        _strict_epochs(sidecar.get("connection_epochs"), "SIDECAR_IDENTITY_MISMATCH")
    if (not isinstance(sidecar, dict) or set(sidecar) != expected
            or sidecar["schema"] != SIDECAR_SCHEMA or sidecar["schema_version"] != 1
            or sidecar["capture_schema"] != CAPTURE_SCHEMA
            or sidecar["producer"] != "guance.DebugFrameRecorder"
            or sidecar["producer_version"] != "1"
            or sidecar["protocol_identity"] != PROTOCOL_IDENTITY
            or sidecar["capture_id"] != capture_id or sidecar["source_filename"] != "frames.jsonl"
            or sidecar["source_sha256"] != source_hash
            or type(sidecar["source_bytes"]) is not int or sidecar["source_bytes"] != source_bytes
            or type(sidecar["frames"]) is not int or sidecar["frames"] != metadata["rows"]
            or type(sidecar["bytes"]) is not int or sidecar["bytes"] != source_bytes
            or sidecar["directions"] != metadata["directions"]
            or sidecar["hosts"] != metadata["hosts"]
            or sidecar["connection_epochs"] != metadata["connection_epochs"]
            or type(sidecar["final_sequence"]) is not int
            or sidecar["final_sequence"] != metadata["final_sequence"]
            or sidecar["state"] != "closed" or sidecar["close_reason"] != "recorder_closed"
            or not isinstance(sidecar["closed_at"], str) or not sidecar["closed_at"]
            or sidecar["current_game_id"] != "" or not isinstance(sidecar["games"], list)
            or not sidecar["games"]
            or not all(isinstance(game, dict)
                       and set(game) == {"game_id", "status", "start_frame_index", "end_frame_index",
                                         "start_sequence", "end_sequence"}
                       and game.get("status") == "settled"
                       and all(type(game[name]) is int for name in (
                           "start_frame_index", "end_frame_index", "start_sequence", "end_sequence"))
                       for game in sidecar["games"])):
        _fail("SIDECAR_IDENTITY_MISMATCH")
    sidecar_hash, _ = _file_hash(sidecar_path)
    return CaptureBinding(capture_id, source_hash, source_bytes, int(metadata["rows"]),
                          sidecar_hash, dict(metadata["directions"]),
                          tuple(dict(x) for x in metadata["connection_epochs"]),
                          tuple(dict(x) for x in sidecar["games"]),
                          bool(metadata["no_gap"]), bool(metadata["no_loss"]))


def _trusted_evidence(evidence: EvidenceRef) -> bool:
    prefixes = ("semantic.", "live.", "manual.", "capture.", "trusted.")
    lowered = evidence.source.lower()
    return (evidence.source.startswith(prefixes)
            and not any(term in lowered for term in
                        ("manifest", "ui", "default", "infer", "settlement")))


@dataclass(frozen=True, slots=True)
class ConfirmedActionTransition:
    action_index: int
    next_actor: int | Literal["terminal"]
    pass_reset: int | None
    evidence: EvidenceRef

    def validate(self, action: CommittedActionV2) -> None:
        _strict_int(self.action_index, 1, 10000, "TRANSITION_INDEX_INVALID")
        if self.action_index != action.index or type(self.evidence) is not EvidenceRef:
            _fail("TRANSITION_IDENTITY_INVALID")
        self.evidence.validate()
        if not _trusted_evidence(self.evidence):
            _fail("TRANSITION_EVIDENCE_UNTRUSTED")
        if action.next_player.status is KnowledgeStatus.KNOWN:
            if type(self.next_actor) is not int or self.next_actor != action.next_player.value:
                _fail("NEXT_ACTOR_UNAVAILABLE")
        elif action.next_player.status is KnowledgeStatus.ABSENT:
            if self.next_actor != "terminal":
                _fail("NEXT_ACTOR_UNAVAILABLE")
        else:
            _fail("NEXT_ACTOR_UNAVAILABLE")
        if action.kind == "pass":
            _strict_int(self.pass_reset, 0, 2, "PASS_RESET_UNAVAILABLE")
        elif self.pass_reset is not None:
            _fail("PASS_RESET_UNEXPECTED")


@dataclass(frozen=True, slots=True)
class ConfirmedDecisionInput:
    capture_id: str
    frame_sequence: int
    source_connection_ordinal: int
    source_sequence: int
    public_state: CanonicalGuandanPublicState
    marvel_state: MarvelState
    history: ExactHistoryJournal
    action_session: ActionProtocolSession
    cache_request: CacheRequestIdentity
    transitions: tuple[ConfirmedActionTransition, ...]


def _fact_state(value: str) -> str:
    if value not in FACT_STATES:
        _fail("CAPABILITY_STATE_INVALID")
    return value


def _quality_state(value: str) -> str:
    if value not in QUALITY_STATES:
        _fail("QUALITY_STATE_INVALID")
    return value


def _legal_rows_wire(rows: LegalActionRows) -> dict[str, object]:
    rows.validate()
    return {"rows": [list(row.values) for row in rows.rows],
            "weights": [float(value) for value in rows.weights],
            "is_commit": list(rows.is_commit), "phase": rows.phase,
            "prefix": list(rows.prefix), "session_id": rows.session_id,
            "state_version": rows.state_version}


def _coverage(session: ActionProtocolSession) -> dict[str, int]:
    values = Counter({"pass": 0, "single": 0, "pair": 0, "sequence": 0,
                      "bomb": 0, "complex": 0, "tribute": 0, "return": 0})
    for candidate in session.candidates:
        candidate.validate()
        if candidate.kind in {"pass", "tribute", "return"}:
            values[candidate.kind] += 1
        if candidate.kind != "play":
            continue
        family = candidate.declaration.family if candidate.declaration is not None else ""
        size = len(candidate.physical_cards)
        if size == 1: values["single"] += 1
        elif size == 2: values["pair"] += 1
        if family in {"STRAIGHT", "STRAIGHT_FLUSH", "PAIR_SEQUENCE", "TRIPLE_SEQUENCE"}:
            values["sequence"] += 1
        if family in {"BOMB", "STRAIGHT_FLUSH", "FOUR_KINGS"}:
            values["bomb"] += 1
        if size > 2: values["complex"] += 1
    return dict(values)



def _derive_capability_states(public_state: CanonicalGuandanPublicState,
                              action_session: ActionProtocolSession) -> dict[str, str]:
    actions = public_state.history.actions
    phase_value = public_state.phase.value if public_state.phase.status is KnowledgeStatus.KNOWN else ""
    play_candidates = [candidate for candidate in action_session.candidates if candidate.kind == "play"]
    historical_play = [action for action in actions if action.kind == "play"]
    declaration_statuses = {action.declaration.status for action in historical_play}
    if KnowledgeStatus.UNKNOWN in declaration_statuses:
        declaration_state = "missing"
    elif KnowledgeStatus.UNSUPPORTED in declaration_statuses:
        declaration_state = "unsupported"
    elif play_candidates or historical_play:
        declaration_state = "verified"
    elif phase_value in {"TRIBUTE", "RETURN"}:
        declaration_state = "not_applicable"
    else:
        declaration_state = "missing"
    exchange_candidates = [candidate for candidate in action_session.candidates
                           if candidate.kind in {"tribute", "return"}]
    exchange_statuses = {fact.status for fact in (
        public_state.exchange.tribute_donors, public_state.exchange.tribute_order,
        public_state.exchange.anti_tribute, public_state.exchange.return_obligations,
        public_state.exchange.resolved_transfers, public_state.exchange.self_selection,
    )}
    if phase_value in {"TRIBUTE", "RETURN"}:
        exchange_state = "verified" if exchange_candidates else "missing"
    elif exchange_statuses == {KnowledgeStatus.ABSENT}:
        exchange_state = "not_applicable"
    elif KnowledgeStatus.UNKNOWN in exchange_statuses:
        exchange_state = "missing"
    elif KnowledgeStatus.UNSUPPORTED in exchange_statuses:
        exchange_state = "unsupported"
    else:
        exchange_state = "verified"
    result = {name: "verified" for name in CAPABILITY_NAMES}
    result["declaration"] = declaration_state
    result["tribute_return"] = exchange_state
    return result


def _validate_public_marvel_consistency(public_state: CanonicalGuandanPublicState,
                                        marvel_state: MarvelState) -> None:
    def known_or_absent(fact: object, code: str) -> None:
        if fact.status not in {KnowledgeStatus.KNOWN, KnowledgeStatus.ABSENT,
                               KnowledgeStatus.UNKNOWN, KnowledgeStatus.UNSUPPORTED}:
            _fail(code)
    if (public_state.leader_seat.status is not KnowledgeStatus.KNOWN
            or public_state.leader_seat.value != marvel_state.leader_seat):
        _fail("LEADER_SEAT_MISMATCH")
    winner = public_state.current_winner_seat
    if ((winner.status is KnowledgeStatus.KNOWN and winner.value != marvel_state.current_winner_seat)
            or (winner.status is KnowledgeStatus.ABSENT and marvel_state.current_winner_seat is not None)
            or winner.status in {KnowledgeStatus.UNKNOWN, KnowledgeStatus.UNSUPPORTED}):
        _fail("CURRENT_WINNER_MISMATCH")
    if (public_state.consecutive_passes.status is not KnowledgeStatus.KNOWN
            or public_state.consecutive_passes.value != marvel_state.consecutive_passes
            or public_state.emptied_seats.status is not KnowledgeStatus.KNOWN
            or tuple(public_state.emptied_seats.value) != marvel_state.emptied_seats):
        _fail("PUBLIC_COUNTERS_MISMATCH")
    winning = public_state.current_winning
    if winning.status is KnowledgeStatus.ABSENT:
        if marvel_state.current_winning_cards or marvel_state.current_family is not None:
            _fail("CURRENT_WINNING_MISMATCH")
    elif winning.status is KnowledgeStatus.KNOWN:
        cards = tuple(wire_card_to_guandan_id(card) for card in winning.value.cards)
        if cards != marvel_state.current_winning_cards:
            _fail("CURRENT_WINNING_MISMATCH")
    else:
        _fail("CURRENT_WINNING_UNAVAILABLE")
    for index, fact in enumerate(public_state.finishing_ranks):
        known_or_absent(fact, "FINISHING_RANKS_INVALID")
        if fact.status is KnowledgeStatus.KNOWN and fact.value != marvel_state.finishing_ranks[index]:
            _fail("FINISHING_RANKS_MISMATCH")
    previous = public_state.previous_result
    if previous.status is KnowledgeStatus.ABSENT:
        if marvel_state.previous_result is not None:
            _fail("PREVIOUS_RESULT_MISMATCH")
    elif previous.status is KnowledgeStatus.KNOWN:
        if marvel_state.previous_result is None:
            _fail("PREVIOUS_RESULT_MISMATCH")
        summary = previous.value; facts = marvel_state.previous_result
        if (summary.ranking_by_seat != facts.ranking_by_seat
                or summary.winner_team != facts.winner_team
                or summary.outcome_class != facts.outcome_class
                or summary.tribute_cancelled.status is not KnowledgeStatus.KNOWN
                or summary.tribute_cancelled.value != facts.tribute_cancelled):
            _fail("PREVIOUS_RESULT_MISMATCH")
    else:
        _fail("PREVIOUS_RESULT_UNAVAILABLE")

def _derive_record(value: ConfirmedDecisionInput, ordinal: int) -> dict[str, object]:
    if type(value) is not ConfirmedDecisionInput:
        _fail("DECISION_INPUT_INVALID")
    _identifier(value.capture_id, "CAPTURE_ID_INVALID", 64)
    _strict_int(value.frame_sequence, 0, 2**63 - 1, "FRAME_SEQUENCE_INVALID")
    _strict_int(value.source_connection_ordinal, 1, 2**31 - 1, "CONNECTION_ORDINAL_INVALID")
    _strict_int(value.source_sequence, 0, 2**63 - 1, "SOURCE_SEQUENCE_INVALID")
    if value.frame_sequence != value.source_sequence:
        _fail("SOURCE_SEQUENCE_MISMATCH")
    if type(value.public_state) is not CanonicalGuandanPublicState:
        _fail("PUBLIC_STATE_REQUIRED")
    value.public_state.validate()
    observer = value.public_state.observer_seat
    active = value.public_state.active_seat
    level = value.public_state.level; phase = value.public_state.phase
    if observer.status is not KnowledgeStatus.KNOWN or observer.evidence is None:
        _fail("OBSERVER_IDENTITY_UNAVAILABLE")
    if not _trusted_evidence(observer.evidence):
        _fail("OBSERVER_EVIDENCE_UNTRUSTED")
    if level.status is not KnowledgeStatus.KNOWN or phase.status is not KnowledgeStatus.KNOWN:
        _fail("CANONICAL_FACTS_INCOMPLETE")
    observer_seat = _strict_int(observer.value, 0, 3, "OBSERVER_IDENTITY_UNAVAILABLE")
    if active.status is not KnowledgeStatus.KNOWN:
        _fail("ACTIVE_SEAT_UNAVAILABLE")
    active_seat = _strict_int(active.value, 0, 3, "ACTIVE_SEAT_UNAVAILABLE")
    if active_seat != observer_seat:
        _fail("OBSERVER_NOT_ACTIVE")
    level_value = _strict_int(level.value, 1, 13, "LEVEL_UNAVAILABLE")
    phase_value = phase.value
    if phase_value not in {"LEAD_PLAY", "FOLLOW_PLAY", "TRIBUTE", "RETURN"}:
        _fail("PHASE_UNAVAILABLE")
    if type(value.marvel_state) is not MarvelState:
        _fail("MARVEL_STATE_REQUIRED")
    value.marvel_state.validate()
    if (value.marvel_state.observer_seat != observer_seat
            or value.marvel_state.active_seat != active_seat
            or value.marvel_state.level != level_value or value.marvel_state.phase != phase_value
            or not value.marvel_state.history_complete):
        _fail("MARVEL_STATE_MISMATCH")
    _validate_public_marvel_consistency(value.public_state, value.marvel_state)
    current_channel = encode_channels(value.marvel_state)
    if type(value.history) is not ExactHistoryJournal or not value.history.exact:
        _fail("EXACT_FOUR_OBSERVER_HISTORY_REQUIRED")
    history_identity = value.history.identity
    if (history_identity.cursor.prefix_stage > 0
            and (history_identity.cursor.phase != phase_value
                 or history_identity.cursor.actor != active_seat)):
        _fail("HISTORY_CURSOR_MISMATCH")
    if history_identity.branch_id != "confirmed":
        _fail("SPECULATIVE_HISTORY_REJECTED")
    if (history_identity.session_id != value.public_state.identity.session_id
            or history_identity.game_id != value.public_state.identity.game_id):
        _fail("HISTORY_OWNER_MISMATCH")
    if type(value.action_session) is not ActionProtocolSession or value.action_session.closed:
        _fail("ACTION_SESSION_INVALID")
    confirmed_domain = value.action_session.confirmed_domain
    if (type(confirmed_domain) is not CanonicalGuandanPublicState
            or _json_hash(confirmed_domain.to_dict()) != _json_hash(value.public_state.to_dict())):
        _fail("CONFIRMED_DOMAIN_MISMATCH")
    if value.public_state.own_hand.status is not KnowledgeStatus.KNOWN:
        _fail("OWN_HAND_UNAVAILABLE")
    own_hand = tuple(wire_card_to_guandan_id(card) for card in value.public_state.own_hand.value)
    if value.action_session.own_hand != own_hand or value.marvel_state.own_hand != own_hand:
        _fail("OWN_HAND_MISMATCH")
    if value.public_state.remaining_counts.status is not KnowledgeStatus.KNOWN:
        _fail("REMAINING_COUNTS_UNAVAILABLE")
    if tuple(value.public_state.remaining_counts.value) != value.marvel_state.remaining_counts:
        _fail("REMAINING_COUNTS_MISMATCH")
    if (value.action_session.session_id != history_identity.session_id
            or value.action_session.state_version != value.public_state.identity.state_version
            or value.action_session.phase != phase_value
            or value.action_session.token_step != history_identity.token_step
            or value.action_session.committed_step != history_identity.committed_step
            or len(value.action_session.prefix) != history_identity.cursor.prefix_stage):
        _fail("COUNTER_OR_SESSION_MISMATCH")
    value.cache_request.validate()
    if value.cache_request.history != history_identity:
        _fail("CACHE_REQUEST_HISTORY_MISMATCH")
    actions = value.public_state.history.actions
    if type(value.transitions) is not tuple or len(value.transitions) != len(actions):
        _fail("ACTION_TRANSITIONS_INCOMPLETE")
    for action, transition in zip(actions, value.transitions):
        action.validate()
        if type(transition) is not ConfirmedActionTransition:
            _fail("ACTION_TRANSITION_INVALID")
        transition.validate(action)
    legal_rows = value.action_session.legal_rows
    legal_wire = _legal_rows_wire(legal_rows)
    public_wire = value.public_state.to_dict()
    observer_history = [list(row) for row in value.history.observer_streams[observer_seat]]
    public_action_index = len(actions)
    decision_id = _json_hash({"capture": value.capture_id,
        "game": value.public_state.identity.game_id, "ordinal": ordinal,
        "frame": value.frame_sequence, "state": value.public_state.identity.state_version,
        "action": public_action_index, "token": history_identity.token_step,
        "commit": history_identity.committed_step})
    prefix_identity = _json_hash(list(value.action_session.prefix))
    legal_hash = _json_hash(legal_wire)
    derived = _derive_capability_states(value.public_state, value.action_session)
    declaration_state = derived["declaration"]
    exchange_state = derived["tribute_return"]
    capabilities = {name: "verified" for name in CAPABILITY_NAMES}
    capabilities["declaration"] = declaration_state
    capabilities["tribute_return"] = exchange_state
    for state in capabilities.values(): _fact_state(state)
    quality = {"no_loss": "unknown", "no_gap": "unknown",
               "no_partial": "verified", "no_quarantine": "verified"}
    for state in quality.values(): _quality_state(state)
    return {
        "schema": DECISION_SCHEMA, "schema_version": 1,
        "producer": PRODUCER, "producer_version": PRODUCER_VERSION,
        "capture_id": value.capture_id, "game_id": value.public_state.identity.game_id,
        "decision_id": decision_id, "decision_ordinal": ordinal,
        "observer_seat": observer_seat, "observer_evidence": observer.evidence.to_dict(),
        "level": level_value, "phase": phase_value,
        "frame_sequence": value.frame_sequence,
        "canonical_state_version": value.public_state.identity.state_version,
        "public_action_index": public_action_index,
        "marvel_token_step": history_identity.token_step,
        "marvel_committed_step": history_identity.committed_step,
        "history_identity": history_identity.cache_key,
        "history_branch_id": history_identity.branch_id,
        "cache_generation": value.cache_request.generation,
        "pending_request_identity": value.cache_request.request_id,
        "private_prefix_identity": prefix_identity,
        "legal_action_identity": legal_hash,
        "public_self_state_contract": value.public_state.contract,
        "public_self_state_hash": _json_hash(public_wire),
        "public_self_state": public_wire,
        "encoder_channels_contract": "GuandanMARVELV2.ObserverChannels.Self.512.v1",
        "encoder_channels_hash": _json_hash(list(current_channel)),
        "observer_channel_hash": _json_hash(list(current_channel)),
        "observer_channels": list(current_channel),
        "event_history_contract": "GuandanMARVELV2.ExactHistory.v1",
        "event_history_hash": _json_hash(observer_history),
        "phase4_history_sha256": history_identity.history_sha256,
        "observer_history": observer_history,
        "private_prefix": list(value.action_session.prefix),
        "legal_rows_contract": "GuandanMARVELV2.ActionRows.v2",
        "legal_rows_hash": legal_hash, "legal_rows": legal_wire,
        "action_transitions": [{"action_index": item.action_index,
            "next_actor": item.next_actor, "pass_reset": item.pass_reset,
            "evidence": item.evidence.to_dict()} for item in value.transitions],
        "source_connection_ordinal": value.source_connection_ordinal,
        "source_sequence": value.source_sequence,
        "quality": quality, "capabilities": capabilities,
        "coverage": _coverage(value.action_session),
    }


_DECISION_KEYS = {
    "schema", "schema_version", "producer", "producer_version", "capture_id", "game_id",
    "decision_id", "decision_ordinal", "observer_seat", "observer_evidence", "level", "phase",
    "frame_sequence", "canonical_state_version", "public_action_index", "marvel_token_step",
    "marvel_committed_step", "history_identity", "history_branch_id", "cache_generation",
    "pending_request_identity", "private_prefix_identity", "legal_action_identity",
    "public_self_state_contract", "public_self_state_hash", "public_self_state",
    "encoder_channels_contract", "encoder_channels_hash", "observer_channel_hash",
    "observer_channels",
    "event_history_contract", "event_history_hash", "phase4_history_sha256", "observer_history",
    "private_prefix", "legal_rows_contract", "legal_rows_hash", "legal_rows",
    "action_transitions", "source_connection_ordinal", "source_sequence", "quality",
    "capabilities", "coverage",
}



def _validate_legal_wire(value: object) -> LegalActionRows:
    keys = {"rows", "weights", "is_commit", "phase", "prefix", "session_id", "state_version"}
    if not isinstance(value, dict) or set(value) != keys:
        _fail("LEGAL_ROWS_INVALID")
    if (not isinstance(value["rows"], list) or not isinstance(value["weights"], list)
            or not isinstance(value["is_commit"], list) or not isinstance(value["prefix"], list)
            or not isinstance(value["phase"], str) or not isinstance(value["session_id"], str)):
        _fail("LEGAL_ROWS_INVALID")
    state_version = _strict_int(value["state_version"], 0, 2**31 - 1, "LEGAL_ROWS_INVALID")
    rows = []
    for raw in value["rows"]:
        if not isinstance(raw, list) or len(raw) != 8:
            _fail("LEGAL_ROWS_INVALID")
        rows.append(ActionRow(tuple(_strict_int(item, 0, 319, "LEGAL_ROWS_INVALID") for item in raw),
                              value["session_id"], state_version))
    weights = []
    for item in value["weights"]:
        if type(item) not in {int, float} or not math.isfinite(float(item)):
            _fail("LEGAL_ROWS_INVALID")
        weights.append(float(item))
    if any(type(item) is not bool for item in value["is_commit"]):
        _fail("LEGAL_ROWS_INVALID")
    prefix = tuple(_strict_int(item, 0, 255, "LEGAL_ROWS_INVALID") for item in value["prefix"])
    if not rows:
        _fail("LEGAL_ROWS_INVALID")
    result = LegalActionRows(tuple(rows), tuple(weights), tuple(value["is_commit"]),
                             value["phase"], prefix, value["session_id"], state_version)
    result.validate(); return result

def validate_decision_record(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _DECISION_KEYS:
        _fail("DECISION_FIELDS_INVALID")
    if (value["schema"] != DECISION_SCHEMA or value["schema_version"] != 1
            or value["producer"] != PRODUCER or value["producer_version"] != PRODUCER_VERSION):
        _fail("DECISION_SCHEMA_INVALID")
    for name in ("capture_id", "game_id"):
        _identifier(value[name], "DECISION_IDENTITY_INVALID")
    for name in ("decision_id", "history_identity", "pending_request_identity",
                 "private_prefix_identity", "legal_action_identity", "public_self_state_hash",
                 "encoder_channels_hash", "observer_channel_hash", "event_history_hash",
                 "phase4_history_sha256",
                 "legal_rows_hash"):
        _sha(value[name], "DECISION_HASH_INVALID")
    for name, low, high in (
        ("decision_ordinal", 1, 10000), ("observer_seat", 0, 3), ("level", 1, 13),
        ("frame_sequence", 0, 2**63 - 1), ("canonical_state_version", 0, 2**63 - 1),
        ("public_action_index", 0, 10000), ("marvel_token_step", 0, 8191),
        ("marvel_committed_step", 0, 8191), ("cache_generation", 0, 2**31 - 1),
        ("source_connection_ordinal", 1, 2**31 - 1), ("source_sequence", 0, 2**63 - 1)):
        _strict_int(value[name], low, high, "DECISION_COUNTER_INVALID")
    if value["marvel_committed_step"] > value["marvel_token_step"]:
        _fail("DECISION_COUNTER_INVALID")
    if value["history_branch_id"] != "confirmed" or value["phase"] not in {
            "LEAD_PLAY", "FOLLOW_PLAY", "TRIBUTE", "RETURN"}:
        _fail("DECISION_CONTEXT_INVALID")
    public_state = CanonicalGuandanPublicState.from_dict(value["public_self_state"])
    if (value["public_self_state_contract"] != "CanonicalGuandanPublicState.v2"
            or public_state.contract != value["public_self_state_contract"]
            or _json_hash(value["public_self_state"]) != value["public_self_state_hash"]):
        _fail("PUBLIC_STATE_HASH_MISMATCH")
    if (public_state.identity.game_id != value["game_id"]
            or public_state.identity.state_version != value["canonical_state_version"]
            or public_state.observer_seat.status is not KnowledgeStatus.KNOWN
            or public_state.observer_seat.value != value["observer_seat"]
            or public_state.observer_seat.evidence is None
            or public_state.observer_seat.evidence.to_dict() != value["observer_evidence"]
            or public_state.active_seat.status is not KnowledgeStatus.KNOWN
            or public_state.active_seat.value != value["observer_seat"]
            or public_state.own_hand.status is not KnowledgeStatus.KNOWN
            or public_state.remaining_counts.status is not KnowledgeStatus.KNOWN
            or public_state.level.status is not KnowledgeStatus.KNOWN
            or public_state.level.value != value["level"]
            or public_state.phase.status is not KnowledgeStatus.KNOWN
            or public_state.phase.value != value["phase"]
            or len(public_state.history.actions) != value["public_action_index"]):
        _fail("DECISION_PUBLIC_STATE_MISMATCH")
    if value["frame_sequence"] != value["source_sequence"]:
        _fail("SOURCE_SEQUENCE_MISMATCH")
    channels = value["observer_channels"]
    if not isinstance(channels, list) or len(channels) != 512 or any(type(x) is not int or not 0 <= x <= 255 for x in channels):
        _fail("OBSERVER_CHANNELS_INVALID")
    if (value["encoder_channels_contract"] != "GuandanMARVELV2.ObserverChannels.Self.512.v1"
            or _json_hash(channels) != value["observer_channel_hash"]
            or value["encoder_channels_hash"] != value["observer_channel_hash"]):
        _fail("OBSERVER_CHANNEL_HASH_MISMATCH")
    if value["event_history_contract"] != "GuandanMARVELV2.ExactHistory.v1":
        _fail("EVENT_HISTORY_CONTRACT_INVALID")
    history = value["observer_history"]
    if (not isinstance(history, list) or not history or
            any(not isinstance(row, list) or len(row) != 48
                or any(type(token) is not int or not 0 <= token <= 319 for token in row)
                for row in history)):
        _fail("OBSERVER_HISTORY_INVALID")
    if _json_hash(history) != value["event_history_hash"]:
        _fail("EVENT_HISTORY_HASH_MISMATCH")
    if value["legal_rows_contract"] != "GuandanMARVELV2.ActionRows.v2":
        _fail("LEGAL_ROWS_CONTRACT_INVALID")
    legal = value["legal_rows"]
    legal_object = _validate_legal_wire(legal)
    if (legal_object.phase != value["phase"]
            or legal_object.state_version != value["canonical_state_version"]):
        _fail("LEGAL_ROWS_CONTEXT_MISMATCH")
    if _json_hash(legal) != value["legal_rows_hash"]:
        _fail("LEGAL_ROWS_HASH_MISMATCH")
    if value["legal_action_identity"] != value["legal_rows_hash"]:
        _fail("LEGAL_ACTION_IDENTITY_MISMATCH")
    if not isinstance(value["private_prefix"], list) or _json_hash(value["private_prefix"]) != value["private_prefix_identity"]:
        _fail("PRIVATE_PREFIX_HASH_MISMATCH")
    transitions = value["action_transitions"]
    if not isinstance(transitions, list) or len(transitions) != len(public_state.history.actions):
        _fail("ACTION_TRANSITIONS_INCOMPLETE")
    for raw, action in zip(transitions, public_state.history.actions):
        if not isinstance(raw, dict) or set(raw) != {"action_index", "next_actor", "pass_reset", "evidence"}:
            _fail("ACTION_TRANSITION_INVALID")
        transition = ConfirmedActionTransition(raw["action_index"], raw["next_actor"],
                                               raw["pass_reset"], EvidenceRef.from_dict(raw["evidence"]))
        transition.validate(action)
    if not isinstance(value["capabilities"], dict) or set(value["capabilities"]) != set(CAPABILITY_NAMES):
        _fail("CAPABILITIES_INVALID")
    for state in value["capabilities"].values(): _fact_state(state)
    expected_capabilities = {name: "verified" for name in CAPABILITY_NAMES}
    historical_play = [action for action in public_state.history.actions if action.kind == "play"]
    statuses = {action.declaration.status for action in historical_play}
    has_declaration_path = bool(legal_object.prefix) or any(row.kind != "PASS" for row in legal_object.rows)
    if KnowledgeStatus.UNKNOWN in statuses: expected_capabilities["declaration"] = "missing"
    elif KnowledgeStatus.UNSUPPORTED in statuses: expected_capabilities["declaration"] = "unsupported"
    elif value["phase"] in {"TRIBUTE", "RETURN"}: expected_capabilities["declaration"] = "not_applicable"
    elif not historical_play and not has_declaration_path: expected_capabilities["declaration"] = "missing"
    exchange_statuses = {fact.status for fact in (
        public_state.exchange.tribute_donors, public_state.exchange.tribute_order,
        public_state.exchange.anti_tribute, public_state.exchange.return_obligations,
        public_state.exchange.resolved_transfers, public_state.exchange.self_selection)}
    if value["phase"] in {"TRIBUTE", "RETURN"}: expected_capabilities["tribute_return"] = "verified"
    elif exchange_statuses == {KnowledgeStatus.ABSENT}: expected_capabilities["tribute_return"] = "not_applicable"
    elif KnowledgeStatus.UNKNOWN in exchange_statuses: expected_capabilities["tribute_return"] = "missing"
    elif KnowledgeStatus.UNSUPPORTED in exchange_statuses: expected_capabilities["tribute_return"] = "unsupported"
    if value["capabilities"] != expected_capabilities:
        _fail("CAPABILITIES_NOT_DERIVED")
    expected_quality = {"no_loss": "unknown", "no_gap": "unknown",
                        "no_partial": "verified", "no_quarantine": "verified"}
    if value["quality"] != expected_quality:
        _fail("QUALITY_NOT_DERIVED")
    coverage_keys = {"pass", "single", "pair", "sequence", "bomb", "complex", "tribute", "return"}
    if not isinstance(value["coverage"], dict) or set(value["coverage"]) != coverage_keys:
        _fail("COVERAGE_INVALID")
    for count in value["coverage"].values(): _strict_int(count, 0, 2**31 - 1, "COVERAGE_INVALID")
    EvidenceRef.from_dict(value["observer_evidence"])
    expected_decision_id = _json_hash({"capture": value["capture_id"],
        "game": value["game_id"], "ordinal": value["decision_ordinal"],
        "frame": value["frame_sequence"], "state": value["canonical_state_version"],
        "action": value["public_action_index"], "token": value["marvel_token_step"],
        "commit": value["marvel_committed_step"]})
    if value["decision_id"] != expected_decision_id:
        _fail("DECISION_IDENTITY_MISMATCH")
    return value


@dataclass(frozen=True, slots=True)
class ProducerCloseResult:
    status: Literal["eligible", "blocked"]
    missing_fact_codes: tuple[str, ...]
    decision_count: int
    manifest_sha256: str = ""


class CanonicalCaptureConverter:
    """Single-owner producer for one local capture directory."""

    producer = PRODUCER
    producer_version = PRODUCER_VERSION

    def __init__(self, capture_dir: Path, capture_id: str) -> None:
        self.capture_dir = _safe_capture_directory(Path(capture_dir).resolve(), capture_id)
        self.capture_id = _identifier(capture_id, "CAPTURE_ID_INVALID", 64)
        self.source_path = self.capture_dir / "frames.jsonl"
        self.sidecar_path = self.capture_dir / "capture_status.json"
        self.journal_path = self.capture_dir / JOURNAL_FILENAME
        self.manifest_path = self.capture_dir / MANIFEST_FILENAME
        self.status_path = self.capture_dir / STATUS_FILENAME
        self._temporary = self.journal_path.with_name(self.journal_path.name + ".tmp")
        if any(path.exists() for path in (self.journal_path, self.manifest_path, self.status_path)):
            _fail("OUTPUT_ALREADY_EXISTS")
        self._temporary.unlink(missing_ok=True)
        try:
            self._stream = self._temporary.open("x", encoding="utf-8", newline="\n")
        except OSError:
            _fail("JOURNAL_OPEN_FAILED")
        self._records: list[dict[str, object]] = []
        self._missing: set[str] = set()
        self._closed: ProducerCloseResult | None = None
        self._last_frame = -1; self._last_state = -1; self._last_action = -1
        self._last_token = -1; self._last_commit = -1; self._last_request_sequence = 0
        self._opportunities: list[tuple[int, int, int]] = []

    def block(self, code: str) -> None:
        if not _safe_code(code): code = "CANONICAL_CAPTURE_REJECTED"
        self._missing.add(code)

    def observe_decision_opportunity(self, *, frame_sequence: int,
                                     state_version: int, public_action_index: int) -> None:
        if self._closed is not None:
            _fail("PRODUCER_CLOSED")
        current = (_strict_int(frame_sequence, 0, 2**63 - 1, "FRAME_SEQUENCE_INVALID"),
                   _strict_int(state_version, 0, 2**63 - 1, "STATE_VERSION_INVALID"),
                   _strict_int(public_action_index, 0, 10000, "ACTION_INDEX_INVALID"))
        if self._opportunities:
            previous = self._opportunities[-1]
            if current[0] <= previous[0] or current[1] <= previous[1] or current[2] < previous[2]:
                self.block("DECISION_OPPORTUNITY_NOT_MONOTONIC")
                _fail("DECISION_OPPORTUNITY_NOT_MONOTONIC")
        self._opportunities.append(current)

    def observe_engine_update(self, update: object) -> None:
        from guance.contracts import EngineUpdate
        if type(update) is not EngineUpdate:
            self.block("ENGINE_UPDATE_INVALID")
            return
        if update.kind == "error":
            game_scoped = (isinstance(update.data, dict)
                           and update.data.get("scope") == "game"
                           and update.error_code in GAME_SCOPED_EXCLUSIONS)
            if not game_scoped:
                self.block("ENGINE_ERROR_OBSERVED")
        quality = update.data.get("quality") if isinstance(update.data, dict) else None
        if isinstance(quality, dict):
            if quality.get("lost") is True: self.block("SOURCE_LOSS_OBSERVED")
            if quality.get("status") == "partial": self.block("PARTIAL_STATE_OBSERVED")
            if quality.get("status") == "quarantine": self.block("QUARANTINE_OBSERVED")

    def observe_history_invalidation(self) -> None:
        self.block("HISTORY_INVALIDATED")

    def observe_cache_invalidation(self) -> None:
        self.block("CACHE_GENERATION_INVALIDATED")

    def record_decision(self, value: ConfirmedDecisionInput) -> str:
        if self._closed is not None:
            _fail("PRODUCER_CLOSED")
        if self._missing:
            _fail("PRODUCER_BLOCKED")
        try:
            record = _derive_record(value, len(self._records) + 1)
            validate_decision_record(record)
            if record["capture_id"] != self.capture_id:
                _fail("CAPTURE_ID_MISMATCH")
            expected_index = len(self._records)
            if expected_index >= len(self._opportunities):
                _fail("DECISION_OPPORTUNITY_MISSING")
            expected = self._opportunities[expected_index]
            if expected != (record["frame_sequence"], record["canonical_state_version"],
                            record["public_action_index"]):
                _fail("DECISION_OPPORTUNITY_MISMATCH")
            counters = (record["frame_sequence"], record["canonical_state_version"],
                        record["public_action_index"], record["marvel_token_step"],
                        record["marvel_committed_step"])
            previous = (self._last_frame, self._last_state, self._last_action,
                        self._last_token, self._last_commit)
            if (type(counters[0]) is not int or counters[0] <= previous[0]
                    or type(counters[1]) is not int or counters[1] <= previous[1]
                    or any(type(current) is not int or current < prior
                           for current, prior in zip(counters[2:], previous[2:]))):
                _fail("DECISION_COUNTER_NOT_MONOTONIC")
            request_sequence = value.cache_request.sequence
            if request_sequence != self._last_request_sequence + 1:
                _fail("CACHE_REQUEST_SEQUENCE_GAP")
            if any(existing["decision_id"] == record["decision_id"] for existing in self._records):
                _fail("DECISION_ID_DUPLICATE")
            line = _canonical_bytes(record).decode("utf-8") + "\n"
            self._stream.write(line); self._stream.flush()
            self._records.append(record)
            (self._last_frame, self._last_state, self._last_action,
             self._last_token, self._last_commit) = counters
            self._last_request_sequence = request_sequence
            return str(record["decision_id"])
        except CanonicalCaptureError as exc:
            self.block(exc.code)
            raise
        except (OSError, TypeError, ValueError, OverflowError):
            self.block("DECISION_WRITE_FAILED")
            _fail("DECISION_WRITE_FAILED")

    def _close_stream(self) -> bool:
        stream, self._stream = self._stream, None
        if stream is None: return True
        ok = True
        try: stream.flush(); os.fsync(stream.fileno())
        except OSError: ok = False
        try: stream.close()
        except OSError: ok = False
        return ok

    def _status(self, binding: CaptureBinding | None) -> ProducerCloseResult:
        self._temporary.unlink(missing_ok=True)
        self.manifest_path.unlink(missing_ok=True)
        codes = tuple(sorted(self._missing or {"CANONICAL_DECISIONS_UNAVAILABLE"}))
        value = {"schema": STATUS_SCHEMA, "capture_id": self.capture_id, "status": "blocked",
                 "missing_fact_codes": list(codes),
                 "source_sha256": binding.source_sha256 if binding else "",
                 "source_bytes": binding.source_bytes if binding else 0,
                 "source_rows": binding.source_rows if binding else 0,
                 "sidecar_sha256": binding.sidecar_sha256 if binding else "",
                 "canonical_decisions_rows": len(self._records),
                 "created_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        if not self.status_path.exists(): _atomic_json(self.status_path, value)
        result = ProducerCloseResult("blocked", codes, len(self._records))
        self._closed = result; return result

    def close(self) -> ProducerCloseResult:
        if self._closed is not None: return self._closed
        binding: CaptureBinding | None = None
        try: binding = inspect_closed_capture(self.capture_dir, self.capture_id)
        except CanonicalCaptureError as exc: self.block(exc.code)
        if not self._records: self.block("CANONICAL_DECISIONS_MISSING")
        if len(self._opportunities) != len(self._records): self.block("PARTIAL_DECISIONS")
        if not self._close_stream(): self.block("JOURNAL_CLOSE_FAILED")
        if self._missing or binding is None: return self._status(binding)
        for record in self._records:
            ordinal = int(record["source_connection_ordinal"])
            if not 1 <= ordinal <= len(binding.connection_epochs):
                self.block("SOURCE_DECISION_BINDING_MISMATCH")
                break
            epoch = binding.connection_epochs[ordinal - 1]
            sequence = int(record["source_sequence"])
            if not int(epoch["start_sequence"]) <= sequence <= int(epoch["end_sequence"]):
                self.block("SOURCE_DECISION_BINDING_MISMATCH")
                break
        if self._missing: return self._status(binding)
        try:
            journal_hash, journal_bytes = _file_hash(self._temporary)
            journal_rows = sum(1 for _ in self._temporary.open("rb"))
            if journal_rows != len(self._records): _fail("JOURNAL_ROWS_MISMATCH")
            if self.journal_path.exists(): _fail("OUTPUT_ALREADY_EXISTS")
            self._temporary.replace(self.journal_path)
            capabilities = _aggregate_fact_states(record["capabilities"] for record in self._records)
            quality = {"no_loss": "verified" if binding.no_loss else "failed",
                       "no_gap": "verified" if binding.no_gap else "failed",
                       "no_partial": "verified", "no_quarantine": "verified"}
            missing = sorted(name.upper() + "_" + state.upper()
                             for name, state in {**capabilities, **quality}.items()
                             if state not in {"verified", "not_applicable"})
            coverage = Counter()
            for record in self._records: coverage.update(record["coverage"])
            eligible = not missing
            manifest = {"schema": MANIFEST_SCHEMA, "schema_version": 2,
                "producer": PRODUCER, "producer_version": PRODUCER_VERSION,
                "capture_id": self.capture_id, "capture_schema": CAPTURE_SCHEMA,
                "protocol_identity": PROTOCOL_IDENTITY, "target_host_identity": TARGET_HOST,
                "source_filename": "frames.jsonl", "source_sha256": binding.source_sha256,
                "source_bytes": binding.source_bytes, "source_rows": binding.source_rows,
                "closed_sidecar_filename": "capture_status.json",
                "closed_sidecar_sha256": binding.sidecar_sha256,
                "canonical_decisions_filename": JOURNAL_FILENAME,
                "canonical_decisions_sha256": journal_hash,
                "canonical_decisions_bytes": journal_bytes,
                "canonical_decisions_rows": journal_rows,
                "direction_counts": dict(binding.direction_counts),
                "connection_epochs": [dict(item) for item in binding.connection_epochs],
                "game_count": len(binding.games), "decision_count": len(self._records),
                "decision_opportunity_count": len(self._opportunities),
                "levels": sorted({int(record["level"]) for record in self._records}),
                "observer_seats": sorted({int(record["observer_seat"]) for record in self._records}),
                "counter_contract": dict(COUNTER_CONTRACT), "capabilities": capabilities,
                "quality": quality, "coverage": dict(sorted(coverage.items())),
                "eligibility": "eligible" if eligible else "blocked",
                "missing_fact_codes": missing}
            if not eligible:
                self._missing.update(missing); return self._status(binding)
            _atomic_json(self.manifest_path, manifest)
            summary = validate_v2_candidate(self.capture_dir, self.capture_id)
            if not summary["eligible"]:
                self.manifest_path.unlink(missing_ok=True); self.block("FINAL_BINDING_RECHECK_FAILED")
                return self._status(binding)
            manifest_hash, _ = _file_hash(self.manifest_path)
            self.status_path.unlink(missing_ok=True)
            result = ProducerCloseResult("eligible", (), len(self._records), manifest_hash)
            self._closed = result; return result
        except CanonicalCaptureError as exc:
            self.block(exc.code); return self._status(binding)
        except (OSError, TypeError, ValueError, OverflowError):
            self.block("PRODUCER_CLOSE_FAILED"); return self._status(binding)


def _aggregate_fact_states(values: Iterable[Mapping[str, str]]) -> dict[str, str]:
    items = tuple(values)
    if not items: _fail("CAPABILITIES_EMPTY")
    result: dict[str, str] = {}
    for name in CAPABILITY_NAMES:
        states = {item[name] for item in items}
        for state in states: _fact_state(state)
        if "missing" in states: result[name] = "missing"
        elif "unsupported" in states: result[name] = "unsupported"
        elif states == {"not_applicable"}: result[name] = "not_applicable"
        else: result[name] = "verified"
    return result


def _manifest_keys() -> set[str]:
    return {"schema", "schema_version", "producer", "producer_version", "capture_id",
            "capture_schema", "protocol_identity", "target_host_identity", "source_filename",
            "source_sha256", "source_bytes", "source_rows", "closed_sidecar_filename",
            "closed_sidecar_sha256", "canonical_decisions_filename",
            "canonical_decisions_sha256", "canonical_decisions_bytes",
            "canonical_decisions_rows", "direction_counts", "connection_epochs",
            "game_count", "decision_count", "decision_opportunity_count", "levels",
            "observer_seats", "counter_contract",
            "capabilities", "quality", "coverage", "eligibility", "missing_fact_codes"}


def validate_v2_candidate(capture_dir: Path, capture_id: str) -> dict[str, object]:
    capture_dir = _safe_capture_directory(Path(capture_dir).resolve(), capture_id)
    manifest_path = capture_dir / MANIFEST_FILENAME; journal_path = capture_dir / JOURNAL_FILENAME
    if not manifest_path.is_file() or not journal_path.is_file():
        _fail("V2_INPUT_MISSING")
    try: manifest = strict_json_loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError): _fail("V2_MANIFEST_INVALID")
    if (not isinstance(manifest, dict) or set(manifest) != _manifest_keys()
            or manifest["schema"] != MANIFEST_SCHEMA or manifest["schema_version"] != 2
            or manifest["producer"] != PRODUCER or manifest["producer_version"] != PRODUCER_VERSION
            or manifest["capture_id"] != capture_id or manifest["capture_schema"] != CAPTURE_SCHEMA
            or manifest["protocol_identity"] != PROTOCOL_IDENTITY
            or manifest["target_host_identity"] != TARGET_HOST
            or manifest["source_filename"] != "frames.jsonl"
            or manifest["closed_sidecar_filename"] != "capture_status.json"
            or manifest["canonical_decisions_filename"] != JOURNAL_FILENAME
            or manifest["counter_contract"] != COUNTER_CONTRACT):
        _fail("V2_MANIFEST_INVALID")
    for name in ("source_sha256", "closed_sidecar_sha256", "canonical_decisions_sha256"):
        _sha(manifest[name], "V2_MANIFEST_HASH_INVALID")
    for name, low, high in (("source_bytes", 1, 2**63 - 1), ("source_rows", 1, 2**31 - 1),
                            ("canonical_decisions_bytes", 1, 2**63 - 1),
                            ("canonical_decisions_rows", 1, 10000),
                            ("game_count", 1, 10000), ("decision_count", 1, 10000),
                            ("decision_opportunity_count", 1, 10000)):
        _strict_int(manifest[name], low, high, "V2_MANIFEST_COUNTER_INVALID")
    if (not isinstance(manifest["levels"], list)
            or any(type(item) is not int or not 1 <= item <= 13 for item in manifest["levels"])
            or not isinstance(manifest["observer_seats"], list)
            or any(type(item) is not int or item not in range(4) for item in manifest["observer_seats"])
            or manifest["eligibility"] not in {"eligible", "blocked"}
            or not isinstance(manifest["missing_fact_codes"], list)
            or any(not _safe_code(code) for code in manifest["missing_fact_codes"])):
        _fail("V2_MANIFEST_INVALID")
    if not isinstance(manifest["capabilities"], dict) or set(manifest["capabilities"]) != set(CAPABILITY_NAMES):
        _fail("V2_MANIFEST_INVALID")
    for state in manifest["capabilities"].values(): _fact_state(state)
    if not isinstance(manifest["quality"], dict) or set(manifest["quality"]) != set(QUALITY_NAMES):
        _fail("V2_MANIFEST_INVALID")
    for state in manifest["quality"].values(): _quality_state(state)
    coverage_keys = {"pass", "single", "pair", "sequence", "bomb", "complex", "tribute", "return"}
    if not isinstance(manifest["coverage"], dict) or set(manifest["coverage"]) != coverage_keys:
        _fail("V2_MANIFEST_INVALID")
    for count in manifest["coverage"].values(): _strict_int(count, 0, 2**31 - 1, "V2_MANIFEST_INVALID")
    _strict_counts(manifest["direction_counts"], "V2_MANIFEST_INVALID")
    _strict_epochs(manifest["connection_epochs"], "V2_MANIFEST_INVALID")
    binding = inspect_closed_capture(capture_dir, capture_id)
    journal_hash, journal_bytes = _file_hash(journal_path)
    if (manifest["source_sha256"] != binding.source_sha256
            or manifest["source_bytes"] != binding.source_bytes
            or manifest["source_rows"] != binding.source_rows
            or manifest["closed_sidecar_sha256"] != binding.sidecar_sha256
            or manifest["canonical_decisions_sha256"] != journal_hash
            or manifest["canonical_decisions_bytes"] != journal_bytes
            or manifest["direction_counts"] != dict(binding.direction_counts)
            or manifest["connection_epochs"] != [dict(x) for x in binding.connection_epochs]
            or manifest["game_count"] != len(binding.games)):
        _fail("V2_IDENTITY_MISMATCH")
    records: list[dict[str, object]] = []; previous = (-1, -1, -1, -1, -1); ids: set[str] = set()
    try:
        with journal_path.open("r", encoding="utf-8") as stream:
            for ordinal, line in enumerate(stream, 1):
                record = validate_decision_record(strict_json_loads(line))
                if record["capture_id"] != capture_id or record["decision_ordinal"] != ordinal:
                    _fail("JOURNAL_IDENTITY_MISMATCH")
                connection_ordinal = int(record["source_connection_ordinal"])
                if not 1 <= connection_ordinal <= len(binding.connection_epochs):
                    _fail("SOURCE_DECISION_BINDING_MISMATCH")
                epoch = binding.connection_epochs[connection_ordinal - 1]
                if not int(epoch["start_sequence"]) <= int(record["source_sequence"]) <= int(epoch["end_sequence"]):
                    _fail("SOURCE_DECISION_BINDING_MISMATCH")
                current = (record["frame_sequence"], record["canonical_state_version"],
                           record["public_action_index"], record["marvel_token_step"],
                           record["marvel_committed_step"])
                if (type(current[0]) is not int or current[0] <= previous[0]
                        or type(current[1]) is not int or current[1] <= previous[1]
                        or any(type(x) is not int or x < y
                               for x, y in zip(current[2:], previous[2:]))):
                    _fail("JOURNAL_COUNTER_NOT_MONOTONIC")
                previous = current
                if record["decision_id"] in ids: _fail("DECISION_ID_DUPLICATE")
                ids.add(record["decision_id"]); records.append(record)
    except (OSError, UnicodeError): _fail("JOURNAL_INVALID")
    if (not records or manifest["canonical_decisions_rows"] != len(records)
            or manifest["decision_count"] != len(records)
            or manifest["decision_opportunity_count"] != len(records)):
        _fail("JOURNAL_ROWS_MISMATCH")
    capabilities = _aggregate_fact_states(record["capabilities"] for record in records)
    quality = {"no_loss": "verified" if binding.no_loss else "failed",
               "no_gap": "verified" if binding.no_gap else "failed",
               "no_partial": "verified", "no_quarantine": "verified"}
    coverage = Counter()
    for record in records: coverage.update(record["coverage"])
    eligible = (manifest["capabilities"] == capabilities and manifest["quality"] == quality
                and manifest["coverage"] == dict(sorted(coverage.items()))
                and manifest["levels"] == sorted({r["level"] for r in records})
                and manifest["observer_seats"] == sorted({r["observer_seat"] for r in records})
                and manifest["eligibility"] == "eligible" and manifest["missing_fact_codes"] == []
                and all(state in {"verified", "not_applicable"} for state in capabilities.values())
                and all(state == "verified" for state in quality.values()))
    return {"capture_id": capture_id, "schema": MANIFEST_SCHEMA,
            "producer": PRODUCER, "producer_version": PRODUCER_VERSION,
            "protocol_identity": PROTOCOL_IDENTITY, "source_sha256": binding.source_sha256,
            "source_bytes": binding.source_bytes, "source_rows": binding.source_rows,
            "sidecar_status": "valid", "canonical_journal_status": "valid",
            "game_count": len(binding.games), "decision_count": len(records),
            "levels": manifest["levels"], "observer_seats": manifest["observer_seats"],
            "coverage": manifest["coverage"], "capability_states": capabilities,
            "quality_states": quality, "missing_fact_codes": [] if eligible else ["V2_NOT_ELIGIBLE"],
            "eligible": eligible}


__all__ = [
    "CanonicalCaptureConverter", "CanonicalCaptureError", "ConfirmedActionTransition",
    "ConfirmedDecisionInput", "ProducerCloseResult", "validate_decision_record",
    "validate_v2_candidate", "strict_json_loads", "inspect_closed_capture",
]
