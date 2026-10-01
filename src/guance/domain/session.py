"""Application-facing deterministic session aggregate."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from guance.contracts import EngineUpdate, SourceFrame
from guance.infrastructure.protocol.live import GAME_HOST, parse_aggregate
from .events import (ActionEvent, DealEvent, InformationalEvent, OwnSeatCandidateEvent,
                      SeatRelation, SettlementEvent, SourceMeta, StartEvent)
from .state import GameState, SeatConfirmationConflict, StateError


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

    def confirm_my_seat_relative(
        self, relation: SeatRelation | str, *, captured_at: float | None = None,
    ) -> list[EngineUpdate]:
        """Confirm the current game's seat from the first-seat relationship.

        The caller supplies ``self``, ``left``, ``partner`` or ``right`` as the
        relationship of the observed first seat to the observer. Numeric seat
        identifiers are intentionally not part of this API. Confirmation is
        accepted only before the first public action and never replaces an
        already inferred seat.
        """
        if self._paused_reason:
            return [EngineUpdate(kind="error", message=self._paused_reason,
                                 error_code="engine_paused")]
        if self._state is None:
            return [EngineUpdate(kind="error",
                                 message="manual seat confirmation requires a current game",
                                 error_code="manual_seat_no_game")]
        try:
            self._state.confirm_my_seat_relative(relation)
        except SeatConfirmationConflict as exc:
            return self._pause(str(exc), "manual_seat_conflict")
        except StateError as exc:
            return [EngineUpdate(kind="error", message=str(exc),
                                 error_code="manual_seat_invalid")]

        when = 0.0 if captured_at is None else float(captured_at)
        updates = [self._state_update("manual-seat-confirmation",
                                      max(self._last_sequence, 0), when)]
        observation = self._state.observation(self.session_id, when)
        # Only make a newly manual-confirmed state immediately recommendation
        # eligible when the observed turn is ours. The table snapshot remains
        # available in either case.
        if (observation is not None
                and observation.current_seat == observation.my_seat):
            self._latest_observation = observation
            updates.append(EngineUpdate(
                kind="observation", observation=observation,
                data={"state_version": observation.state_version}))
        else:
            self._latest_observation = None
        return updates

    def feed(self, frame: SourceFrame) -> list[EngineUpdate]:
        if self._paused_reason:
            return [EngineUpdate(kind="error", message=self._paused_reason,
                                 error_code="engine_paused")]
        if frame.host and frame.host != GAME_HOST:
            return []
        if frame.connection_id and self._connection_id and frame.connection_id != self._connection_id:
            # A terminal game is a safe boundary: accept a new transport, but
            # never append its events to the completed game.  A live game must
            # remain fail-closed until the caller explicitly resets the engine.
            if self._state is None or self._state.phase in {"ended", "settled"}:
                # Drop the terminal aggregate before accepting the new
                # transport.  A delayed action/settlement from the old socket
                # must not be applied to it; the next DealEvent establishes the
                # new game boundary.
                self._state = None
                self._latest_observation = None
                self._connection_id = frame.connection_id
                self._last_sequence = -1
            else:
                return self._pause("capture connection changed; reconnect requires a fresh game")
        elif frame.connection_id:
            self._connection_id = frame.connection_id
        if frame.sequence and self._last_sequence >= 0 and frame.sequence <= self._last_sequence:
            return self._pause("capture frame sequence repeated or went backwards")
        self._last_sequence = max(self._last_sequence, frame.sequence)

        # Both transport directions are recorded for offline forensics, but only
        # server frames are authoritative game input.  A missing/unknown
        # direction must fail closed instead of silently becoming server data.
        if frame.direction == "client":
            return []
        if frame.direction != "server":
            return self._pause(
                "capture frame direction is unknown; refusing to parse",
                "frame_direction_unknown",
            )
        try:
            events = parse_aggregate(
                frame.payload,
                SourceMeta(received_at=frame.received_at, sequence=frame.sequence,
                           connection_id=frame.connection_id, message_index=frame.sequence),
            )
        except (ValueError, TypeError) as exc:
            return self._pause(f"protocol frame rejected: {exc}", "protocol_rejected")
        updates: list[EngineUpdate] = []
        for event in events:
            updates.extend(self._apply_event(event, frame.received_at, frame.sequence))
            if self._paused_reason:
                break
        return updates

    def _apply_event(self, event: object, captured_at: float, source_sequence: int) -> list[EngineUpdate]:
        if isinstance(event, InformationalEvent):
            if self._state and self._state.phase == "active" and event.command == 3010:
                return self._pause("reconnect or snapshot command received during active game")
            if self._state is not None and event.command in {3002, 3003}:
                self._state.apply_informational(event)
                # Evidence is display-only. Do not emit an observation or
                # advance state_version/model eligibility for this update.
                return [self._state_update("information", source_sequence, captured_at)]
            # Known keepalive/status commands are safe to ignore. Unknown
            # commands never reach this branch: the protocol parser rejects
            # them.
            return []
        if isinstance(event, OwnSeatCandidateEvent):
            if self._state is None:
                return []
            try:
                changed = self._state.apply_own_seat_candidate(event)
            except SeatConfirmationConflict as exc:
                return self._pause(str(exc), "seat_evidence_conflict")
            except StateError as exc:
                return self._pause(str(exc), "state_invalid")
            if not changed:
                return []
            updates = [self._state_update("seat-evidence", source_sequence, captured_at)]
            observation = self._state.observation(self.session_id, captured_at)
            if observation is not None:
                self._latest_observation = observation
                updates.append(EngineUpdate(kind="observation", observation=observation,
                                             data={"state_version": observation.state_version}))
            return updates
        if isinstance(event, DealEvent):
            # A deal after ended/settled is the only authoritative boundary for
            # a new game.  Build the replacement transactionally so an invalid
            # deal cannot destroy the previous terminal state.
            if self._state is None or self._state.phase in {"ended", "settled"}:
                candidate = GameState(self._new_game_id())
                try:
                    candidate.apply_deal(event)
                except StateError as exc:
                    return self._pause(str(exc), "state_invalid")
                self._state = candidate
                self._latest_observation = None
            else:
                try:
                    self._state.apply_deal(event)
                except StateError as exc:
                    return self._pause(str(exc), "state_invalid")
            return [self._state_update("deal", source_sequence, captured_at)]
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
        except SeatConfirmationConflict as exc:
            return self._pause(str(exc), "manual_seat_conflict")
        except StateError as exc:
            return self._pause(str(exc), "state_invalid")
        updates = [self._state_update(kind, source_sequence, captured_at)]
        # An active state without a confirmed observer seat is deliberately
        # not exposed as a model observation.  The UI still receives the state
        # update (with my_seat=-1) and can show that seat confirmation is in
        # progress; no recommendation can be submitted from an ambiguous seat.
        observation = self._state.observation(self.session_id, captured_at)
        if observation is not None:
            self._latest_observation = observation
            updates.append(EngineUpdate(kind="observation", observation=observation,
                                        data={"state_version": observation.state_version}))
        else:
            # Terminal transitions must invalidate the last active observation
            # immediately; otherwise consumers can keep recommending from the
            # previous game while the aggregate is already ended/settled.
            self._latest_observation = None
        return updates

    def _new_game_id(self) -> str:
        self._game_counter += 1
        return f"game-{self._game_counter:04d}"

    def _state_update(
        self, reason: str, source_sequence: int, captured_at: float,
    ) -> EngineUpdate:
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
                "seat_confirmation_source": snapshot.seat_confirmation_source,
                "current_seat": snapshot.current_seat,
                "remaining": list(snapshot.remaining),
                "history_length": len(snapshot.history),
                "free_lead": snapshot.free_lead,
                "trick": snapshot.trick,
                "source_sequence": source_sequence,
                "table": self._state.table_snapshot(self.session_id, captured_at),
            })

    def _pause(self, message: str, code: str = "capture_paused") -> list[EngineUpdate]:
        self._paused_reason = message
        self._latest_observation = None
        return [EngineUpdate(kind="error", message=message, error_code=code)]
