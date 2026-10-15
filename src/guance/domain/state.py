"""Pure, transactional state transitions for the observed classic-room rules.

A GameState is mutated only by confirmed events, never by recommendations.
Opponent hands are not state. Settlement hands are checked and immediately
forgotten; every prior observation remains an immutable visible-information view.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import cast

from guance.contracts import GameObservation, PublicAction
from guance.public_state_contract import (
    CanonicalGuandanPublicState, CommittedActionV2, CurrentWinning, EvidenceRef,
    ExchangeState, HistoryEvidence, Knowledge, KnowledgeStatus, PreviousResultSummary,
    PublicDeclaration, PublicStateContractError, StateIdentity, unsupported_exchange,
)
from guance.domain.card_counter import count_visible_cards
from guance.domain.cards import card_parts, validate_deck
from guance.domain.events import (
    UNCONFIRMED_SEAT, ActionEvent, DealEvent, InformationalEvent,
    OwnSeatCandidateEvent, SettlementEvent, SeatRelation, SourceMeta, StartEvent,
    DeclaredActionCommitted, ExchangeResolvedObserved, OwnExchangeSelectionObserved,
    PhaseObserved, PublicResultObserved,
)


class StateError(ValueError):
    """A confirmed event cannot be applied without guessing or corrupting state."""


class SeatConfirmationConflict(StateError):
    """A manual seat choice conflicts with observed public cards."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise StateError(message)


def checked_cards(cards: tuple[int, ...]) -> None:
    require(len(cards) == len(set(cards)), "duplicate physical card")
    for code in cards:
        card_parts(code)


def source_evidence(meta: SourceMeta, source: str, derivation: str = "") -> EvidenceRef:
    """Whitelist local provenance; never expose arbitrary transport identifiers.

    The digest is only a local connection correlation token, not a server game
    identity. Local sequences do not establish server/history completeness.
    """
    require(type(meta) is SourceMeta, "invalid source metadata")
    require(type(meta.connection_id) is str, "invalid connection metadata")
    require(type(meta.sequence) is int and 0 <= meta.sequence < 2**63,
            "invalid source sequence")
    for index in (meta.message_index, meta.sub_index):
        require(type(index) is int and -1 <= index < 2**63, "invalid source index")
    connection = ("local-" + sha256(meta.connection_id.encode("utf-8")).hexdigest()[:24]
                  if meta.connection_id else "")
    return EvidenceRef(source, connection, meta.sequence,
                       meta.message_index if meta.message_index >= 0 else None,
                       meta.sub_index if meta.sub_index >= 0 else None, derivation)


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    game_id: str
    level: int
    my_seat: int
    first_seat: int | None
    current_seat: int | None
    hand: tuple[int, ...]
    remaining: tuple[int, int, int, int]
    history: tuple[PublicAction, ...]
    lead_action: PublicAction | None
    free_lead: bool
    trick: int
    phase: str
    state_version: int
    seat_confirmation_source: str | None


