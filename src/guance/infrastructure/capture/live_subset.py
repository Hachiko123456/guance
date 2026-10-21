from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable

from guance.domain.events import (
    ActionEvent, DealEvent, DeclaredActionCommitted, ExchangeInitializationEvent,
    ExchangeResolvedObserved, ExchangeSummaryEvent, InformationalEvent,
    OwnExchangeSelectionObserved, PhaseObserved, PublicResultObserved,
    ReturnSelectionEvent, SettlementEvent, StartEvent, TributeSelectionEvent,
    TributeSummaryEvent,
)
from guance.domain.session import GameScopedUnavailable
from guance.domain.state import GameState, source_evidence
from guance.infrastructure.capture.canonical_capture import (
    ConfirmedActionTransition, ConfirmedDecisionInput,
)
from guance.infrastructure.model.marvel_v2.action_decoder import declaration_tokens
from guance.infrastructure.model.marvel_v2.action_protocol import ActionProtocolSession
from guance.infrastructure.model.marvel_v2.cache_lifecycle import AbstractCacheLifecycle
from guance.infrastructure.model.marvel_v2.contracts import (
    ActionDeclaration, CARD_TOKEN_BASE, COMMIT_TOKEN, ExchangeFacts,
    ExchangeTransferFacts, MarvelEvent, MarvelState, PASS_TOKEN, PreviousResultFacts,
)
from guance.infrastructure.model.marvel_v2.history import ExactHistoryJournal
from guance.infrastructure.model.marvel_v2.lifecycle_contracts import CacheRequestIdentity
from guance.infrastructure.model.marvel_v2.live_adapter import MarvelV2ConfirmedDecisionAdapter
from guance.infrastructure.model.marvel_v2.rules_oracle import (
    ObservedContinuousGameChain, RulesOracleError, VerifiedGuandanRulesOracle,
)
from guance.public_state_contract import (
    CanonicalGuandanPublicState, CurrentWinning, EvidenceRef, ExchangeState,
    ExchangeTransfer, HistoryEvidence, Knowledge, KnowledgeStatus,
    PreviousResultSummary, PublicDeclaration, wire_card_to_guandan_id,
)

SUBSET_IDENTITY = "MarvelV2NormalExchangeLiveSubset.v1"
PROTOCOL_IDENTITY = "TencentDaguandanObservedProtocol.v20261014"
NORMAL_EXCHANGE_IDENTITY = "NormalExchangeObserved.v1"
# Capture-side cache binding only (never a model load-path pin): the normal-exchange
# subset was calibrated against the historical iter000100 evaluation artifact.
# Phase 3 will source this identity from the explicitly selected bundle.
_ARTIFACT_ID = "iter000100"
_LEGACY_CACHE_ARTIFACT_SHA256 = "44b8a41371965279c7ce8f3e592162689e9a1b32cb4a07147f2a8ea58ff89696"
_ALLOWED_DEAL_SHAPES = {
    "1:0,2:2,3:0,4:0,5:2,6:2",
    "1:0,2:2,4:0,5:2,6:2",
}
_ALLOWED_SETTLEMENT_SHAPES = {
    "2:0,3:2,8:0,9:0,10:2,11:2,12:2,13:2,13:2,13:2,13:2,15:2,16:0,17:2,19:2,21:2,21:2,21:2,21:2,22:2,22:2,22:2,22:2",
    "1:0,2:0,3:2,8:0,9:0,10:2,11:2,12:2,13:2,13:2,13:2,13:2,15:2,16:0,17:2,19:2,21:2,21:2,21:2,21:2,22:2,22:2,22:2,22:2",
}


