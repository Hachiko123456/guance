"""Application-facing deterministic session aggregate."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from itertools import islice

from guance.contracts import EngineUpdate, SourceFrame
from guance.public_state_contract import CanonicalGuandanPublicState
from .events import (ActionEvent, DealEvent, InformationalEvent, OwnSeatCandidateEvent,
                      SeatRelation, SettlementEvent, SourceMeta, StartEvent,
                      DeclaredActionCommitted, ExchangeResolvedObserved,
                      OwnExchangeSelectionObserved, PhaseObserved, PublicResultObserved)
from .state import GameState, SeatConfirmationConflict, StateError


AggregateParser = Callable[[bytes, SourceMeta], Iterable[object]]
EventAdapter = Callable[[object, GameState | None, str], Iterable[object]]
EventObserver = Callable[[object, GameState, str], None]
SessionResetObserver = Callable[[], None]


class GameScopedUnavailable(ValueError):
    """Invalidate only the current game while transport observation continues."""

    def __init__(self, code: str, message: str = "live subset unavailable") -> None:
        self.code = code
        super().__init__(message)


def _empty_aggregate_parser(_payload: bytes, _meta: SourceMeta) -> tuple[object, ...]:
    """Safe compatibility parser used without a protocol adapter."""
    return ()


# Compatibility symbol for tests and embedders that provide a parser by
# monkeypatching. It never imports or invokes infrastructure.
parse_aggregate: AggregateParser = _empty_aggregate_parser


class SessionEngine:
    """Convert captured live frames into safe observations and state updates.

    The engine is intentionally fail-closed. It never advances a game after an
    unknown command, reconnect, missing action, or state invariant failure.
    """

    def __init__(self, session_id: str | None = None, *,
                 aggregate_parser: AggregateParser | None = None,
                 accepted_host: str | None = None,
                 event_adapter: EventAdapter | None = None,
                 event_observer: EventObserver | None = None,
                 reset_observer: SessionResetObserver | None = None) -> None:
        self.session_id = session_id or "session"
        self._aggregate_parser = aggregate_parser
        self._accepted_host = accepted_host
        self._event_adapter = event_adapter
        self._event_observer = event_observer
        self._reset_observer = reset_observer
        self._state: GameState | None = None
        self._game_counter = 0
        self._latest_observation = None
        self._latest_public_state_v2: CanonicalGuandanPublicState | None = None
        self._paused_reason = ""
        self._connection_id = ""
        self._last_sequence = -1
        self._game_invalid_reason = ""

    @property
    def latest_observation(self):
        return self._latest_observation

    @property
    def latest_public_state_v2(self) -> CanonicalGuandanPublicState | None:
        """Latest partial public+self facts; never a model-ready assertion."""
        return self._latest_public_state_v2

    @property
    def paused(self) -> bool:
        return bool(self._paused_reason)

    @property
    def state(self) -> GameState | None:
        return self._state

    def reset(self, reason: str = "") -> None:
        self._state = None
        self._latest_observation = None
        self._latest_public_state_v2 = None
        self._paused_reason = reason
        self._connection_id = ""
        self._last_sequence = -1
        self._game_invalid_reason = ""
        if self._reset_observer is not None:
            self._reset_observer()

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
        if self._accepted_host is not None and frame.host != self._accepted_host:
            return []
        if type(frame.sequence) is not int or not 0 <= frame.sequence < 2**63:
            return self._pause("capture sequence invalid", "frame_sequence_invalid")
        if type(frame.connection_id) is not str:
            return self._pause("capture connection invalid", "frame_connection_invalid")
        if self._last_sequence >= 0 and frame.connection_id != self._connection_id:
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
                self._latest_public_state_v2 = None
                self._connection_id = frame.connection_id
                self._last_sequence = -1
            else:
                return self._pause("capture connection changed; reconnect requires a fresh game")
        else:
            self._connection_id = frame.connection_id
        if self._last_sequence >= 0:
            if frame.sequence <= self._last_sequence:
                return self._pause("capture frame sequence repeated or went backwards", "frame_sequence_repeated")
            if frame.sequence != self._last_sequence + 1:
                return self._pause("capture frame sequence gap", "frame_sequence_gap")
        self._last_sequence = frame.sequence

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
            parser = self._aggregate_parser or parse_aggregate
            # Materialize before mutation: lazy parser failures cannot publish
            # a partially accepted aggregate or echo untrusted exception text.
            events = tuple(islice(parser(
                frame.payload,
                SourceMeta(received_at=frame.received_at, sequence=frame.sequence,
                           connection_id=frame.connection_id, message_index=frame.sequence),
            ), 129))
            if len(events) > 128:
                raise ValueError("aggregate event limit exceeded")
        except (ValueError, TypeError):
            return self._pause("protocol frame rejected", "protocol_rejected")
        updates: list[EngineUpdate] = []
        for event in events:
            if self._game_invalid_reason:
                if not isinstance(event, DealEvent):
                    continue
                self._state = None
                self._latest_observation = None
                self._latest_public_state_v2 = None
                self._game_invalid_reason = ""
            try:
                adapted = (tuple(self._event_adapter(event, self._state, self.session_id))
                           if self._event_adapter is not None else (event,))
            except GameScopedUnavailable as exc:
                updates.extend(self._invalidate_game(str(exc), exc.code))
                continue
            except (ValueError, TypeError):
                updates.extend(self._invalidate_game("live subset unavailable",
                                                     "LIVE_STATE_UNAVAILABLE"))
                continue
            for accepted in adapted:
                updates.extend(self._apply_event(accepted, frame.received_at, frame.sequence))
                if self._paused_reason:
                    # Never return earlier observations as trusted when a later
                    # event invalidates this frame's chain.
                    return [update for update in updates if update.kind == "error"]
                if self._state is not None and self._event_observer is not None:
                    try:
                        self._event_observer(accepted, self._state, self.session_id)
                    except GameScopedUnavailable as exc:
                        updates.extend(self._invalidate_game(str(exc), exc.code))
                        break
                    except (ValueError, TypeError):
                        updates.extend(self._invalidate_game("live subset unavailable",
                                                             "LIVE_STATE_UNAVAILABLE"))
                        break
        return updates

    def _apply_event(self, event: object, captured_at: float, source_sequence: int) -> list[EngineUpdate]:
        try:
            return self._apply_event_checked(event, captured_at, source_sequence)
        except (ValueError, TypeError):
            return self._pause("confirmed state rejected", "state_invalid")

    def _apply_event_checked(self, event: object, captured_at: float,
                             source_sequence: int) -> list[EngineUpdate]:
        if isinstance(event, InformationalEvent):
            # 3010 has only an observed structural contract.  It is not a
            # verified reconnect marker and must not sever a same-connection
            # game chain or mutate state.  Transport connection IDs and
            # sequence continuity remain the authoritative reconnect guards.
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
            elif type(event) is PhaseObserved:
                self._state.apply_phase_observed(event)
                kind = "explicit-phase"
            elif type(event) is PublicResultObserved:
                self._state.apply_previous_result(event)
                kind = "previous-result"
            elif type(event) is DeclaredActionCommitted:
                self._state.apply_declared_action(event)
                kind = "declared-action"
            elif type(event) is ExchangeResolvedObserved:
                self._state.apply_exchange_resolved(event)
                kind = "resolved-exchange"
            elif type(event) is OwnExchangeSelectionObserved:
                self._state.apply_own_exchange_selection(event)
                kind = "self-selection"
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
        self._latest_public_state_v2 = self._state.public_state_v2(self.session_id)
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

    def _invalidate_game(self, message: str, code: str) -> list[EngineUpdate]:
        self._game_invalid_reason = code
        self._latest_observation = None
        self._latest_public_state_v2 = None
        return [EngineUpdate(kind="error", message=message, error_code=code,
                             data={"scope": "game", "recover_on_next_deal": True})]

    def _pause(self, message: str, code: str = "capture_paused") -> list[EngineUpdate]:
        self._paused_reason = message
        self._latest_observation = None
        self._latest_public_state_v2 = None
        return [EngineUpdate(kind="error", message=message, error_code=code)]
