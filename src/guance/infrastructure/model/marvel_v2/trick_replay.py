"""Bounded deterministic replay of the exact-history trick topology.

The exact four-observer history is the only accepted source of play history.
This module replays its PUBLIC_COMMIT steps in order and derives the same
facts that ``guance.domain.state.GameState`` derives from confirmed action
events:

* decreasing seat rotation (the project's observed order, never assumed);
* emptied-seat skipping;
* the per-trick pass set and trailing consecutive passes;
* trick closure when every alive seat except the current winner has passed;
* the inherited lead of an emptied winner's partner (``(winner + 2) % 4``);
* the team-pair terminal condition.

Initial card counts are derived exactly as ``final remaining + cards played
inside the window``, so a bounded window that starts after a seat emptied is
still replayed correctly and a seat's emptying happens on its true last play.

Any actor gap, repeated actor, skipped seat, missing pass, pass on a closed
trick, commit after terminal, wrong closure or wrong inheritance raises
``MODEL_INPUT_UNSUPPORTED``. Nothing is repaired, guessed or backfilled, and
there is no second rule engine: the differential tests drive the real
``GameState`` and compare every derived fact after every commit.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from guance.ports.model import ModelBackendError
from .contracts import CARD_TOKEN_BASE, EVENT_PUBLIC_COMMIT, PASS_TOKEN
from .history import HistoryStep

SEAT_COUNT = 4
_CARD_TOKEN_END = CARD_TOKEN_BASE + 108
_MAX_PLAY_CARDS = 27
_MAX_SEAT_CARDS = 28


def _reject() -> None:
    raise ModelBackendError("MODEL_INPUT_UNSUPPORTED")


@dataclass(frozen=True, slots=True)
class TrickTopologyReplay:
    """Deterministic topology facts derived from one exact history."""

    phase: str
    terminal: bool
    trick_open: bool
    expected_actor: int | None
    expected_leader: int | None
    current_leader: int | None
    current_winner: int | None
    passed: frozenset[int]
    trailing_passes: int
    last_play_actor: int | None
    alive: frozenset[int]
    emptied: frozenset[int]
    winner_tokens: tuple[int, ...]
    committed: int
    lead_committed: int


def _played_cards(tokens: tuple[int, ...]) -> int:
    return sum(1 for token in tokens if CARD_TOKEN_BASE <= token < _CARD_TOKEN_END)


def _next_alive(alive: frozenset[int], actor: int) -> int | None:
    seat = (actor - 1) % SEAT_COUNT
    for _ in range(SEAT_COUNT):
        if seat in alive:
            return seat
        seat = (seat - 1) % SEAT_COUNT
    return None


def replay_trick_topology(steps: Sequence[HistoryStep], *,
                          remaining_counts: Sequence[int]) -> TrickTopologyReplay:
    """Replay PUBLIC_COMMIT steps; raise MODEL_INPUT_UNSUPPORTED on any gap."""
    counts = tuple(remaining_counts)
    if (len(counts) != SEAT_COUNT
            or any(type(count) is not int or not 0 <= count <= _MAX_SEAT_CARDS
                   for count in counts)):
        _reject()
    commits = tuple(step for step in steps if step.event_code == EVENT_PUBLIC_COMMIT)
    played = [0] * SEAT_COUNT
    for step in commits:
        tokens = step.event_tokens
        if tokens == (PASS_TOKEN,):
            continue
        size = _played_cards(tokens)
        if not 1 <= size <= _MAX_PLAY_CARDS:
            _reject()
        played[step.actor] += size
    remaining = [counts[seat] + played[seat] for seat in range(SEAT_COUNT)]
    alive = frozenset(seat for seat in range(SEAT_COUNT) if remaining[seat] > 0)

    trick_open = False
    terminal = False
    current_leader: int | None = None
    current_winner: int | None = None
    expected_actor: int | None = None
    expected_leader: int | None = None
    passed: set[int] = set()
    trailing = 0
    last_play: int | None = None
    winner_tokens: tuple[int, ...] = ()
    lead_committed = 0

    for step in commits:
        if terminal:
            _reject()
        tokens = step.event_tokens
        is_pass = tokens == (PASS_TOKEN,)
        if trick_open:
            if step.phase != "FOLLOW_PLAY":
                _reject()
        else:
            if step.phase != "LEAD_PLAY":
                _reject()
            if lead_committed == 0:
                # The window's first lead is the anchor fact: a bounded history
                # does not contain the game start that selected that leader.
                expected_actor = step.actor
        if step.actor != expected_actor:
            _reject()
        if remaining[step.actor] <= 0:
            _reject()
        if is_pass:
            passed.add(step.actor)
            trailing += 1
        else:
            size = _played_cards(tokens)
            remaining[step.actor] -= size
            if remaining[step.actor] < 0:
                _reject()
            if not trick_open:
                current_leader = step.actor
                lead_committed += 1
                expected_leader = None
            passed.clear()
            trailing = 0
            last_play = step.actor
            winner_tokens = tokens
            current_winner = step.actor
            trick_open = True
        alive = frozenset(seat for seat in range(SEAT_COUNT) if remaining[seat] > 0)
        if remaining[0] == remaining[2] == 0 or remaining[1] == remaining[3] == 0:
            terminal = True
            expected_actor = None
            expected_leader = None
            continue
        if trick_open and alive - {current_winner} <= passed:
            winner = current_winner
            leader = winner if winner in alive else (winner + 2) % SEAT_COUNT
            if leader not in alive:
                _reject()
            passed.clear()
            trailing = 0
            trick_open = False
            current_leader = leader
            current_winner = None
            expected_leader = leader
            expected_actor = leader
            continue
        if not trick_open:
            # A closed trick must be reopened by an explicit LEAD commit; the
            # branch is defensive because the phase check above already fails.
            _reject()
        expected_actor = _next_alive(alive, step.actor)
        if expected_actor is None:
            _reject()

    return TrickTopologyReplay(
        phase=("TERMINAL" if terminal else
               "LEAD_PLAY" if not trick_open else "FOLLOW_PLAY"),
        terminal=terminal, trick_open=trick_open,
        expected_actor=expected_actor, expected_leader=expected_leader,
        current_leader=current_leader, current_winner=current_winner,
        passed=frozenset(passed), trailing_passes=trailing,
        last_play_actor=last_play, alive=alive, emptied=frozenset(
            seat for seat in range(SEAT_COUNT) if remaining[seat] == 0),
        winner_tokens=winner_tokens, committed=len(commits),
        lead_committed=lead_committed)


__all__ = ["SEAT_COUNT", "TrickTopologyReplay", "replay_trick_topology"]