class NormalExchangeLiveSubsetProvider:
    """Strict experimental semantic/history/decision provider for the normal subset."""

    identity = SUBSET_IDENTITY
    protocol_identity = PROTOCOL_IDENTITY
    exchange_identity = NORMAL_EXCHANGE_IDENTITY

    def __init__(self, rules_source_root: str | Path | None = None) -> None:
        self._rules_source_root = None if not rules_source_root else Path(rules_source_root).resolve()
        self._oracle: VerifiedGuandanRulesOracle | None = None
        self._capture_id = ""
        self._runtime_session_id = ""
        self._global_request_sequence = 0
        self._decisions: list[ConfirmedDecisionInput] = []
        self._model_adapter = MarvelV2ConfirmedDecisionAdapter()
        self._model_adapter_eligible_count = 0
        self._artifact_identity_provider: Callable[[], tuple[str, str] | None] | None = None
        self._cache_identity: tuple[str, str] | None = None
        self.reset_session()

    def reset_session(self) -> None:
        self._last_settlement: tuple[str, tuple[int, int, int, int], str, int] | None = None
        self._game_ordinal = 0
        self._session_id = "desktop"
        self._current_invalid = False
        self._eligible = False
        self._candidate_previous: tuple[str, tuple[int, int, int, int], str, int] | None = None
        self._chain: ObservedContinuousGameChain | None = None
        self._stage = "none"
        self._tribute_selections: list[TributeSelectionEvent] = []
        self._tribute_transfers: tuple[ExchangeTransfer, ...] = ()
        self._tribute_order: tuple[int, ...] = ()
        self._return_obligations: tuple[tuple[int, int], ...] = ()
        self._return_selections: list[ReturnSelectionEvent] = []
        self._history: ExactHistoryJournal | None = None
        self._history_binding: tuple[str, str] | None = None
        self._cache: AbstractCacheLifecycle | None = None
        self._cache_game_id: str | None = None
        self._transitions: list[ConfirmedActionTransition] = []
        self._last_decision_key: tuple[object, ...] | None = None
        self._decisions.clear()

    def bind_capture(self, capture_id: str) -> None:
        if not isinstance(capture_id, str) or not capture_id:
            raise ValueError("capture id required")
        self._capture_id = capture_id
        self._global_request_sequence = 0
        self._model_adapter_eligible_count = 0
        self._decisions.clear()
        self._last_decision_key = None

    def bind_runtime_session(self, session_id: str) -> None:
        """Enable recommendation decision production without debug evidence.

        This binding is independent of ``bind_capture``: no raw frame file or
        canonical converter is required for the confirmed-decision producer to
        work, and disabling debug recording must never disable it.
        """
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("runtime session id required")
        self._runtime_session_id = session_id
        self._global_request_sequence = 0
        self._model_adapter_eligible_count = 0
        self._decisions.clear()
        self._last_decision_key = None

    def unbind_runtime_session(self) -> None:
        self._runtime_session_id = ""
        self._decisions.clear()
        self._last_decision_key = None

    @property
    def decision_session_id(self) -> str:
        """Active producer identity: debug capture first, else the runtime session."""
        return self._capture_id or self._runtime_session_id

    def set_model_artifact_identity_provider(
            self, provider: Callable[[], tuple[str, str] | None] | None) -> None:
        """Bind decisions to the explicitly selected model artifact identity.

        The provider is called at decision time and returns the ready MARVEL
        artifact ``(artifact_id, artifact_sha256)`` or ``None``. ``None`` keeps
        the historical fixture identity for capture-journal compatibility; the
        recommendation path rejects such decisions because their identity cannot
        equal the selected model's. Without any provider the fixture identity is
        used (offline acceptance fixtures).
        """
        self._artifact_identity_provider = provider
        self._cache = None
        self._cache_identity = None

    def _resolved_artifact_identity(self) -> tuple[str, str]:
        if self._artifact_identity_provider is None:
            return (_ARTIFACT_ID, _LEGACY_CACHE_ARTIFACT_SHA256)
        return (self._artifact_identity_provider()
                or (_ARTIFACT_ID, _LEGACY_CACHE_ARTIFACT_SHA256))

    def _rebind_history_artifact(self, identity: tuple[str, str]) -> None:
        """Keep the confirmed history artifact binding equal to the selected model."""
        if self._history is None or self._history_binding == identity:
            return
        self._history = replace(self._history, artifact_id=identity[0],
                                artifact_sha256=identity[1])
        self._history_binding = identity
        self._cache = None
        self._cache_identity = None
        self._last_decision_key = None

    def unbind_capture(self) -> None:
        self._capture_id = ""
        self._decisions.clear()
        self._last_decision_key = None

    def drain_decisions(self) -> tuple[ConfirmedDecisionInput, ...]:
        values = tuple(self._decisions)
        self._decisions.clear()
        return values

    @property
    def continuous_chain(self) -> ObservedContinuousGameChain | None:
        return self._chain

    @property
    def model_adapter_eligible_count(self) -> int:
        return self._model_adapter_eligible_count

    def _invalidate(self, code: str, message: str) -> None:
        self._current_invalid = True
        self._eligible = False
        self._history = None
        self._cache = None
        self._last_decision_key = None
        raise GameScopedUnavailable(code, message)

    def _rules(self) -> VerifiedGuandanRulesOracle:
        if self._oracle is not None:
            return self._oracle
        if self._rules_source_root is None:
            self._invalidate("LIVE_RULES_PROVIDER_UNAVAILABLE", "verified Guandan rules unavailable")
        try:
            self._oracle = VerifiedGuandanRulesOracle(self._rules_source_root)
        except RulesOracleError:
            self._invalidate("LIVE_RULES_PROVIDER_UNAVAILABLE", "verified Guandan rules unavailable")
        assert self._oracle is not None
        return self._oracle

    @staticmethod
    def _require_meta(event: object) -> None:
        meta = getattr(event, "meta", None)
        if (meta is None or meta.record_type != 15815 or meta.record_version != 2
                or not meta.connection_id or meta.sequence < 0 or meta.sub_index < 0
                or not meta.group_shape):
            raise GameScopedUnavailable("LIVE_PROTOCOL_VARIANT_UNSUPPORTED",
                                        "strict protocol identity unavailable")

    @staticmethod
    def _previous_summary(previous: tuple[str, tuple[int, int, int, int], str, int],
                          meta) -> PreviousResultSummary:
        game_id, ranks, _connection, _settlement_sequence = previous
        winner = ranks.index(1)
        mate = (winner + 2) % 4
        outcome = {2: "DOUBLE_DOWN", 3: "HEAD_THIRD", 4: "HEAD_LAST"}[ranks[mate]]
        evidence = source_evidence(meta, "semantic.previous_result", "experimental_transport_chain")
        return PreviousResultSummary(
            Knowledge.known(game_id, evidence), ranks, winner % 2, outcome,
            Knowledge.known(False, evidence),
        )

    def _start_game(self, event: DealEvent, old_state: GameState | None) -> None:
        self._game_ordinal += 1
        self._current_invalid = False
        self._eligible = False
        self._candidate_previous = None
        self._chain = None
        self._stage = "none"
        self._tribute_selections.clear()
        self._tribute_transfers = ()
        self._tribute_order = ()
        self._return_obligations = ()
        self._return_selections.clear()
        self._history = None
        self._cache = None
        self._transitions.clear()
        self._last_decision_key = None
        if self._game_ordinal == 1:
            return
        previous = self._last_settlement
        self._last_settlement = None
        if previous is None:
            return
        source_game, _ranks, connection_id, settlement_sequence = previous
        if (event.meta.connection_id != connection_id or event.meta.sequence <= settlement_sequence
                or old_state is not None and old_state.game_id != source_game):
            return
        self._candidate_previous = previous
        self._chain = ObservedContinuousGameChain(
            source_game, f"game-{self._game_ordinal:04d}", settlement_sequence,
            event.meta.sequence, connection_id,
        )

    def adapt_event(self, event: object, state: GameState | None,
                    session_id: str) -> Iterable[object]:
        if isinstance(event, (DealEvent, StartEvent, ActionEvent, SettlementEvent,
                              ExchangeInitializationEvent, TributeSelectionEvent,
                              TributeSummaryEvent, ReturnSelectionEvent, ExchangeSummaryEvent)):
            self._require_meta(event)
        if isinstance(event, DealEvent):
            self._session_id = session_id
            if event.meta.group_shape not in _ALLOWED_DEAL_SHAPES:
                self._invalidate("LIVE_PROTOCOL_VARIANT_UNSUPPORTED", "unsupported deal shape")
            self._start_game(event, state)
            return (event,)
        if isinstance(event, ExchangeInitializationEvent):
            if state is None or state.phase != "waiting_start" or self._stage != "none":
                self._invalidate("LIVE_EXCHANGE_VARIANT_UNSUPPORTED", "exchange initialization out of order")
            self._stage = "initialized"
            if self._candidate_previous is not None:
                self._rules()
                self._eligible = True
            return ()
        if isinstance(event, TributeSelectionEvent):
            if not self._eligible or self._candidate_previous is None:
                self._invalidate("PREVIOUS_RESULT_ASSOCIATION_UNSUPPORTED",
                                 "tribute without a continuous previous result")
            if self._stage not in {"initialized", "tribute"}:
                self._invalidate("LIVE_EXCHANGE_VARIANT_UNSUPPORTED", "tribute sequence out of order")
            donors = self._rules().expected_donors(self._candidate_previous[1])
            if event.donor_seat not in donors or any(x.donor_seat == event.donor_seat
                                                     for x in self._tribute_selections):
                self._invalidate("LIVE_EXCHANGE_VARIANT_UNSUPPORTED", "tribute donor mismatch")
            self._tribute_selections.append(event)
            observed_cards = tuple(item.tribute_card for item in self._tribute_selections)
            if event.tribute_cards and event.tribute_cards != observed_cards:
                self._invalidate("LIVE_EXCHANGE_VARIANT_UNSUPPORTED", "tribute card summary mismatch")
            self._stage = "tribute"
            return ()
        if isinstance(event, TributeSummaryEvent):
            if not self._eligible or self._candidate_previous is None or self._stage != "tribute":
                self._invalidate("LIVE_EXCHANGE_VARIANT_UNSUPPORTED", "tribute summary out of order")
            donors = self._rules().expected_donors(self._candidate_previous[1])
            selected = {(item.donor_seat, item.tribute_card) for item in self._tribute_selections}
            summarized = {(item.donor_seat, item.tribute_card) for item in event.entries}
            if len(self._tribute_selections) != len(donors) or selected != summarized:
                self._invalidate("LIVE_EXCHANGE_VARIANT_UNSUPPORTED", "tribute summary mismatch")
            order = self._rules().tribute_order(selected, wire_level=state.level if state else 0)
            ranks = self._candidate_previous[1]
            recipients = (ranks.index(1), ranks.index(2)) if len(donors) == 2 else (ranks.index(1),)
            expected = {donor: recipient for donor, recipient in zip(order, recipients)}
            actual = {item.donor_seat: item.recipient_seat for item in event.entries}
            if actual != expected or any(source == target for source, target in actual.items()):
                self._invalidate("LIVE_EXCHANGE_VARIANT_UNSUPPORTED", "tribute recipient mismatch")
            final_3012 = self._tribute_selections[-1]
            if final_3012.recipient_seats:
                selected_targets = tuple(actual[item.donor_seat] for item in self._tribute_selections)
                if final_3012.recipient_seats != selected_targets:
                    self._invalidate("LIVE_EXCHANGE_VARIANT_UNSUPPORTED", "tribute target list mismatch")
            self._tribute_order = order
            self._tribute_transfers = tuple(
                ExchangeTransfer("tribute", donor, expected[donor],
                                 next(item.tribute_card for item in self._tribute_selections
                                      if item.donor_seat == donor))
                for donor in order
            )
            self._return_obligations = tuple((transfer.target_seat, transfer.source_seat)
                                             for transfer in self._tribute_transfers)
            self._stage = "summary"
            return ()
        if isinstance(event, ReturnSelectionEvent):
            if not self._eligible or self._stage not in {"summary", "return"}:
                self._invalidate("LIVE_EXCHANGE_VARIANT_UNSUPPORTED", "return sequence out of order")
            pair = (event.source_seat, event.target_seat)
            if pair not in self._return_obligations or any(
                    (item.source_seat, item.target_seat) == pair for item in self._return_selections):
                self._invalidate("LIVE_EXCHANGE_VARIANT_UNSUPPORTED", "return obligation mismatch")
            self._return_selections.append(event)
            self._stage = "return"
            return ()
        if isinstance(event, ExchangeSummaryEvent):
            if (not self._eligible or self._candidate_previous is None or self._stage != "return"
                    or state is None or state.my_seat not in range(4)):
                self._invalidate("LIVE_EXCHANGE_VARIANT_UNSUPPORTED", "exchange summary unavailable")
            returns = tuple(
                ExchangeTransfer("return", item.source_seat, item.target_seat, item.return_card)
                for source, target in self._return_obligations
                for item in self._return_selections
                if (item.source_seat, item.target_seat) == (source, target)
            )
            if len(returns) != len(self._return_obligations):
                self._invalidate("LIVE_EXCHANGE_VARIANT_UNSUPPORTED", "return sequence incomplete")
            direct = {(item.source_seat, item.target_seat, item.return_card)
                      for item in self._return_selections}
            summary = {(item.source_seat, item.target_seat, item.return_card)
                       for item in event.return_entries}
            if direct != summary:
                self._invalidate("LIVE_EXCHANGE_VARIANT_UNSUPPORTED", "return summary mismatch")
            all_transfers = (*self._tribute_transfers, *returns)
            if (len({item.card for item in all_transfers}) != len(all_transfers)
                    or any(item.source_seat == item.target_seat for item in all_transfers)):
                self._invalidate("LIVE_EXCHANGE_VARIANT_UNSUPPORTED", "exchange transfer duplicate")
            source_pairs = {(item.card, item.source_seat) for item in event.card_sources}
            if source_pairs != {(item.card, item.source_seat) for item in all_transfers}:
                self._invalidate("LIVE_EXCHANGE_VARIANT_UNSUPPORTED", "exchange source map mismatch")
            own_hand = set(state.hand)
            own_tribute = next((item for item in self._tribute_transfers
                                if item.source_seat == state.my_seat), None)
            if own_tribute is not None:
                if (own_tribute.card not in own_hand
                        or own_tribute.card not in self._rules().legal_tribute_cards(
                            own_hand, wire_level=state.level)):

                    self._invalidate("LIVE_EXCHANGE_VARIANT_UNSUPPORTED", "own tribute selection invalid")
                own_hand.remove(own_tribute.card)
            for transfer in self._tribute_transfers:
                if transfer.target_seat == state.my_seat:
                    own_hand.add(transfer.card)
            own_return = next((item for item in returns if item.source_seat == state.my_seat), None)
            if own_return is not None and own_return.card not in set(
                    self._rules().legal_return_cards(own_hand, wire_level=state.level)):
                self._invalidate("LIVE_EXCHANGE_VARIANT_UNSUPPORTED", "own return selection invalid")
            previous = self._previous_summary(self._candidate_previous, event.meta)
            semantic: list[object] = [
                PublicResultObserved(previous, event.meta),
                PhaseObserved("TRIBUTE", None, event.meta),
            ]
            if own_tribute is not None:
                semantic.append(OwnExchangeSelectionObserved(
                    state.my_seat, "TRIBUTE", (own_tribute.card,), event.meta))
            semantic.append(ExchangeResolvedObserved(
                self._tribute_transfers, self._rules().expected_donors(self._candidate_previous[1]),
                self._tribute_order, False, self._return_obligations, event.meta,
            ))
            semantic.append(PhaseObserved("RETURN", None, event.meta))
            if own_return is not None:
                semantic.append(OwnExchangeSelectionObserved(
                    state.my_seat, "RETURN", (own_return.card,), event.meta))
            semantic.append(ExchangeResolvedObserved(returns, meta=event.meta))
            self._build_exchange_history(state, all_transfers)
            self._stage = "complete"
            return tuple(semantic)
        if isinstance(event, StartEvent):
            if self._candidate_previous is not None and self._stage != "complete":
                self._invalidate("LIVE_EXCHANGE_VARIANT_UNSUPPORTED",
                                 "ANTI_TRIBUTE_OR_UNKNOWN_EXCHANGE_UNSUPPORTED")
            return (event,)
        if isinstance(event, ActionEvent) and event.kind == "play":
            if state is None:
                self._invalidate("LIVE_STATE_UNAVAILABLE", "play before deal")
            if not self._eligible:
                return (event,)
            current = None
            if state.current_winning_declaration is not None:
                meaning = state.current_winning_declaration
                if meaning.status is KnowledgeStatus.KNOWN:
                    current = meaning.value
                elif self._eligible:
                    self._invalidate("LIVE_DECLARATION_AMBIGUOUS", "current declaration unavailable")
            try:
                declaration = self._rules().validate_observed_play(
                    event.cards, wire_level=state.level, combo_type=int(event.combo_type or 0),
                    current=current,
                )
            except RulesOracleError as exc:
                if self._eligible:
                    self._invalidate(exc.code, "live declaration ambiguous")
                return (event,)
            return (DeclaredActionCommitted(event, declaration),)
        return (event,)

    def _build_exchange_history(self, state: GameState,
                                transfers: tuple[ExchangeTransfer, ...]) -> None:
        events: list[MarvelEvent] = [
            MarvelEvent("RESET", "TRIBUTE", self._tribute_order[0], level=state.level),
        ]
        ordered_tribute = tuple(next(item for item in self._tribute_transfers
                                     if item.source_seat == donor)
                                for donor in self._tribute_order)
        ordered_return = tuple(next(item for item in transfers
                                    if item.kind == "return" and item.source_seat == source
                                    and item.target_seat == target)
                               for source, target in self._return_obligations)
        pending = (*ordered_tribute, *ordered_return)
        facts = tuple(ExchangeTransferFacts(
            item.kind, item.source_seat, item.target_seat, wire_card_to_guandan_id(item.card),
        ) for item in pending)
        for transfer in ordered_tribute:
            token = CARD_TOKEN_BASE + wire_card_to_guandan_id(transfer.card)
            events.append(MarvelEvent("PRIVATE_PREFIX", "TRIBUTE", transfer.source_seat,
                                      tokens=(token,)))
            events.append(MarvelEvent("PRIVATE_EXCHANGE_COMMIT", "TRIBUTE", transfer.source_seat,
                                      tokens=(token, COMMIT_TOKEN)))
        for index, transfer in enumerate(ordered_return):
            token = CARD_TOKEN_BASE + wire_card_to_guandan_id(transfer.card)
            events.append(MarvelEvent("PRIVATE_PREFIX", "RETURN", transfer.source_seat,
                                      tokens=(token,)))
            if index + 1 == len(ordered_return):
                events.append(MarvelEvent("EXCHANGE_RESOLVED", "RETURN", transfer.source_seat,
                                          transfers=facts, last_actor=transfer.source_seat))
            else:
                events.append(MarvelEvent("PRIVATE_EXCHANGE_COMMIT", "RETURN", transfer.source_seat,
                                          tokens=(token, COMMIT_TOKEN)))
        identity = self._resolved_artifact_identity()
        self._history = ExactHistoryJournal.from_events(
            session_id=self._session_id, game_id=state.game_id, state_id="state-pending",
            artifact_id=identity[0], artifact_sha256=identity[1],
            events=tuple(events),
        )
        self._history_binding = identity

    def _exchange_transitions(self, start: StartEvent) -> list[ConfirmedActionTransition]:
        actors = [item.source_seat for item in self._tribute_transfers]
        actors += [source for source, _target in self._return_obligations]
        next_actors = actors[1:] + [start.first_seat]
        evidence = source_evidence(start.meta, "semantic.exchange_transition", "confirmed_sequence")
        return [ConfirmedActionTransition(index + 1, next_actor, None, evidence)
                for index, next_actor in enumerate(next_actors)]

    def observe_applied(self, event: object, state: GameState, session_id: str) -> None:
        if isinstance(event, SettlementEvent):
            if event.meta.group_shape not in _ALLOWED_SETTLEMENT_SHAPES or len(event.remaining_hands) != 4:
                self._current_invalid = True
                self._last_settlement = None
                return
            if state.phase == "settled" and not self._current_invalid:
                self._last_settlement = (
                    state.game_id, tuple(event.ranks_by_seat), event.meta.connection_id,
                    event.meta.sequence,
                )
            return
        if not self._eligible or self._current_invalid:
            return
        if isinstance(event, ExchangeResolvedObserved) and event.transfers and all(
                transfer.kind == "return" for transfer in event.transfers):
            if self._history is None:
                self._invalidate("EXACT_HISTORY_UNAVAILABLE", "exchange history unavailable")
            self._history = replace(self._history, state_id=f"state-{state.state_version}")
        elif isinstance(event, StartEvent):
            if self._history is None:
                self._invalidate("EXACT_HISTORY_UNAVAILABLE", "exchange history unavailable")
            self._transitions = self._exchange_transitions(event)
            self._history = replace(self._history, state_id=f"state-{state.state_version}")
        elif isinstance(event, DeclaredActionCommitted):
            if self._history is None:
                self._invalidate("EXACT_HISTORY_UNAVAILABLE", "action history unavailable")
            declaration = ActionDeclaration.from_domain(event.declaration)
            tokens = declaration_tokens(declaration, state.level)
            phase = state.committed_actions_v2[-1].phase
            for token in tokens[:-1]:
                self._history = self._history.append(MarvelEvent(
                    "PRIVATE_PREFIX", phase, event.action.player, tokens=(token,)))
            self._history = self._history.append(MarvelEvent(
                "PUBLIC_COMMIT", phase, event.action.player, tokens=tokens),
                state_id=f"state-{state.state_version}",
            )
            self._append_transition(event.action, state)
        elif isinstance(event, ActionEvent) and event.kind == "pass":
            if self._history is None:
                self._invalidate("EXACT_HISTORY_UNAVAILABLE", "action history unavailable")
            phase = state.committed_actions_v2[-1].phase
            self._history = self._history.append(MarvelEvent(
                "PUBLIC_COMMIT", phase, event.player, tokens=(PASS_TOKEN,)),
                state_id=f"state-{state.state_version}",
            )
            self._append_transition(event, state)
        self._maybe_decision(state, session_id, getattr(event, "meta", None))

    def _append_transition(self, event: ActionEvent, state: GameState) -> None:
        action = state.committed_actions_v2[-1]
        next_actor: int | str = "terminal" if event.next_player is None else event.next_player
        reset = None if event.kind == "play" else (event.reset_flag if event.reset_flag is not None else 0)
        evidence = source_evidence(event.meta, "semantic.action_transition", "confirmed_action_chain")
        self._transitions.append(ConfirmedActionTransition(action.index, next_actor, reset, evidence))

    @staticmethod
    def _known_exchange_state(base: CanonicalGuandanPublicState,
                              exact_evidence: EvidenceRef) -> ExchangeState:
        selection = base.exchange.self_selection
        if selection.status is not KnowledgeStatus.KNOWN:
            selection = Knowledge.absent(exact_evidence)
        return replace(base.exchange, self_selection=selection)

    def _exact_public_state(self, state: GameState, session_id: str) -> CanonicalGuandanPublicState:
        if self._history is None:
            self._invalidate("EXACT_HISTORY_UNAVAILABLE", "history unavailable")
        base = state.public_state_v2(session_id)
        history_identity = self._history.identity
        observer = base.observer_seat
        assert observer.evidence is not None
        exact = EvidenceRef(
            "semantic.exact_history", observer.evidence.connection_id,
            observer.evidence.sequence, observer.evidence.message_index,
            observer.evidence.sub_index, "confirmed_transport_chain",
        )
        rules = EvidenceRef("trusted.gd_rules", derivation="frozen_source_identity")
        actions = []
        if len(base.history.actions) != len(self._transitions):
            self._invalidate("EXACT_HISTORY_UNAVAILABLE", "action transitions incomplete")
        for action, transition in zip(base.history.actions, self._transitions):
            next_fact = (Knowledge.absent(transition.evidence) if transition.next_actor == "terminal"
                         else Knowledge.known(transition.next_actor, transition.evidence))
            actions.append(replace(action, next_player=next_fact))
        history = HistoryEvidence(
            tuple(actions), len(actions), Knowledge.known(True, exact),
            Knowledge.known(True, exact), Knowledge.known(True, exact),
            Knowledge.known(history_identity.token_step, exact),
            Knowledge.known(history_identity.committed_step, exact),
        )
        result = replace(
            base, ruleset_compatibility=Knowledge.known("rules.GD-RULES-0.1.verified", rules),
            observed_protocol=PROTOCOL_IDENTITY,
            exchange=self._known_exchange_state(base, exact), history=history,
            unknown_reasons=(), unsupported_reasons=(),
        )
        result.validate()
        return CanonicalGuandanPublicState.from_dict(result.to_dict())

    @staticmethod
    def _required_known(fact: Knowledge, code: str):
        if fact.status is not KnowledgeStatus.KNOWN:
            raise GameScopedUnavailable(code, "canonical fact unavailable")
        return fact.value

    def _marvel_state(self, public: CanonicalGuandanPublicState) -> MarvelState:
        observer = int(self._required_known(public.observer_seat, "OBSERVER_IDENTITY_UNAVAILABLE"))
        active = int(self._required_known(public.active_seat, "ACTIVE_SEAT_UNAVAILABLE"))
        leader = int(self._required_known(public.leader_seat, "LEADER_SEAT_UNAVAILABLE"))
        level = int(self._required_known(public.level, "LEVEL_UNAVAILABLE"))
        hand = tuple(wire_card_to_guandan_id(card)
                     for card in self._required_known(public.own_hand, "OWN_HAND_UNAVAILABLE"))
        counts = tuple(self._required_known(public.remaining_counts, "REMAINING_COUNTS_UNAVAILABLE"))
        winner = public.current_winner_seat
        winner_seat = winner.value if winner.status is KnowledgeStatus.KNOWN else None
        winning_cards: tuple[int, ...] = ()
        family = None
        comparison = None
        if public.current_winning.status is KnowledgeStatus.KNOWN:
            current: CurrentWinning = public.current_winning.value
            if current.declaration.status is not KnowledgeStatus.KNOWN:
                self._invalidate("LIVE_DECLARATION_AMBIGUOUS", "winning declaration unavailable")
            declaration: PublicDeclaration = current.declaration.value
            winning_cards = tuple(wire_card_to_guandan_id(card) for card in declaration.canonical_cards)
            family = declaration.family
            comparison = declaration.comparison_rank
        elif public.current_winning.status is not KnowledgeStatus.ABSENT:
            self._invalidate("CURRENT_WINNING_UNAVAILABLE", "winning context unavailable")
        previous = self._required_known(public.previous_result, "PREVIOUS_RESULT_ASSOCIATION_UNSUPPORTED")
        if previous.tribute_cancelled.status is not KnowledgeStatus.KNOWN:
            self._invalidate("PREVIOUS_RESULT_ASSOCIATION_UNSUPPORTED", "tribute status unavailable")
        previous_facts = PreviousResultFacts(previous.ranking_by_seat, previous.winner_team,
                                             previous.outcome_class, previous.tribute_cancelled.value)
        exchange = public.exchange
        transfers = tuple(ExchangeTransferFacts(
            item.kind, item.source_seat, item.target_seat, wire_card_to_guandan_id(item.card),
        ) for item in self._required_known(exchange.resolved_transfers,
                                           "LIVE_EXCHANGE_VARIANT_UNSUPPORTED"))
        selection = (() if exchange.self_selection.status is KnowledgeStatus.ABSENT else
                     tuple(wire_card_to_guandan_id(card) for card in
                           self._required_known(exchange.self_selection,
                                                "LIVE_EXCHANGE_VARIANT_UNSUPPORTED")))
        facts = ExchangeFacts(
            tuple(self._required_known(exchange.tribute_donors, "LIVE_EXCHANGE_VARIANT_UNSUPPORTED")),
            tuple(self._required_known(exchange.tribute_order, "LIVE_EXCHANGE_VARIANT_UNSUPPORTED")),
            bool(self._required_known(exchange.anti_tribute, "LIVE_EXCHANGE_VARIANT_UNSUPPORTED")),
            tuple(tuple(pair) for pair in self._required_known(
                exchange.return_obligations, "LIVE_EXCHANGE_VARIANT_UNSUPPORTED")),
            transfers, selection,
            any(item.kind == "return" and item.source_seat == observer for item in transfers),
        )
        finishing = tuple(fact.value if fact.status is KnowledgeStatus.KNOWN else 0
                          for fact in public.finishing_ranks)
        history = self._history.identity
        result = MarvelState(
            observer, active, leader, winner_seat, str(public.phase.value), level,
            hand, winning_cards, (), (), counts, finishing,
            tuple(self._required_known(public.emptied_seats, "PUBLIC_COUNTERS_UNAVAILABLE")),
            int(self._required_known(public.consecutive_passes, "PUBLIC_COUNTERS_UNAVAILABLE")),
            family, comparison, len(winning_cards), previous_facts, facts,
            history.token_step, history.committed_step, True,
        )
        result.validate()
        return result

    def _maybe_decision(self, state: GameState, session_id: str, meta) -> None:
        decision_id = self.decision_session_id
        if (not decision_id or self._history is None or state.phase != "active"
                or state.my_seat not in range(4) or state.current_seat != state.my_seat
                or meta is None):
            return
        public = self._exact_public_state(state, session_id)
        current = (public.current_winning.value.declaration.value
                   if public.current_winning.status is KnowledgeStatus.KNOWN else None)
        try:
            candidates = self._rules().legal_action_candidates(
                public.own_hand.value, wire_level=state.level,
                phase=str(public.phase.value), current=current,
            )
        except RulesOracleError as exc:
            self._invalidate(exc.code, "legal actions unavailable")
        history_identity = self._history.identity
        action_session = ActionProtocolSession(
            session_id, public.identity.state_version, str(public.phase.value),
            tuple(wire_card_to_guandan_id(card) for card in public.own_hand.value),
            candidates, public, prefix=(), token_step=history_identity.token_step,
            committed_step=history_identity.committed_step,
        )
        marvel = self._marvel_state(public)
        artifact_identity = self._resolved_artifact_identity()
        if self._history_binding is not None and artifact_identity != self._history_binding:
            # Selected model changed: rebind the confirmed history/cache so the
            # decision identity always follows the currently selected artifact.
            self._rebind_history_artifact(artifact_identity)
        if (self._cache is None or self._cache_identity != artifact_identity
                or self._cache_game_id != public.identity.game_id):
            self._cache = AbstractCacheLifecycle(
                lifecycle_id="normal-exchange-live", session_id=session_id,
                game_id=public.identity.game_id, artifact_id=artifact_identity[0],
                artifact_sha256=artifact_identity[1],
                initial_sequence=self._global_request_sequence,
            )
            self._cache_identity = artifact_identity
            self._cache_game_id = public.identity.game_id
            self._last_decision_key = None
        request_key = f"decision-{self._global_request_sequence + 1}"
        request = self._cache.begin_request(self._history, request_key=request_key)
        self._global_request_sequence += 1
        if request.sequence != self._global_request_sequence:
            self._invalidate("CACHE_REQUEST_SEQUENCE_GAP", "cache request sequence mismatch")
        key = (public.identity.game_id, public.identity.state_version,
               history_identity.token_step, history_identity.committed_step)
        if key == self._last_decision_key:
            return
        self._last_decision_key = key
        decision = ConfirmedDecisionInput(
            decision_id, meta.sequence, 1, meta.sequence, public, marvel,
            self._history, action_session, request, tuple(self._transitions),
        )
        self._model_adapter.adapt(decision)
        self._model_adapter_eligible_count += 1
        self._decisions.append(decision)


__all__ = [
    "NORMAL_EXCHANGE_IDENTITY", "NormalExchangeLiveSubsetProvider",
    "PROTOCOL_IDENTITY", "SUBSET_IDENTITY",
]
