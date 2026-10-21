"""Bounded pure-data projection/parser for confirmed-decision recommendation.

Wire schema: ``GuandanMARVELV2.ConfirmedDecision.v1``.

Only public+self facts, the observer's own hand, exact four-observer history
rows, legal-row identity and cache/model identity cross the process boundary.
Never serialized here: hidden opponent hands, raw capture frames/payloads,
credentials, checkpoint tensors, provider objects or absolute paths.

Both sides of the wire use the same strict parser: the client validates the
projection before sending, and the worker-side provider re-parses and
re-validates it before any model work. Any malformed, unknown, duplicated,
non-finite, out-of-bound, mismatched or incomplete field fails closed with a
bounded error code.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any

from guance.contracts import CONFIRMED_DECISION_REQUEST_CONTRACT
from guance.ports.model import ModelBackendError
from guance.public_state_contract import (
    KnowledgeStatus, PublicDeclaration, wire_card_to_guandan_id,
)
from .action_decoder import declaration_tokens
from .contracts import (
    FAMILIES, PHASES, EVENT_WIDTH, OBSERVER_COUNT, MAX_ACTION_ROWS, MAX_PREFIX_TOKENS,
    ActionDeclaration, ExchangeFacts, ExchangeTransferFacts, MarvelState,
    PreviousResultFacts, WildAssignment,
)
from .history import ExactHistoryJournal, HistoryStep
from .lifecycle_contracts import CacheRequestIdentity, HistoryCursor, HistoryIdentity
from .trick_replay import replay_trick_topology

MAX_CONFIRMED_HISTORY_STEPS = 512
_MAX_PAYLOAD_BYTES = 1_500_000
_MAX_TEXT = 128
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_BOOL = (bool,)

TOP_KEYS = frozenset({
    "schema", "capture_id", "frame_sequence", "session_id", "game_id", "state_version",
    "model", "marvel_state", "current_winning_declaration", "session", "history",
    "cache_request", "decision_identity_sha256",
})
_SESSION_KEYS = frozenset({"phase", "own_hand", "prefix", "token_step", "committed_step",
                           "candidates_sha256", "legal_generation", "legal_row_count"})
_HISTORY_KEYS = frozenset({"identity", "steps"})
_IDENTITY_KEYS = frozenset({"session_id", "game_id", "state_id", "artifact_id",
                            "artifact_sha256", "history_sha256", "branch_id",
                            "event_count", "cursor", "exact"})
_CURSOR_KEYS = frozenset({"phase", "actor", "token_step", "committed_step", "prefix_stage",
                          "pending_exchange_count", "context_bound", "awaiting_play"})
_CACHE_KEYS = frozenset({"request_id", "lifecycle_id", "request_key", "sequence",
                         "generation", "history"})
_MODEL_KEYS = frozenset({"artifact_id", "artifact_sha256"})
_STATE_KEYS = frozenset({
    "observer_seat", "active_seat", "leader_seat", "current_winner_seat", "phase", "level",
    "own_hand", "current_winning_cards", "self_exchange_cards", "private_prefix",
    "remaining_counts", "finishing_ranks", "emptied_seats", "consecutive_passes",
    "current_family", "current_comparison_rank", "current_winning_size", "previous_result",
    "exchange", "token_step", "committed_step", "history_complete",
})
_EXCHANGE_KEYS = frozenset({"tribute_donors", "tribute_order", "anti_tribute",
                            "return_obligations", "resolved_transfers", "self_selection",
                            "outgoing_return_transfer"})
_TRANSFER_KEYS = frozenset({"kind", "source_seat", "target_seat", "card_id"})
_PREVIOUS_KEYS = frozenset({"ranking_by_seat", "winner_team", "outcome_class",
                            "tribute_cancelled"})


def _fail(code: str = "MODEL_INPUT_INVALID") -> None:
    raise ModelBackendError(code)


def _exact_keys(value: Any, keys: frozenset, code: str = "MODEL_INPUT_INVALID") -> dict:
    if type(value) is not dict or frozenset(value.keys()) != keys:
        _fail(code)
    return value


def _int(value: Any, minimum: int, maximum: int, code: str = "MODEL_INPUT_INVALID") -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(code)
    return value


def _text(value: Any, code: str = "MODEL_INPUT_INVALID") -> str:
    if (not isinstance(value, str) or not value or len(value) > _MAX_TEXT
            or not all(char.isalnum() or char in "._:-" for char in value)):
        _fail(code)
    return value


def _hex64(value: Any, code: str = "MODEL_INPUT_INVALID") -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        _fail(code)
    return value


def _flag(value: Any, code: str = "MODEL_INPUT_INVALID") -> bool:
    if type(value) is not bool:
        _fail(code)
    return value


def _tuple_ints(value: Any, minimum: int, maximum: int, limit: int,
                code: str = "MODEL_INPUT_INVALID", *, unique: bool = False) -> tuple[int, ...]:
    if (not isinstance(value, (list, tuple)) or len(value) > limit
            or any(type(item) is not int or not minimum <= item <= maximum for item in value)
            or (unique and len(set(value)) != len(value))):
        _fail(code)
    return tuple(value)


def _weight(value: Any, code: str = "MODEL_INPUT_INVALID") -> float:
    if type(value) not in (int, float) or isinstance(value, bool):
        _fail(code)
    weight = float(value)
    if not math.isfinite(weight) or weight <= 0:
        _fail(code)
    return weight


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _marvel_state_wire(state: MarvelState) -> dict[str, Any]:
    previous = state.previous_result
    exchange = state.exchange
    return {
        "observer_seat": state.observer_seat, "active_seat": state.active_seat,
        "leader_seat": state.leader_seat, "current_winner_seat": state.current_winner_seat,
        "phase": state.phase, "level": state.level, "own_hand": list(state.own_hand),
        "current_winning_cards": list(state.current_winning_cards),
        "self_exchange_cards": list(state.self_exchange_cards),
        "private_prefix": list(state.private_prefix),
        "remaining_counts": list(state.remaining_counts),
        "finishing_ranks": list(state.finishing_ranks),
        "emptied_seats": list(state.emptied_seats),
        "consecutive_passes": state.consecutive_passes,
        "current_family": state.current_family,
        "current_comparison_rank": state.current_comparison_rank,
        "current_winning_size": state.current_winning_size,
        "previous_result": None if previous is None else {
            "ranking_by_seat": list(previous.ranking_by_seat),
            "winner_team": previous.winner_team,
            "outcome_class": previous.outcome_class,
            "tribute_cancelled": previous.tribute_cancelled,
        },
        "exchange": {
            "tribute_donors": list(exchange.tribute_donors),
            "tribute_order": list(exchange.tribute_order),
            "anti_tribute": exchange.anti_tribute,
            "return_obligations": [list(pair) for pair in exchange.return_obligations],
            "resolved_transfers": [
                {"kind": item.kind, "source_seat": item.source_seat,
                 "target_seat": item.target_seat, "card_id": item.card_id}
                for item in exchange.resolved_transfers],
            "self_selection": list(exchange.self_selection),
            "outgoing_return_transfer": exchange.outgoing_return_transfer,
        },
        "token_step": state.token_step, "committed_step": state.committed_step,
        "history_complete": state.history_complete,
    }


def _history_identity_wire(identity: HistoryIdentity) -> dict[str, Any]:
    cursor = identity.cursor
    return {
        "session_id": identity.session_id, "game_id": identity.game_id,
        "state_id": identity.state_id, "artifact_id": identity.artifact_id,
        "artifact_sha256": identity.artifact_sha256,
        "history_sha256": identity.history_sha256, "branch_id": identity.branch_id,
        "event_count": identity.event_count,
        "cursor": {
            "phase": cursor.phase, "actor": cursor.actor,
            "token_step": cursor.token_step, "committed_step": cursor.committed_step,
            "prefix_stage": cursor.prefix_stage,
            "pending_exchange_count": cursor.pending_exchange_count,
            "context_bound": cursor.context_bound, "awaiting_play": cursor.awaiting_play,
        },
        "exact": identity.exact,
    }


def _cache_request_wire(request: CacheRequestIdentity) -> dict[str, Any]:
    return {
        "request_id": request.request_id, "lifecycle_id": request.lifecycle_id,
        "request_key": request.request_key, "sequence": request.sequence,
        "generation": request.generation,
        "history": _history_identity_wire(request.history),
    }


def decision_identity_sha256(core: Mapping[str, Any]) -> str:
    """Bind the complete canonical confirmed-decision payload (except the id itself)."""
    return _canonical_hash({key: value for key, value in core.items()
                            if key != "decision_identity_sha256"})


def candidate_sequences_sha256(candidates: Sequence[Any]) -> str:
    payload = [{"tokens": list(candidate.tokens), "weight": float(candidate.weight)}
               for candidate in candidates]
    return _canonical_hash(payload)


def project_confirmed_decision(decision: Any, *, artifact_id: str,
                               artifact_sha256: str) -> dict[str, Any]:
    """Project one validated ConfirmedDecisionInput into the bounded wire payload."""
    from guance.infrastructure.capture.canonical_capture import ConfirmedDecisionInput
    if type(decision) is not ConfirmedDecisionInput:
        _fail("CONFIRMED_DECISION_UNAVAILABLE")
    _text(artifact_id, "CONFIRMED_DECISION_UNAVAILABLE")
    _hex64(artifact_sha256, "CONFIRMED_DECISION_UNAVAILABLE")
    public = decision.public_state
    state = decision.marvel_state
    history = decision.history
    session = decision.action_session
    public.validate()
    state.validate()
    _text(decision.capture_id, "CONFIRMED_DECISION_UNAVAILABLE")
    _int(decision.frame_sequence, 0, 2**63 - 1, "CONFIRMED_DECISION_UNAVAILABLE")
    _text(public.identity.session_id, "CONFIRMED_DECISION_UNAVAILABLE")
    _text(public.identity.game_id, "CONFIRMED_DECISION_UNAVAILABLE")
    history_identity = history.identity
    if session.closed or session.prefix:
        _fail("CONFIRMED_DECISION_UNAVAILABLE")
    if session.phase not in {"LEAD_PLAY", "FOLLOW_PLAY"}:
        _fail("CONFIRMED_DECISION_UNAVAILABLE")
    if not 0 < len(history.steps) <= MAX_CONFIRMED_HISTORY_STEPS:
        _fail("CONFIRMED_DECISION_UNAVAILABLE")
    if (history_identity.artifact_id != artifact_id
            or history_identity.artifact_sha256 != artifact_sha256):
        _fail("CONFIRMED_DECISION_UNAVAILABLE")
    rows = session.legal_rows
    current = (public.current_winning.value.declaration.value
               if public.current_winning.status is KnowledgeStatus.KNOWN else None)
    validate_confirmed_topology(state, current, session.phase, history)
    payload = {
        "schema": CONFIRMED_DECISION_REQUEST_CONTRACT,
        "capture_id": decision.capture_id,
        "frame_sequence": decision.frame_sequence,
        "session_id": public.identity.session_id,
        "game_id": public.identity.game_id,
        "state_version": public.identity.state_version,
        "model": {"artifact_id": artifact_id, "artifact_sha256": artifact_sha256},
        "marvel_state": _marvel_state_wire(state),
        "current_winning_declaration": current.to_dict() if current is not None else None,
        "session": {
            "phase": session.phase,
            "own_hand": [int(card) for card in session.own_hand],
            "prefix": list(session.prefix),
            "token_step": session.token_step, "committed_step": session.committed_step,
            "candidates_sha256": candidate_sequences_sha256(session.candidates),
            "legal_generation": rows.rows[0].generation,
            "legal_row_count": len(rows.rows),
        },
        "history": {
            "identity": _history_identity_wire(history_identity),
            "steps": [[list(row) for row in step.rows] for step in history.steps],
        },
        "cache_request": _cache_request_wire(decision.cache_request),
    }
    payload["decision_identity_sha256"] = decision_identity_sha256(payload)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_PAYLOAD_BYTES:
        _fail("MODEL_INPUT_UNSUPPORTED")
    return payload


def _parse_marvel_state(value: Any) -> MarvelState:
    data = _exact_keys(value, _STATE_KEYS)
    previous = data["previous_result"]
    if previous is not None:
        item = _exact_keys(previous, _PREVIOUS_KEYS)
        previous = PreviousResultFacts(
            tuple(_tuple_ints(item["ranking_by_seat"], 1, 4, 4, unique=True)),
            _int(item["winner_team"], 0, 1),
            item["outcome_class"], _flag(item["tribute_cancelled"]))
    exchange = _exact_keys(data["exchange"], _EXCHANGE_KEYS)
    transfers = []
    if not isinstance(exchange["resolved_transfers"], list) or len(exchange["resolved_transfers"]) > 8:
        _fail()
    for item in exchange["resolved_transfers"]:
        item = _exact_keys(item, _TRANSFER_KEYS)
        transfers.append(ExchangeTransferFacts(
            item["kind"], _int(item["source_seat"], 0, 3),
            _int(item["target_seat"], 0, 3), _int(item["card_id"], 0, 107)))
    obligations = exchange["return_obligations"]
    if not isinstance(obligations, list) or len(obligations) > 4:
        _fail()
    obligations = tuple(
        (lambda pair: (_int(pair[0], 0, 3), _int(pair[1], 0, 3)))(
            _tuple_ints(pair, 0, 3, 2))
        for pair in obligations)
    facts = ExchangeFacts(
        tuple(_tuple_ints(exchange["tribute_donors"], 0, 3, 4, unique=True)),
        tuple(_tuple_ints(exchange["tribute_order"], 0, 3, 4, unique=True)),
        _flag(exchange["anti_tribute"]), obligations, tuple(transfers),
        tuple(_tuple_ints(exchange["self_selection"], 0, 107, 1, unique=True)),
        _flag(exchange["outgoing_return_transfer"]))
    family = data["current_family"]
    if family is not None and family not in FAMILIES:
        _fail()
    state = MarvelState(
        observer_seat=_int(data["observer_seat"], 0, 3),
        active_seat=_int(data["active_seat"], 0, 3),
        leader_seat=_int(data["leader_seat"], 0, 3),
        current_winner_seat=(None if data["current_winner_seat"] is None
                             else _int(data["current_winner_seat"], 0, 3)),
        phase=data["phase"], level=_int(data["level"], 1, 13),
        own_hand=tuple(_tuple_ints(data["own_hand"], 0, 107, 28, unique=True)),
        current_winning_cards=tuple(_tuple_ints(data["current_winning_cards"], 0, 107, 27, unique=True)),
        self_exchange_cards=tuple(_tuple_ints(data["self_exchange_cards"], 0, 107, 1, unique=True)),
        private_prefix=tuple(_tuple_ints(data["private_prefix"], 0, 255, MAX_PREFIX_TOKENS)),
        remaining_counts=tuple(_tuple_ints(data["remaining_counts"], 0, 28, 4)),
        finishing_ranks=tuple(_tuple_ints(data["finishing_ranks"], 0, 4, 4)),
        emptied_seats=tuple(_tuple_ints(data["emptied_seats"], 0, 3, 4, unique=True)),
        consecutive_passes=_int(data["consecutive_passes"], 0, 3),
        current_family=family,
        current_comparison_rank=(None if data["current_comparison_rank"] is None
                                 else _int(data["current_comparison_rank"], 3, 17)),
        current_winning_size=_int(data["current_winning_size"], 0, 10),
        previous_result=previous, exchange=facts,
        token_step=_int(data["token_step"], 0, 8191),
        committed_step=_int(data["committed_step"], 0, 8191),
        history_complete=_flag(data["history_complete"]))
    state.validate()
    return state


def _parse_history(value: Any) -> tuple[ExactHistoryJournal, HistoryIdentity, dict[str, Any]]:
    data = _exact_keys(value, _HISTORY_KEYS)
    wire_identity = _exact_keys(data["identity"], _IDENTITY_KEYS)
    steps_wire = data["steps"]
    if not isinstance(steps_wire, list) or not 0 < len(steps_wire) <= MAX_CONFIRMED_HISTORY_STEPS:
        _fail()
    steps = []
    for step in steps_wire:
        if not isinstance(step, list) or len(step) != OBSERVER_COUNT:
            _fail()
        rows = []
        for row in step:
            if (not isinstance(row, list) or len(row) != EVENT_WIDTH
                    or any(type(token) is not int or not 0 <= token < 320 for token in row)):
                _fail()
            rows.append(tuple(row))
        steps.append(HistoryStep(tuple(rows)))
    journal = ExactHistoryJournal(
        session_id=_text(wire_identity["session_id"]),
        game_id=_text(wire_identity["game_id"]),
        state_id=_text(wire_identity["state_id"]),
        artifact_id=_text(wire_identity["artifact_id"]),
        artifact_sha256=_hex64(wire_identity["artifact_sha256"]),
        steps=tuple(steps),
        branch_id=_text(wire_identity["branch_id"]))
    identity = journal.identity
    if _history_identity_wire(identity) != wire_identity:
        _fail()
    if (wire_identity["session_id"] != journal.session_id
            or wire_identity["game_id"] != journal.game_id):
        _fail()
    return journal, identity, wire_identity


def _parse_cache_request(value: Any, identity: HistoryIdentity,
                         identity_wire: Mapping[str, Any]) -> CacheRequestIdentity:
    data = _exact_keys(value, _CACHE_KEYS)
    if data["history"] != dict(identity_wire):
        _fail()
    result = CacheRequestIdentity(
        request_id=_hex64(data["request_id"]),
        lifecycle_id=_text(data["lifecycle_id"]),
        request_key=_text(data["request_key"]),
        sequence=_int(data["sequence"], 1, 2**31 - 1),
        generation=_int(data["generation"], 0, 2**31 - 1),
        history=identity)
    result.validate()
    return result


@dataclass(frozen=True, slots=True)
class ParsedConfirmedDecision:
    """Strictly parsed, self-consistent confirmed decision for the provider path."""

    capture_id: str
    frame_sequence: int
    session_id: str
    game_id: str
    state_version: int
    artifact_id: str
    artifact_sha256: str
    marvel_state: MarvelState
    current_declaration: PublicDeclaration | None
    phase: str
    own_hand: tuple[int, ...]
    prefix: tuple[int, ...]
    token_step: int
    committed_step: int
    candidates_sha256: str
    legal_generation: str
    legal_row_count: int
    history: ExactHistoryJournal
    history_identity: HistoryIdentity
    cache_request: CacheRequestIdentity
    decision_identity_sha256: str

    @property
    def level(self) -> int:
        return self.marvel_state.level

    @property
    def observer_seat(self) -> int:
        return self.marvel_state.observer_seat


def _action_declaration(declaration: PublicDeclaration) -> ActionDeclaration:
    """Lossless public declaration -> model-layer declaration round trip."""
    return ActionDeclaration(
        family=declaration.family,
        declared_ranks=tuple(declaration.declared_ranks),
        comparison_rank=declaration.comparison_rank,
        suit=declaration.suit,
        length=declaration.length,
        canonical_cards=tuple(declaration.canonical_cards),
        wild_assignments=tuple(
            WildAssignment(item.physical_card, item.assigned_rank,
                           item.assigned_suit, item.substituted)
            for item in declaration.wild_assignments),
        canonical_action_cards=tuple(declaration.canonical_action_cards),
        protocol_length=declaration.protocol_length)


def validate_confirmed_topology(state: MarvelState, declaration: PublicDeclaration | None,
                                phase: str, journal: ExactHistoryJournal) -> None:
    """Complete phase/seat/current/history truth table; raise MODEL_INPUT_UNSUPPORTED.

    The exact history is replayed first by ``replay_trick_topology`` (decreasing
    rotation, emptied-seat skipping, pass sets, closure, inherited lead, terminal
    condition — differentially tested against ``guance.domain.state.GameState``).
    The claimed state must then equal that replay exactly:

    LEAD_PLAY (a new trick):
      * no current declaration/winner/family/comparison-rank/cards/size;
      * leader_seat == active_seat == observer_seat;
      * consecutive_passes == 0 and the active/leading seat is not emptied;
      * the replay is a closed trick (never an open trick), and when the replay
        derived a leader from the previous trick it must equal the active seat
        (winner if alive, otherwise the inherited partner).

    FOLLOW_PLAY (the current trick):
      * the declaration exists and its family/comparison-rank/cards/length equal
        the state's current-winning context exactly, and its canonical token
        sequence equals the exact history's winning play tokens;
      * winner != active == observer; the declaration and current winner exist;
        active and winner are not emptied;
      * leader_seat/current_winner_seat equal the replay's open-trick leader and
        winner; active_seat equals the replay's expected actor;
      * consecutive_passes equals the replay's trailing pass count (< 3).

    `remaining_counts` must agree with `emptied_seats`, and a terminal exact
    history can never be presented as LEAD_PLAY or FOLLOW_PLAY. Both sides of
    the wire use this function (client pre-check, worker re-parse) and the
    recommendation entry re-runs it before any model work.
    """
    emptied = {seat for seat, count in enumerate(state.remaining_counts) if count == 0}
    if set(state.emptied_seats) != emptied:
        _fail("MODEL_INPUT_UNSUPPORTED")
    if state.active_seat in emptied:
        _fail("MODEL_INPUT_UNSUPPORTED")
    replay = replay_trick_topology(journal.steps, remaining_counts=state.remaining_counts)
    if replay.terminal:
        _fail("MODEL_INPUT_UNSUPPORTED")
    if phase == "LEAD_PLAY":
        if (declaration is not None or state.current_winner_seat is not None
                or state.current_family is not None or state.current_comparison_rank is not None
                or state.current_winning_cards or state.current_winning_size != 0
                or state.leader_seat != state.active_seat
                or state.active_seat != state.observer_seat
                or state.consecutive_passes != 0):
            _fail("MODEL_INPUT_UNSUPPORTED")
        if replay.trick_open:
            _fail("MODEL_INPUT_UNSUPPORTED")
        if replay.expected_leader is not None and replay.expected_leader != state.active_seat:
            _fail("MODEL_INPUT_UNSUPPORTED")
        return
    if phase != "FOLLOW_PLAY":
        _fail("MODEL_INPUT_UNSUPPORTED")
    if (declaration is None or state.current_winner_seat is None
            or state.current_winner_seat == state.active_seat
            or state.current_winner_seat in emptied
            or state.active_seat != state.observer_seat):
        _fail("MODEL_INPUT_UNSUPPORTED")
    expected_cards = tuple(wire_card_to_guandan_id(card)
                          for card in declaration.canonical_action_cards)
    if (declaration.family != state.current_family
            or declaration.comparison_rank != state.current_comparison_rank
            or declaration.length != state.current_winning_size
            or len(state.current_winning_cards) != declaration.length
            or set(state.current_winning_cards) != set(expected_cards)):
        _fail("MODEL_INPUT_UNSUPPORTED")
    if not replay.trick_open or replay.current_winner is None:
        _fail("MODEL_INPUT_UNSUPPORTED")
    if replay.expected_actor != state.active_seat:
        _fail("MODEL_INPUT_UNSUPPORTED")
    if replay.current_leader != state.leader_seat:
        _fail("MODEL_INPUT_UNSUPPORTED")
    if replay.current_winner != state.current_winner_seat:
        _fail("MODEL_INPUT_UNSUPPORTED")
    if replay.trailing_passes != state.consecutive_passes or state.consecutive_passes >= 3:
        _fail("MODEL_INPUT_UNSUPPORTED")
    try:
        tokens = declaration_tokens(_action_declaration(declaration), state.level)
    except Exception:
        _fail("MODEL_INPUT_UNSUPPORTED")
    if tokens != replay.winner_tokens:
        _fail("MODEL_INPUT_UNSUPPORTED")


def _parse_confirmed_decision(payload: Mapping[str, Any]) -> ParsedConfirmedDecision:
    data = _exact_keys(payload if isinstance(payload, dict) else dict(payload or {}), TOP_KEYS,
                       "MODEL_INPUT_UNSUPPORTED")
    if data["schema"] != CONFIRMED_DECISION_REQUEST_CONTRACT:
        _fail("MODEL_INPUT_UNSUPPORTED")
    if len(json.dumps(data, ensure_ascii=False, separators=(",", ":"))) > _MAX_PAYLOAD_BYTES:
        _fail("MODEL_INPUT_UNSUPPORTED")
    capture_id = _text(data["capture_id"])
    frame_sequence = _int(data["frame_sequence"], 0, 2**63 - 1)
    session_id = _text(data["session_id"])
    game_id = _text(data["game_id"])
    state_version = _int(data["state_version"], 0, 2**63 - 1)
    model = _exact_keys(data["model"], _MODEL_KEYS)
    artifact_id = _text(model["artifact_id"])
    artifact_sha256 = _hex64(model["artifact_sha256"])
    state = _parse_marvel_state(data["marvel_state"])
    declaration_wire = data["current_winning_declaration"]
    declaration = None
    if declaration_wire is not None:
        declaration = PublicDeclaration.from_dict(declaration_wire)
        declaration.validate_for_level(state.level)
    session = _exact_keys(data["session"], _SESSION_KEYS)
    phase = session["phase"]
    if phase not in {"LEAD_PLAY", "FOLLOW_PLAY"}:
        _fail("MODEL_INPUT_UNSUPPORTED")
    own_hand = tuple(_tuple_ints(session["own_hand"], 0, 107, 28, unique=True))
    prefix = tuple(_tuple_ints(session["prefix"], 0, 255, MAX_PREFIX_TOKENS))
    if prefix:
        _fail("MODEL_INPUT_UNSUPPORTED")
    token_step = _int(session["token_step"], 0, 8191)
    committed_step = _int(session["committed_step"], 0, 8191)
    candidates_sha256 = _hex64(session["candidates_sha256"])
    legal_generation = _hex64(session["legal_generation"])
    legal_row_count = _int(session["legal_row_count"], 1, MAX_ACTION_ROWS)
    if (state.observer_seat != state.active_seat
            or state.phase != phase
            or tuple(state.own_hand) != own_hand
            or state.token_step != token_step or state.committed_step != committed_step):
        _fail()
    journal, identity, identity_wire = _parse_history(data["history"])
    validate_confirmed_topology(state, declaration, phase, journal)
    if (identity.session_id != session_id or identity.game_id != game_id
            or identity.artifact_id != artifact_id
            or identity.artifact_sha256 != artifact_sha256
            or identity.token_step != token_step or identity.committed_step != committed_step):
        _fail()
    cache_request = _parse_cache_request(data["cache_request"], identity, identity_wire)
    if decision_identity_sha256(data) != data["decision_identity_sha256"]:
        _fail()
    _hex64(data["decision_identity_sha256"])
    return ParsedConfirmedDecision(
        capture_id=capture_id, frame_sequence=frame_sequence,
        session_id=session_id, game_id=game_id, state_version=state_version,
        artifact_id=artifact_id, artifact_sha256=artifact_sha256,
        marvel_state=state, current_declaration=declaration, phase=phase,
        own_hand=own_hand, prefix=prefix, token_step=token_step,
        committed_step=committed_step, candidates_sha256=candidates_sha256,
        legal_generation=legal_generation, legal_row_count=legal_row_count,
        history=journal, history_identity=identity, cache_request=cache_request,
        decision_identity_sha256=data["decision_identity_sha256"])


def parse_confirmed_decision(payload: Mapping[str, Any]) -> ParsedConfirmedDecision:
    """Strict public entry point; never leaks raw protocol exceptions."""
    try:
        return _parse_confirmed_decision(payload)
    except ModelBackendError:
        raise
    except Exception:
        raise ModelBackendError("MODEL_INPUT_INVALID") from None


__all__ = [
    "MAX_CONFIRMED_HISTORY_STEPS", "ParsedConfirmedDecision", "TOP_KEYS",
    "candidate_sequences_sha256", "decision_identity_sha256", "parse_confirmed_decision",
    "project_confirmed_decision",
]