class GameState:
    """Single-game aggregate; apply methods are atomic on validation failure.

    Classic rules observed in both samples: seats act in decreasing order,
    opposite seats are partners, and a round is inherited by a finished leader's
    partner after all remaining opponents pass. Unobserved variants fail closed.
    """

    MAX_ACTIONS = 4096

    def __init__(self, game_id: str) -> None:
        self.game_id = game_id
        self.phase = "waiting_deal"
        self.level = 0
        self.my_seat = UNCONFIRMED_SEAT
        self.seat_confirmation_source: str | None = None
        self.first_seat: int | None = None
        self.current_seat: int | None = None
        self._first_seat_hint: int | None = None
        self._deal_connection_id = ""
        self._deal_sequence = 0
        self.hand: set[int] = set()
        self.remaining = [0, 0, 0, 0]
        self.history: list[PublicAction] = []
        self.lead_action: PublicAction | None = None
        self.free_lead = True
        self.trick = 0
        self.state_version = 0
        self._passed: set[int] = set()
        self._played_public: set[int] = set()
        self._played_by_seat: list[list[int]] = [[], [], [], []]
        self._finished: list[int] = []
        self._finish_rank_by_seat: list[int | None] = [None, None, None, None]
        self._initial_hand: tuple[int, ...] = ()
        self._reported_seat_snapshot: tuple[int, ...] | None = None
        self._reported_values: tuple[float, ...] | None = None
        self._reported_flag: int | None = None
        # V2 is a facts projection of this aggregate, not a second rule engine.
        self.public_state_revision = 0
        self.leader_seat: int | None = None
        self.consecutive_passes = 0
        self._v2_evidence: dict[str, EvidenceRef] = {}
        self._v2_history: list[CommittedActionV2] = []
        self._v2_winning: CurrentWinning | None = None
        self._v2_phase = Knowledge.unknown("phase_not_observed")
        self._v2_previous_result = Knowledge.unknown("previous_result_not_associated")
        self._v2_exchange = unsupported_exchange()
        self._exchange_active_seat: int | None = None
        # Publicly resolved location only. This never tracks hidden hands.
        self._exchange_card_locations: dict[int, int] = {}

    def clone(self) -> GameState:
        """Bounded cheap transaction: immutable actions are shared, not deep-copied."""
        other = GameState(self.game_id)
        other.__dict__.update(self.__dict__)
        other.hand = self.hand.copy()
        other.remaining = self.remaining.copy()
        other.history = self.history.copy()
        other._passed = self._passed.copy()
        other._played_public = self._played_public.copy()
        other._played_by_seat = [cards.copy() for cards in self._played_by_seat]
        other._finished = self._finished.copy()
        other._finish_rank_by_seat = self._finish_rank_by_seat.copy()
        other._v2_evidence = self._v2_evidence.copy()
        other._v2_history = self._v2_history.copy()
        other._exchange_card_locations = self._exchange_card_locations.copy()
        return other

    def _touch_v2(self, evidence: EvidenceRef, *fields: str) -> None:
        self.public_state_revision += 1
        for name in fields:
            self._v2_evidence[name] = evidence

    def _play_phase_v2(self, evidence: EvidenceRef) -> None:
        if self.phase in {"ended", "settled"}:
            phase = "TERMINAL"
        else:
            phase = "LEAD_PLAY" if self.free_lead else "FOLLOW_PLAY"
        self._v2_phase = Knowledge.known(phase, evidence)

    def apply_deal(self, event: DealEvent) -> None:
        require(self.phase == "waiting_deal", "unexpected deal; active game cannot be replaced")
        require(event.my_seat == UNCONFIRMED_SEAT, "deal must not assign an unverified own seat")
        require(1 <= event.level <= 13, "invalid level")
        require(len(event.hand) == 27, "expected 27 own cards")
        checked_cards(event.hand)
        evidence = source_evidence(event.meta, "live.3001", "observed_deal_27")
        self.phase = "waiting_start"
        self.level, self.my_seat = event.level, UNCONFIRMED_SEAT
        self.seat_confirmation_source = None
        # 3001 seat-like fields are retained only for compatibility and are not
        # authoritative start facts in TencentDaguandanObservedProtocol.v20261014.
        self._first_seat_hint = None
        self._finish_rank_by_seat = [None, None, None, None]
        self._deal_connection_id = event.meta.connection_id
        self._deal_sequence = event.meta.sequence
        self.hand = set(event.hand)
        self._initial_hand = event.hand
        self.remaining = [27, 27, 27, 27]
        self.state_version += 1
        self._touch_v2(evidence, "deal", "hand", "counts", "finishes")

    def apply_start(self, event: StartEvent) -> None:
        require(self.phase == "waiting_start", "start requires a complete fresh deal")
        require(event.first_seat_present is True, "first-seat presence required")
        require(type(event.first_seat) is int and event.first_seat in range(4), "invalid first seat")
        require(self.remaining == [27, 27, 27, 27] and len(self.hand) == 27,
                "start requires resolved exchange counts")
        evidence = source_evidence(event.meta, "live.3004", "observed_start")
        self.first_seat = self.current_seat = event.first_seat
        self.phase = "active"
        self.leader_seat = event.first_seat
        self.state_version += 1
        self._play_phase_v2(evidence)
        self._touch_v2(evidence, "position", "leader", "passes")

    def apply_own_seat_candidate(self, event: OwnSeatCandidateEvent) -> bool:
        """Confirm a 3019 candidate only when it exactly owns this deal's hand."""
        require(self.phase in {"waiting_start", "active"},
                "seat evidence requires a current deal")
        require(event.candidate_seat_present is True, "candidate-seat presence required")
        evidence = source_evidence(event.meta, "live.3019", "own_hand_match")
        if self._deal_connection_id and event.meta.connection_id:
            require(event.meta.connection_id == self._deal_connection_id,
                    "seat evidence connection mismatch")
        if self._deal_connection_id and event.meta.connection_id:
            require(event.meta.sequence >= self._deal_sequence,
                    "seat evidence precedes the deal")
        require(type(event.candidate_seat) is int and event.candidate_seat in range(4),
                "invalid candidate seat")
        # 3019 is repeated for changing private snapshots and its scalar field
        # is not stable once play advances.  Only an exact original-deal hand
        # match is seat evidence; every later snapshot remains informational.
        if (self._v2_exchange.resolved_transfers.status is KnowledgeStatus.KNOWN
                and self._v2_exchange.resolved_transfers.value):
            return False
        if Counter(event.referenced_cards) != Counter(self._initial_hand):
            return False
        if self.my_seat in range(4):
            if self.my_seat != event.candidate_seat:
                raise SeatConfirmationConflict(
                    "3019 seat conflicts with an already confirmed seat"
                )
            return False
        self.my_seat = event.candidate_seat
        self.seat_confirmation_source = event.source
        self.state_version += 1
        self._touch_v2(evidence, "observer")
        return True

    def confirm_my_seat_relative(self, relation: SeatRelation | str) -> None:
        """Explicitly confirm the observer seat from the observed first seat.

        ``relation`` describes who the first seat is relative to the observer,
        not a numeric protocol seat. This is allowed only before the first
        action of the current game. An already inferred seat is never silently
        replaced; a conflicting public-card observation remains fail-closed.
        """
        try:
            relation = SeatRelation(relation)
        except (TypeError, ValueError) as exc:
            raise StateError(
                "invalid seat relation; expected self, left, partner, or right"
            ) from exc
        require(self.phase == "active", "manual seat confirmation requires an active game")
        require(self.first_seat in range(4), "manual seat confirmation requires first seat")
        require(not self.history, "manual seat confirmation must occur before first action")
        offsets = {
            SeatRelation.SELF: 0,
            SeatRelation.RIGHT: 1,
            SeatRelation.PARTNER: 2,
            SeatRelation.LEFT: 3,
        }
        candidate = (self.first_seat + offsets[relation]) % 4
        if self.my_seat in range(4):
            require(
                self.my_seat == candidate,
                "manual seat conflicts with an already confirmed seat",
            )
            return
        self.my_seat = candidate
        self.seat_confirmation_source = "manual"
        self.state_version += 1
        evidence = replace(self._v2_evidence["position"], source="manual.seat_relation",
                           derivation="observed_first_seat_relation")
        self._touch_v2(evidence, "observer")

    def apply_informational(self, event: InformationalEvent) -> None:
        """Record safe pre-play evidence without changing game state.

        Informational commands are never model inputs and must not advance the
        turn, hand, history, or state version. Only the already-sanitized
        neutral evidence extracted by the protocol layer is retained.
        """
        if event.command != 3003:
            return
        self._reported_seat_snapshot = event.reported_seat_snapshot
        self._reported_values = event.reported_values
        self._reported_flag = event.reported_flag

    def apply_action(self, event: ActionEvent) -> None:
        candidate = self.clone()
        candidate._apply_action(event)
        self.__dict__.update(candidate.__dict__)

    def _apply_action(self, event: ActionEvent,
                      declaration: PublicDeclaration | None = None) -> None:
        evidence = source_evidence(event.meta, "semantic.declared_action" if declaration else
                                   "live.3005" if event.kind == "play" else "live.3006",
                                   "observed_classic_transition")
        action_phase = "LEAD_PLAY" if self.free_lead else "FOLLOW_PLAY"
        require(type(event.player) is int and event.player in range(4), "invalid actor")
        require(self.phase == "active", "action outside active game")
        require(event.player == self.current_seat, "actor mismatch; missing or duplicate action")
        require(event.next_player is None or event.next_player in range(4), "invalid next actor")
        require(len(self.history) < self.MAX_ACTIONS and len(self._v2_history) < self.MAX_ACTIONS,
                "action history limit exceeded")
        if event.finish_rank is not None:
            require(event.finish_rank in range(1, 5), "invalid finish rank")
        if event.kind == "pass":
            require(event.finish_rank is None, "finish rank on non-finishing action")
        ordinal = len(self.history) + 1
        if event.action_index is not None:
            require(event.action_index == ordinal, "non-contiguous action sequence")
        if event.expected_trick is not None:
            require(event.expected_trick == self.trick, "trick index mismatch")
        if event.free_lead_before is not None:
            require(event.free_lead_before == self.free_lead, "lead/follow mismatch")
        if event.kind == "play":
            require(bool(event.cards), "empty play")
            checked_cards(event.cards)
            require(not self._played_public.intersection(event.cards), "physical card played twice")
            played = set(event.cards)
            # After an explicit resolved exchange, ownership follows the current
            # hand, never the pre-exchange private deal.
            own_initial = played.intersection(self.hand)
            if own_initial:
                if self.my_seat == UNCONFIRMED_SEAT:
                    # The first public play that intersects the dealt private
                    # hand identifies the observer seat.  This is intentionally
                    # data-driven because 3001.field3 is not the observer seat.
                    self.my_seat = event.player
                    self.seat_confirmation_source = "inferred"
                    self._v2_evidence["observer"] = replace(evidence, derivation="own_public_card")
                else:
                    if (self.seat_confirmation_source == "manual"
                            and event.player != self.my_seat):
                        raise SeatConfirmationConflict(
                            "manual seat conflicts with public card ownership"
                        )
                    require(event.player == self.my_seat,
                            "conflicting own-seat candidate from public play")

            if event.player == self.my_seat:
                require(played <= self.hand, "played card absent from own hand")
                self.hand.difference_update(played)
            else:
                # Before seat confirmation, a non-intersecting play is safely
                # treated as public opponent information.  Once confirmed, any
                # own initial card played by another actor is a hard conflict.
                require(not own_initial,
                        "conflicting own-seat candidate from public play")
            self.remaining[event.player] -= len(event.cards)
            require(self.remaining[event.player] >= 0, "negative remaining count")
            if event.remaining_after is not None:
                require(event.remaining_after == self.remaining[event.player],
                        "remaining card count mismatch")
            if self.remaining[event.player] != 0:
                require(event.finish_rank is None, "finish rank on non-finishing action")
            require(event.combo_type is not None and event.combo_type > 0, "missing combo type")
            self._played_public.update(event.cards)
            self._played_by_seat[event.player].extend(event.cards)
            if self.remaining[event.player] == 0:
                expected_rank = len(self._finished) + 1
                if event.finish_rank is not None:
                    require(event.finish_rank == expected_rank, "finish-rank flag mismatch")
                self._finished.append(event.player)
                self._finish_rank_by_seat[event.player] = (
                    event.finish_rank if event.finish_rank is not None else expected_rank
                )
        elif event.kind == "pass":
            require(not self.free_lead and self.lead_action is not None, "pass on free lead")
            require(not event.cards, "pass contains cards")
        else:
            raise StateError("unsupported action kind")
        if event.own_hand_snapshot is not None:
            require(self.my_seat in range(4), "own seat not confirmed for private snapshot")
            require(event.player == self.my_seat, "private snapshot attached to another actor")
            checked_cards(event.own_hand_snapshot)
            require(set(event.own_hand_snapshot) == self.hand, "own-hand snapshot mismatch")

        public = PublicAction(index=ordinal, player=event.player, kind=event.kind,
                              cards=event.cards, combo_type=event.combo_type,
                              next_player=event.next_player, trick=self.trick)
        if event.kind == "play":
            self.lead_action = public
            self._passed.clear()
            self.free_lead = False
            self.consecutive_passes = 0
        else:
            self._passed.add(event.player)
            self.consecutive_passes += 1
        assert self.lead_action is not None
        alive = {seat for seat, count in enumerate(self.remaining) if count > 0}
        ended = any(self.remaining[s] == self.remaining[s + 2] == 0 for s in (0, 1))
        reset_flag = 0
        if ended:
            self.current_seat = None
            self.phase = "ended"
        elif alive - {self.lead_action.player} <= self._passed:
            winner = self.lead_action.player
            self.current_seat = winner if winner in alive else (winner + 2) % 4
            reset_flag = 1 if winner in alive else 2
            require(self.current_seat in alive, "invalid inherited lead")
            self._passed.clear()
            self.lead_action = None
            self.free_lead = True
            self.leader_seat = self.current_seat
            self.consecutive_passes = 0
            self._v2_evidence["leader"] = evidence
            self.trick += 1
        else:
            self.current_seat = (event.player - 1) % 4
            while self.current_seat not in alive:
                self.current_seat = (self.current_seat - 1) % 4
        # None means terminal, not "skip validation".
        require(event.next_player == self.current_seat, "next actor mismatch")
        if event.kind == "pass":
            if reset_flag == 0:
                require(event.reset_flag in (None, 0), "unexpected pass reset/lead flag")
            else:
                require(event.reset_flag == reset_flag, "pass reset/lead flag mismatch")
        else:
            require(event.reset_flag is None, "pass reset flag on play")
        self.history.append(public)
        self.state_version += 1
        if event.kind == "pass":
            meaning = Knowledge.absent(evidence)
        elif declaration is None:
            meaning = Knowledge.unsupported("live_declaration_unverified")
        else:
            meaning = Knowledge.known(declaration, evidence)
        canonical = declaration.canonical_cards if declaration else event.cards
        committed = CommittedActionV2(
            len(self._v2_history) + 1, event.player, action_phase, event.kind,
            canonical, meaning,
            Knowledge.absent(evidence) if self.current_seat is None else
            Knowledge.known(self.current_seat, evidence),
            Knowledge.known(self.remaining[event.player], evidence), evidence,
        )
        committed.to_dict()
        self._v2_history.append(committed)
        if event.kind == "play":
            self._v2_winning = CurrentWinning(event.player, canonical, meaning)
        if self.lead_action is None:
            self._v2_winning = None
        self._play_phase_v2(evidence)
        self._touch_v2(evidence, "position", "passes", "hand", "counts", "finishes")

    def apply_settlement(self, event: SettlementEvent) -> None:
        candidate = self.clone()
        candidate._apply_settlement(event)
        self.__dict__.update(candidate.__dict__)

    def _apply_settlement(self, event: SettlementEvent) -> None:
        # Validate the settlement payload even when it is a duplicate.  This
        # keeps malformed/reordered input fail-closed while making a valid
        # repeated 3008 a harmless idempotent notification.
        evidence = source_evidence(event.meta, "live.3008", "validated_public_ranking")
        require(self.my_seat in range(4), "own seat not confirmed before settlement")
        require(all(type(rank) is int for rank in event.ranks_by_seat), "invalid settlement ranks")
        require(sorted(event.ranks_by_seat) == [1, 2, 3, 4], "invalid settlement ranks")
        if self.phase == "settled":
            require(
                tuple(event.ranks_by_seat) == tuple(self._finish_rank_by_seat),
                "conflicting settlement ranks",
            )
        require(len(event.remaining_hands) == 4, "expected four settlement hands")
        require(all(event.ranks_by_seat[s] == i + 1 for i, s in enumerate(self._finished)),
                "settlement finish order mismatch")
        all_cards: list[int] = []
        for seat, hand in enumerate(event.remaining_hands):
            checked_cards(hand)
            require(len(hand) == self.remaining[seat], "settlement remaining count mismatch")
            initial = self._played_by_seat[seat] + list(hand)
            require(len(initial) == 27, "reconstructed initial count mismatch")
            all_cards.extend(initial)
        validate_deck(all_cards)
        require(set(event.remaining_hands[self.my_seat]) == self.hand, "own final hand mismatch")
        if self.phase == "settled":
            return
        require(self.phase == "ended", "settlement before observed game end")
        self._finish_rank_by_seat = list(event.ranks_by_seat)
        # Do NOT retain event.remaining_hands or append them to history/observations.
        self.phase = "settled"
        self.state_version += 1
        self._play_phase_v2(evidence)
        self._touch_v2(evidence, "position", "finishes", "settlement")

    def apply_phase_observed(self, event: PhaseObserved) -> None:
        require(type(event) is PhaseObserved, "invalid semantic event type")
        event.validate()
        evidence = source_evidence(event.meta, "semantic.phase", "explicit_phase")
        if event.phase in {"TRIBUTE", "RETURN"}:
            require(self.phase == "waiting_start", "exchange phase outside pre-play")
            require(not (self._v2_phase.value == "RETURN" and event.phase == "TRIBUTE"),
                    "exchange phase went backwards")
        else:
            expected = ("TERMINAL" if self.phase in {"ended", "settled"} else
                        "LEAD_PLAY" if self.free_lead else "FOLLOW_PLAY")
            require(self.phase in {"active", "ended", "settled"} and event.phase == expected,
                    "phase conflicts with confirmed state")
            require(event.active_seat == self.current_seat, "phase actor mismatch")
        if (self._v2_phase.value == event.phase
                and (event.phase not in {"TRIBUTE", "RETURN"}
                     or self._exchange_active_seat == event.active_seat)):
            return
        self._v2_phase = Knowledge.known(event.phase, evidence)
        self._exchange_active_seat = event.active_seat
        self._v2_exchange = replace(
            self._v2_exchange,
            self_selection=(self._v2_exchange.self_selection
                            if self._v2_exchange.self_selection.status is KnowledgeStatus.KNOWN
                            else Knowledge.unsupported("selection_not_observed_in_phase")),
        )
        self._touch_v2(evidence, "position")

    def apply_previous_result(self, event: PublicResultObserved) -> None:
        require(type(event) is PublicResultObserved, "invalid semantic event type")
        event.validate()
        evidence = source_evidence(event.meta, "semantic.previous_result", "explicit_previous_association")
        result = PreviousResultSummary.from_dict(event.result.to_dict())
        require(result.source_game_id.status is KnowledgeStatus.KNOWN
                and result.source_game_id.value != self.game_id,
                "previous result requires a distinct associated game")
        require(self.phase == "waiting_start", "previous result outside fresh deal")
        if self._v2_previous_result.status is KnowledgeStatus.KNOWN:
            require(self._v2_previous_result.value == result, "conflicting previous result")
            return
        self._v2_previous_result = Knowledge.known(result, evidence)
        self._touch_v2(evidence, "previous_result")

    def apply_declared_action(self, event: DeclaredActionCommitted) -> None:
        require(type(event) is DeclaredActionCommitted, "invalid semantic event type")
        try:
            event.validate()
            declaration = PublicDeclaration.from_dict(event.declaration.to_dict())
            declaration.validate_for_level(self.level)
        except PublicStateContractError:
            raise StateError("declared action semantic invalid") from None
        candidate = self.clone()
        candidate._apply_action(event.action, declaration)
        self.__dict__.update(candidate.__dict__)

    def apply_exchange_resolved(self, event: ExchangeResolvedObserved) -> None:
        require(type(event) is ExchangeResolvedObserved, "invalid semantic event type")
        try:
            event.validate()
        except PublicStateContractError:
            raise StateError("resolved exchange semantic invalid") from None
        candidate = self.clone()
        candidate._apply_exchange_resolved(event)
        self.__dict__.update(candidate.__dict__)

    def _apply_exchange_resolved(self, event: ExchangeResolvedObserved) -> None:
        evidence = source_evidence(event.meta, "semantic.exchange", "public_resolved_transfer")
        require(self.phase == "waiting_start" and self._v2_phase.value in {"TRIBUTE", "RETURN"},
                "exchange requires an explicitly observed phase")
        require(not event.transfers or self.my_seat in range(4),
                "exchange requires a confirmed observer")
        prior = self._v2_exchange
        transfers = (prior.resolved_transfers.value
                     if prior.resolved_transfers.status is KnowledgeStatus.KNOWN else ())
        require(len(transfers) + len(event.transfers) <= 8, "exchange history limit exceeded")
        require(len(self._v2_history) + len(event.transfers) <= self.MAX_ACTIONS,
                "action history limit exceeded")
        event_cards = tuple(transfer.card for transfer in event.transfers)
        require(len(event_cards) == len(set(event_cards)),
                "exchange card repeated within one event")
        prior_kind_cards = {(transfer.kind, transfer.card) for transfer in transfers}
        require(all((transfer.kind, transfer.card) not in prior_kind_cards
                    for transfer in event.transfers),
                "exchange card repeated in the same phase")
        def fact(value: object, old: Knowledge) -> Knowledge:
            if value is None:
                return old
            if old.status is KnowledgeStatus.KNOWN:
                require(old.value == value, "conflicting exchange facts")
                return old
            return Knowledge.known(value, evidence)
        donors = fact(event.tribute_donors, prior.tribute_donors)
        order = fact(event.tribute_order, prior.tribute_order)
        anti = fact(event.anti_tribute, prior.anti_tribute)
        obligations = fact(event.return_obligations, prior.return_obligations)
        all_transfers = (*transfers, *event.transfers)
        if donors.status is KnowledgeStatus.KNOWN and order.status is KnowledgeStatus.KNOWN:
            require(set(donors.value) == set(order.value), "exchange donor/order mismatch")
        if donors.status is KnowledgeStatus.KNOWN:
            donor_seats = set(donors.value)
            require(all(t.source_seat in donor_seats for t in all_transfers
                        if t.kind == "tribute"),
                    "tribute source is not a known donor")
        if obligations.status is KnowledgeStatus.KNOWN:
            obligation_pairs = set(obligations.value)
            require(all((t.source_seat, t.target_seat) in obligation_pairs
                        for t in all_transfers if t.kind == "return"),
                    "return transfer does not match a known obligation")
        require(not (anti.value is True and any(t.kind == "tribute"
                    for t in all_transfers)), "anti-tribute conflicts with transfer")
        locations = self._exchange_card_locations.copy()
        require(len(locations) <= 8, "exchange card-location limit exceeded")
        for transfer in event.transfers:
            require(transfer.kind.upper() == self._v2_phase.value,
                    "transfer kind conflicts with phase")
            require(transfer not in transfers, "resolved exchange repeated")
            known_source = locations.get(transfer.card)
            require(known_source is None or known_source == transfer.source_seat,
                    "resolved exchange card source conflicts with public location")
            locations[transfer.card] = transfer.target_seat
            require(len(locations) <= 8, "exchange card-location limit exceeded")
            require(transfer.card not in self._played_public, "exchange of played card")
            if transfer.source_seat == self.my_seat:
                require(transfer.card in self.hand, "exchange card absent from own hand")
                self.hand.remove(transfer.card)
            else:
                require(transfer.card not in self.hand, "exchange conflicts with own ownership")
            if transfer.target_seat == self.my_seat:
                self.hand.add(transfer.card)
            self.remaining[transfer.source_seat] -= 1
            self.remaining[transfer.target_seat] += 1
            require(all(0 <= count <= 28 for count in self.remaining), "exchange count out of bounds")
            self._v2_history.append(CommittedActionV2(
                len(self._v2_history) + 1, transfer.source_seat, self._v2_phase.value,
                transfer.kind, (transfer.card,), Knowledge.absent(evidence),
                Knowledge.unknown("exchange_next_actor_unobserved"),
                Knowledge.known(self.remaining[transfer.source_seat], evidence), evidence,
            ))
        updated = ExchangeState(donors, order, anti, obligations,
                                Knowledge.known((*transfers, *event.transfers), evidence),
                                prior.self_selection if prior.self_selection.status is KnowledgeStatus.KNOWN
                                else (Knowledge.unsupported("selection_after_resolution_unobserved")
                                      if event.transfers else prior.self_selection))
        updated.to_dict()
        if not event.transfers and updated.to_dict() == prior.to_dict():
            return
        self._v2_exchange = updated
        self._exchange_card_locations = locations
        if event.transfers:
            self.state_version += 1
        self._touch_v2(evidence, "hand", "counts")

    def apply_own_exchange_selection(self, event: OwnExchangeSelectionObserved) -> None:
        require(type(event) is OwnExchangeSelectionObserved, "invalid semantic event type")
        event.validate()
        evidence = source_evidence(event.meta, "semantic.self_selection", "observer_private_selection")
        require(self.phase == "waiting_start" and self._v2_phase.value == event.phase,
                "selection requires an explicitly observed exchange phase")
        require(event.observer_seat == self.my_seat, "selection belongs to another observer")
        require(self._exchange_active_seat is None or self._exchange_active_seat == self.my_seat,
                "selection outside observer turn")
        require(set(event.cards) <= self.hand, "selection card absent from own hand")
        old = self._v2_exchange.self_selection
        if old.status is KnowledgeStatus.KNOWN and old.value == event.cards:
            return
        self._v2_exchange = replace(self._v2_exchange,
                                   self_selection=Knowledge.known(event.cards, evidence))
        self._touch_v2(evidence, "selection")

    def settled_result_candidate(self) -> Knowledge:
        """Public summary for a *subsequent* associated deal; never a current label."""
        require(self.phase == "settled" and "settlement" in self._v2_evidence,
                "no validated settlement result")
        evidence = replace(self._v2_evidence["settlement"], derivation="observed_opposite_teams_ranking")
        ranks = tuple(self._finish_rank_by_seat)
        head = ranks.index(1)
        result = PreviousResultSummary(
            Knowledge.known(self.game_id, evidence), ranks, head % 2,
            {2: "DOUBLE_DOWN", 3: "HEAD_THIRD", 4: "HEAD_LAST"}[ranks[(head + 2) % 4]],
            Knowledge.unsupported("tribute_cancelled_unobserved"),
        )
        result.to_dict()
        return Knowledge.known(result, evidence)

    def public_state_v2(self, session_id: str, *, previous_result: Knowledge | None = None,
                        public_state_revision: int | None = None) -> CanonicalGuandanPublicState:
        """Detached public+self facts, including non-self turns and terminal states.

        Serialization is not model eligibility. Exact histories, exchange
        coverage, counters and rule equivalence remain unverified even when
        individual explicit semantic events have been observed.
        """
        require(self.phase != "waiting_deal", "public state requires a confirmed deal")
        deal = self._v2_evidence.get("deal", EvidenceRef("domain.synthetic"))
        position = self._v2_evidence.get("position", deal)
        hand_evidence = self._v2_evidence.get("hand", deal)
        counts_evidence = self._v2_evidence.get("counts", deal)
        previous = self._v2_previous_result if previous_result is None else previous_result
        require(type(previous) is Knowledge, "invalid previous result")
        if previous.status is KnowledgeStatus.KNOWN:
            require(type(previous.value) is PreviousResultSummary
                    and previous.value.source_game_id.status is KnowledgeStatus.KNOWN
                    and previous.value.source_game_id.value != self.game_id,
                    "previous result requires a distinct associated game")
        terminal = self.phase in {"ended", "settled"}
        started = self.phase in {"active", "ended", "settled"}
        active = self.current_seat if started else self._exchange_active_seat
        observer = (Knowledge.known(self.my_seat, self._v2_evidence.get("observer", position))
                    if self.my_seat in range(4) else Knowledge.unknown("observer_not_confirmed"))
        winning = (Knowledge.known(self._v2_winning, position) if self._v2_winning else
                   Knowledge.absent(position) if started and self.lead_action is None else
                   Knowledge.unknown("current_winning_not_observed"))
        history = HistoryEvidence(
            tuple(self._v2_history), len(self._v2_history),
            Knowledge.unsupported("public_history_origin_unverified"),
            Knowledge.unsupported("self_private_history_unverified"),
            Knowledge.unsupported("transport_completeness_unverified"),
            Knowledge.unsupported("token_step_unverified"),
            Knowledge.unsupported("committed_step_unverified"),
        )
        result = CanonicalGuandanPublicState(
            identity=StateIdentity(session_id, self.game_id,
                                   self.state_version, self.public_state_revision
                                   if public_state_revision is None else public_state_revision),
            ruleset_compatibility=Knowledge.unsupported("marvel_ruleset_equivalence_unverified"),
            observer_seat=observer,
            active_seat=Knowledge.absent(position) if terminal else
                        Knowledge.known(active, position) if active is not None else
                        Knowledge.unknown("active_seat_not_observed"),
            leader_seat=Knowledge.known(self.leader_seat, self._v2_evidence.get("leader", position))
                        if self.leader_seat is not None else Knowledge.unknown("leader_not_observed"),
            current_winner_seat=Knowledge.known(self._v2_winning.actor, position) if self._v2_winning else
                                Knowledge.absent(position) if winning.status is KnowledgeStatus.ABSENT else
                                Knowledge.unknown("current_winner_not_observed"),
            phase=self._v2_phase,
            level=Knowledge.known(self.level, deal),
            own_hand=Knowledge.known(tuple(sorted(self.hand)), hand_evidence),
            remaining_counts=Knowledge.known(tuple(self.remaining), counts_evidence),
            current_winning=winning,
            consecutive_passes=Knowledge.known(self.consecutive_passes,
                                               self._v2_evidence.get("passes", position)) if started else
                               Knowledge.unknown("play_not_started"),
            emptied_seats=Knowledge.known(tuple(s for s, n in enumerate(self.remaining) if n == 0),
                                          counts_evidence),
            finishing_ranks=tuple(Knowledge.known(rank, self._v2_evidence.get("finishes", deal))
                                 if rank is not None else Knowledge.unknown("finish_rank_not_observed")
                                 for rank in self._finish_rank_by_seat),
            previous_result=previous, exchange=self._v2_exchange, history=history,
        )
        facts = [result.ruleset_compatibility, result.observer_seat, result.active_seat,
                 result.leader_seat, result.current_winner_seat, result.phase, result.current_winning,
                 result.consecutive_passes, result.previous_result, *result.finishing_ranks,
                 history.public_history_complete, history.self_private_history_complete,
                 history.transport_contiguous, history.token_step, history.committed_step,
                 result.exchange.tribute_donors, result.exchange.tribute_order,
                 result.exchange.anti_tribute, result.exchange.return_obligations,
                 result.exchange.resolved_transfers, result.exchange.self_selection]
        facts.extend(action.declaration for action in history.actions)
        if previous.status is KnowledgeStatus.KNOWN:
            facts.append(previous.value.tribute_cancelled)
        result = replace(result,
            unknown_reasons=tuple(sorted({x.reason for x in facts if x.status is KnowledgeStatus.UNKNOWN})),
            unsupported_reasons=tuple(sorted({x.reason for x in facts if x.status is KnowledgeStatus.UNSUPPORTED})))
        # Round-trip also freezes caller-supplied Knowledge values into tuples.
        return CanonicalGuandanPublicState.from_dict(result.to_dict())

    def snapshot(self) -> StateSnapshot:
        return StateSnapshot(
            game_id=self.game_id, level=self.level, my_seat=self.my_seat,
            first_seat=self.first_seat, current_seat=self.current_seat,
            hand=tuple(sorted(self.hand)),
            remaining=cast(tuple[int, int, int, int], tuple(self.remaining)),
            history=tuple(self.history), lead_action=self.lead_action,
            free_lead=self.free_lead, trick=self.trick, phase=self.phase,
            state_version=self.state_version,
            seat_confirmation_source=self.seat_confirmation_source)

    def table_snapshot(self, session_id: str, captured_at: float) -> dict[str, object]:
        """Detached display data, available from deal without model eligibility.

        Unknown/terminal seats use -1 in this UI-facing payload.  Keep the
        existing StateSnapshot/observation contracts separate: display data
        does not confirm the observer seat or authorize a recommendation.
        Settlement private hands and informational commands are never inputs.
        """
        counter = count_visible_cards(self.hand, self._played_public, level=self.level)
        return {
            "session_id": session_id,
            "game_id": self.game_id,
            "state_version": self.state_version,
            "captured_at": captured_at,
            "phase": self.phase,
            "level": self.level,
            "my_seat": self.my_seat,
            "first_seat": self.first_seat if self.first_seat is not None else -1,
            "current_seat": self.current_seat if self.current_seat is not None else -1,
            "hand": sorted(self.hand),
            "remaining": list(self.remaining),
            "history": [action.to_dict() for action in self.history],
            "finish_ranks_by_seat": [rank if rank is not None else -1 for rank in self._finish_rank_by_seat],
            "free_lead": self.free_lead,
            "trick": self.trick,
            "seat_confirmed": self.my_seat in range(4),
            "seat_confirmation_source": self.seat_confirmation_source,
            "recommendation_eligible": (
                self.phase == "active"
                and self.my_seat in range(4)
                and self.current_seat == self.my_seat
            ),
            "card_counter": counter,
            "counter_unseen_total": sum(row["remaining"] for row in counter),
            "counter_public_remaining_total": sum(row["remaining"] for row in counter),
            "multiplier_evidence": {
                "reported_seat_snapshot": (
                    list(self._reported_seat_snapshot)
                    if self._reported_seat_snapshot is not None else None
                ),
                "reported_values": (
                    list(self._reported_values)
                    if self._reported_values is not None else None
                ),
                "reported_flag": self._reported_flag,
            },
        }

    def observation(self, session_id: str, captured_at: float) -> GameObservation | None:
        """Safe model/UI view; unavailable until the observer seat is confirmed."""
        if self.phase != "active" or self.my_seat not in range(4):
            return None
        assert self.first_seat is not None and self.current_seat is not None
        return GameObservation(
            session_id=session_id, game_id=self.game_id, state_version=self.state_version,
            captured_at=captured_at, level=self.level, my_seat=self.my_seat,
            first_seat=self.first_seat, current_seat=self.current_seat,
            hand=tuple(sorted(self.hand)),
            remaining=cast(tuple[int, int, int, int], tuple(self.remaining)),
            history=tuple(self.history), lead_action=self.lead_action,
            free_lead=self.free_lead, trick=self.trick)

    @property
    def committed_actions_v2(self) -> tuple[CommittedActionV2, ...]:
        return tuple(self._v2_history)

    @property
    def current_winning_declaration(self) -> Knowledge | None:
        return None if self._v2_winning is None else self._v2_winning.declaration

    @property
    def initial_hand(self) -> tuple[int, ...]:
        return self._initial_hand

    @property
    def finished(self) -> tuple[int, ...]:
        return tuple(self._finished)
