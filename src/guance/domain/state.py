"""Pure, transactional state transitions for the observed classic-room rules.

A GameState is mutated only by confirmed events, never by recommendations.
Opponent hands are not state. Settlement hands are checked and immediately
forgotten; every prior observation remains an immutable visible-information view.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import cast

from guance.contracts import GameObservation, PublicAction
from guance.domain.card_counter import count_visible_cards
from guance.domain.cards import card_parts, validate_deck
from guance.domain.events import (
    UNCONFIRMED_SEAT, ActionEvent, DealEvent, InformationalEvent,
    OwnSeatCandidateEvent, SettlementEvent, SeatRelation, StartEvent,
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
        return other

    def apply_deal(self, event: DealEvent) -> None:
        require(self.phase == "waiting_deal", "unexpected deal; active game cannot be replaced")
        require(event.my_seat == UNCONFIRMED_SEAT, "deal must not assign an unverified own seat")
        require(1 <= event.level <= 13, "invalid level")
        require(len(event.hand) == 27, "expected 27 own cards")
        checked_cards(event.hand)
        self.phase = "waiting_start"
        self.level, self.my_seat = event.level, UNCONFIRMED_SEAT
        self.seat_confirmation_source = None
        self._first_seat_hint = event.first_seat_hint
        self._finish_rank_by_seat = [None, None, None, None]
        self._deal_connection_id = event.meta.connection_id
        self._deal_sequence = event.meta.sequence
        self.hand = set(event.hand)
        self._initial_hand = event.hand
        self.remaining = [27, 27, 27, 27]
        self.state_version += 1

    def apply_start(self, event: StartEvent) -> None:
        require(self.phase == "waiting_start", "start requires a complete fresh deal")
        require(event.first_seat in range(4), "invalid first seat")
        if self._first_seat_hint is not None:
            require(event.first_seat == self._first_seat_hint,
                    "deal/start first-seat hints conflict")
        self.first_seat = self.current_seat = event.first_seat
        self.phase = "active"
        self.state_version += 1

    def apply_own_seat_candidate(self, event: OwnSeatCandidateEvent) -> bool:
        """Confirm a 3019 candidate only when it exactly owns this deal's hand."""
        require(self.phase in {"waiting_start", "active"},
                "seat evidence requires a current deal")
        if self._deal_connection_id and event.meta.connection_id:
            require(event.meta.connection_id == self._deal_connection_id,
                    "seat evidence connection mismatch")
        if self._deal_sequence and event.meta.sequence:
            require(event.meta.sequence >= self._deal_sequence,
                    "seat evidence precedes the deal")
        if Counter(event.referenced_cards) != Counter(self._initial_hand):
            return False
        require(event.candidate_seat in range(4), "invalid candidate seat")
        if self.my_seat in range(4):
            if self.my_seat != event.candidate_seat:
                raise SeatConfirmationConflict(
                    "3019 seat conflicts with an already confirmed seat"
                )
            return False
        self.my_seat = event.candidate_seat
        self.seat_confirmation_source = event.source
        self.state_version += 1
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

    def _apply_action(self, event: ActionEvent) -> None:
        require(self.phase == "active", "action outside active game")
        require(event.player == self.current_seat, "actor mismatch; missing or duplicate action")
        require(event.next_player is None or event.next_player in range(4), "invalid next actor")
        require(len(self.history) < self.MAX_ACTIONS, "action history limit exceeded")
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
            own_initial = played.intersection(self._initial_hand)
            if own_initial:
                if self.my_seat == UNCONFIRMED_SEAT:
                    # The first public play that intersects the dealt private
                    # hand identifies the observer seat.  This is intentionally
                    # data-driven because 3001.field3 is not the observer seat.
                    self.my_seat = event.player
                    self.seat_confirmation_source = "inferred"
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
        else:
            self._passed.add(event.player)
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
            self.trick += 1
        else:
            self.current_seat = (event.player - 1) % 4
            while self.current_seat not in alive:
                self.current_seat = (self.current_seat - 1) % 4
        # None means terminal, not "skip validation".
        require(event.next_player == self.current_seat, "next actor mismatch")
        if event.reset_flag is not None:
            require(event.reset_flag == reset_flag, "pass reset/lead flag mismatch")
        self.history.append(public)
        self.state_version += 1

    def apply_settlement(self, event: SettlementEvent) -> None:
        candidate = self.clone()
        candidate._apply_settlement(event)
        self.__dict__.update(candidate.__dict__)

    def _apply_settlement(self, event: SettlementEvent) -> None:
        # Validate the settlement payload even when it is a duplicate.  This
        # keeps malformed/reordered input fail-closed while making a valid
        # repeated 3008 a harmless idempotent notification.
        require(self.my_seat in range(4), "own seat not confirmed before settlement")
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
    def initial_hand(self) -> tuple[int, ...]:
        return self._initial_hand

    @property
    def finished(self) -> tuple[int, ...]:
        return tuple(self._finished)
