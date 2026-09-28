"""Pure, transactional state transitions for the observed classic-room rules.

A GameState is mutated only by confirmed events, never by recommendations.
Opponent hands are not state. Settlement hands are checked and immediately
forgotten; every prior observation remains an immutable visible-information view.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from guance.contracts import GameObservation, PublicAction
from guance.domain.cards import card_parts, validate_deck
from guance.domain.events import ActionEvent, DealEvent, SettlementEvent, StartEvent


class StateError(ValueError):
    """A confirmed event cannot be applied without guessing or corrupting state."""


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
        self.my_seat = -1
        self.first_seat: int | None = None
        self.current_seat: int | None = None
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
        self._initial_hand: tuple[int, ...] = ()

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
        return other

    def apply_deal(self, event: DealEvent) -> None:
        require(self.phase == "waiting_deal", "unexpected deal; active game cannot be replaced")
        require(event.my_seat in range(4) and 1 <= event.level <= 13, "invalid seat or level")
        require(len(event.hand) == 27, "expected 27 own cards")
        checked_cards(event.hand)
        self.phase = "waiting_start"
        self.level, self.my_seat = event.level, event.my_seat
        self.hand = set(event.hand)
        self._initial_hand = event.hand
        self.remaining = [27, 27, 27, 27]
        self.state_version += 1

    def apply_start(self, event: StartEvent) -> None:
        require(self.phase == "waiting_start", "start requires a complete fresh deal")
        require(event.first_seat in range(4), "invalid first seat")
        self.first_seat = self.current_seat = event.first_seat
        self.phase = "active"
        self.state_version += 1

    def apply_action(self, event: ActionEvent) -> None:
        candidate = self.clone()
        candidate._apply_action(event)
        self.__dict__.update(candidate.__dict__)

    def _apply_action(self, event: ActionEvent) -> None:
        require(self.phase == "active", "action outside active game")
        require(event.player == self.current_seat, "actor mismatch; missing or duplicate action")
        require(event.next_player is None or event.next_player in range(4), "invalid next actor")
        require(len(self.history) < self.MAX_ACTIONS, "action history limit exceeded")
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
            if event.player == self.my_seat:
                require(set(event.cards) <= self.hand, "played card absent from own hand")
                self.hand.difference_update(event.cards)
            else:
                require(not set(event.cards).intersection(self._initial_hand),
                        "opponent played a card belonging to own initial hand")
            self.remaining[event.player] -= len(event.cards)
            require(self.remaining[event.player] >= 0, "negative remaining count")
            if event.remaining_after is not None:
                require(event.remaining_after == self.remaining[event.player],
                        "remaining card count mismatch")
            require(event.combo_type is not None and event.combo_type > 0, "missing combo type")
            self._played_public.update(event.cards)
            self._played_by_seat[event.player].extend(event.cards)
            if self.remaining[event.player] == 0:
                self._finished.append(event.player)
                if event.finish_rank is not None:
                    require(event.finish_rank == len(self._finished), "finish-rank flag mismatch")
        elif event.kind == "pass":
            require(not self.free_lead and self.lead_action is not None, "pass on free lead")
            require(not event.cards, "pass contains cards")
        else:
            raise StateError("unsupported action kind")
        if event.own_hand_snapshot is not None:
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
        require(self.phase == "ended", "settlement before observed game end")
        require(sorted(event.ranks_by_seat) == [1, 2, 3, 4], "invalid settlement ranks")
        require(all(event.ranks_by_seat[s] == i + 1 for i, s in enumerate(self._finished)),
                "settlement finish order mismatch")
        require(len(event.remaining_hands) == 4, "expected four settlement hands")
        all_cards: list[int] = []
        for seat, hand in enumerate(event.remaining_hands):
            checked_cards(hand)
            require(len(hand) == self.remaining[seat], "settlement remaining count mismatch")
            initial = self._played_by_seat[seat] + list(hand)
            require(len(initial) == 27, "reconstructed initial count mismatch")
            all_cards.extend(initial)
        validate_deck(all_cards)
        require(set(event.remaining_hands[self.my_seat]) == self.hand, "own final hand mismatch")
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
            state_version=self.state_version)

    def observation(self, session_id: str, captured_at: float) -> GameObservation | None:
        """Current visible information even on opponent turns; no terminal advice."""
        if self.phase != "active":
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
