"""Application-facing deterministic session aggregate."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from guance.contracts import EngineUpdate, SourceFrame
from guance.infrastructure.protocol.live import GAME_HOST, parse_aggregate
from .events import ActionEvent, DealEvent, InformationalEvent, SettlementEvent, StartEvent
from .state import GameState, StateError


class SessionEngine:
    """Convert captured live frames into safe observations and state updates.

    The engine is intentionally fail-closed. It never advances a game after an
    unknown command, reconnect, missing action, or state invariant failure.
    """

    def __init__(self, session_id: str | None = None) -> None:
        self.session_id = session_id or "session"
        self._state: GameState | None = None
        self._game_counter = 0
        self._latest_observation = None
        self._paused_reason = ""
        self._connection_id = ""
        self._last_sequence = -1

    @property
    def latest_observation(self):
        return self._latest_observation

    @property
    def paused(self) -> bool:
        return bool(self._paused_reason)

    @property
    def state(self) -> GameState | None:
        return self._state

    def reset(self, reason: str = "") -> None:
        self._state = None
        self._latest_observation = None
        self._paused_reason = reason
        self._connection_id = ""
        self._last_sequence = -1

    def feed(self, frame: SourceFrame) -> list[EngineUpdate]:
        if self._paused_reason:
            return [EngineUpdate(kind="error", message=self._paused_reason,
                                 error_code="engine_paused")]
        if frame.host and frame.host != GAME_HOST:
            return []
        if frame.connection_id and self._connection_id and frame.connection_id != self._connection_id:
            return self._pause("capture connection changed; reconnect requires a fresh game")
        if frame.connection_id:
            self._connection_id = frame.connection_id
        if frame.sequence and self._last_sequence >= 0 and frame.sequence <= self._last_sequence:
            return self._pause("capture frame sequence repeated or went backwards")
        self._last_sequence = max(self._last_sequence, frame.sequence)
        try:
            events = parse_aggregate(frame.payload)
        except (ValueError, TypeError) as exc:
            return self._pause(f"protocol frame rejected: {exc}", "protocol_rejected")
        updates: list[EngineUpdate] = []
        for event in events:
            updates.extend(self._apply_event(event, frame.received_at))
            if self._paused_reason:
                break
        return updates

    def _apply_event(self, event: object, captured_at: float) -> list[EngineUpdate]:
        if isinstance(event, InformationalEvent):
            if self._state and self._state.phase == "active" and event.command == 3010:
                return self._pause("reconnect or snapshot command received during active game")
            # Known keepalive/status commands are safe to ignore. Unknown commands
            # never reach this branch: the protocol parser rejects them.
            return []
        if isinstance(event, DealEvent):
            if self._state is None:
                self._game_counter += 1
                self._state = GameState(self._new_game_id())
            try:
                self._state.apply_deal(event)
            except StateError as exc:
                return self._pause(str(exc), "state_invalid")
            return [self._state_update("deal")]
        if self._state is None:
            return self._pause("game event received before deal", "state_out_of_order")
        try:
            if isinstance(event, StartEvent):
                self._state.apply_start(event)
                kind = "start"
            elif isinstance(event, ActionEvent):
                self._state.apply_action(event)
                kind = "action"
            elif isinstance(event, SettlementEvent):
                self._state.apply_settlement(event)
                kind = "settlement"
            else:
                return self._pause("unsupported protocol event", "event_unsupported")
        except StateError as exc:
            return self._pause(str(exc), "state_invalid")
        updates = [self._state_update(kind)]
        observation = self._state.observation(self.session_id, captured_at)
        if observation is not None:
            self._latest_observation = observation
            updates.append(EngineUpdate(kind="observation", observation=observation,
                                        data={"state_version": observation.state_version}))
        return updates

    def _new_game_id(self) -> str:
        suffix = self._connection_id or str(self._game_counter)
        return f"game-{suffix}"

    def _state_update(self, reason: str) -> EngineUpdate:
        assert self._state is not None
        snapshot = self._state.snapshot()
        return EngineUpdate(
            kind="state", message=reason,
            data={
                "game_id": snapshot.game_id,
                "phase": snapshot.phase,
                "state_version": snapshot.state_version,
                "level": snapshot.level,
                "my_seat": snapshot.my_seat,
                "first_seat": snapshot.first_seat,
                "current_seat": snapshot.current_seat,
                "remaining": list(snapshot.remaining),
                "history_length": len(snapshot.history),
                "free_lead": snapshot.free_lead,
                "trick": snapshot.trick,
            })

    def _pause(self, message: str, code: str = "capture_paused") -> list[EngineUpdate]:
        self._paused_reason = message
        self._latest_observation = None
        return [EngineUpdate(kind="error", message=message, error_code=code)]
