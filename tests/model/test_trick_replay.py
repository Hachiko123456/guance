"""Phase 3 Repair-3: deterministic exact-history trick replay.

Two independent layers:

* explicit truth-table cases for actor rotation, emptied-seat skipping, pass
  sets, closure, inherited lead and the terminal condition;
* a differential fuzz that drives the real ``guance.domain.state.GameState``
  through generated confirmed actions and compares every derived topology fact
  with ``replay_trick_topology`` after each commit.

The domain is the project's single rule source; the replay must reproduce it,
never a second unverified rule set.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from guance.domain.cards import full_deck  # noqa: E402
from guance.domain.events import ActionEvent, DealEvent, StartEvent  # noqa: E402
from guance.domain.state import GameState, StateError  # noqa: E402
from guance.infrastructure.model.marvel_v2.contracts import (  # noqa: E402
    CARD_TOKEN_BASE, COMMIT_TOKEN, FAMILY_TOKEN, MarvelEvent, PASS_TOKEN,
)
from guance.infrastructure.model.marvel_v2.event_encoder import encode_event  # noqa: E402
from guance.infrastructure.model.marvel_v2.history import HistoryStep  # noqa: E402
from guance.infrastructure.model.marvel_v2.trick_replay import (  # noqa: E402
    replay_trick_topology,
)
from guance.ports.model import ModelBackendError  # noqa: E402

_ALL = (0, 1, 2, 3)


def _step(event):
    return HistoryStep(tuple(tuple(row) for row in encode_event(event)))


def _play_events(actor, phase, cards):
    """Structurally valid PUBLIC_COMMIT token path for a play of ``len(cards)``.

    The replay only consumes phase/actor/card-token count; declaration semantics
    are the rules oracle's responsibility and are covered elsewhere.
    """
    tokens = ((FAMILY_TOKEN["SINGLE"],)
              + tuple(CARD_TOKEN_BASE + index for index in range(len(cards)))
              + (COMMIT_TOKEN,))
    events = [MarvelEvent("PRIVATE_PREFIX", phase, actor, tokens=(token,))
              for token in tokens[:-1]]
    events.append(MarvelEvent("PUBLIC_COMMIT", phase, actor, tokens=tokens))
    return events


def _steps_from_specs(specs):
    """specs: (kind, actor, phase, size) with kind in {"play", "pass"}."""
    events = []
    for index, (kind, actor, phase, size) in enumerate(specs):
        if index == 0:
            events.append(MarvelEvent("RESET", phase, actor, level=2))
        if kind == "pass":
            events.append(MarvelEvent("PUBLIC_COMMIT", "FOLLOW_PLAY", actor,
                                      tokens=(PASS_TOKEN,)))
        else:
            events.extend(_play_events(actor, phase, tuple(range(size))))
    return tuple(_step(event) for event in events)


def _replay(specs, counts):
    return replay_trick_topology(_steps_from_specs(specs), remaining_counts=counts)


# ---------------------------------------------------------------------------
# Truth table: closure, rotation, inherited lead, terminal, fail-closed paths.
# ---------------------------------------------------------------------------

def test_replay_anchors_first_lead_and_derives_followers():
    replay = _replay([("play", 1, "LEAD_PLAY", 1)], (27, 27, 27, 27))
    assert replay.phase == "FOLLOW_PLAY"
    assert replay.trick_open is True
    assert replay.current_leader == 1
    assert replay.current_winner == 1
    assert replay.expected_actor == 0
    assert replay.trailing_passes == 0
    assert replay.passed == frozenset()


def test_replay_tracks_passes_and_requires_matching_actor():
    replay = _replay([("play", 1, "LEAD_PLAY", 1), ("pass", 0, "FOLLOW_PLAY", 0)],
                     (27, 27, 27, 27))
    assert replay.expected_actor == 3
    assert replay.passed == frozenset({0})
    assert replay.trailing_passes == 1
    with pytest.raises(ModelBackendError) as exc:
        _replay([("play", 1, "LEAD_PLAY", 1), ("pass", 2, "FOLLOW_PLAY", 0)],
                (27, 27, 27, 27))
    assert exc.value.code == "MODEL_INPUT_UNSUPPORTED"


def test_replay_closes_trick_and_derives_winner_lead():
    replay = _replay([("play", 1, "LEAD_PLAY", 1), ("pass", 0, "FOLLOW_PLAY", 0),
                      ("pass", 3, "FOLLOW_PLAY", 0), ("pass", 2, "FOLLOW_PLAY", 0)],
                     (27, 27, 27, 27))
    assert replay.trick_open is False
    assert replay.phase == "LEAD_PLAY"
    assert replay.expected_actor == 1
    assert replay.expected_leader == 1
    assert replay.current_leader == 1
    assert replay.current_winner is None
    assert replay.passed == frozenset()
    assert replay.trailing_passes == 0


def test_replay_requires_all_alive_seats_except_winner_to_pass():
    """Failing follows/protocol actors are rejected, not repaired."""
    with pytest.raises(ModelBackendError):
        _replay([("play", 1, "LEAD_PLAY", 1), ("pass", 0, "FOLLOW_PLAY", 0),
                 ("pass", 0, "FOLLOW_PLAY", 0)], (27, 27, 27, 27))
    with pytest.raises(ModelBackendError):
        _replay([("play", 0, "LEAD_PLAY", 1), ("play", 1, "FOLLOW_PLAY", 1)],
                (27, 27, 27, 27))
    with pytest.raises(ModelBackendError):
        _replay([("pass", 0, "FOLLOW_PLAY", 0)], (27, 27, 27, 27))
    with pytest.raises(ModelBackendError):
        # A play commit without any physical card token is not a legal play.
        _replay([("play", 1, "LEAD_PLAY", 0)], (27, 27, 27, 27))


def test_replay_derives_inherited_lead_and_skips_emptied_seats():
    # Seat 3 empties with a 4-card play; the trick closes with winner 3 emptied
    # and the inherited lead is the partner (3 + 2) % 4 == 1.
    specs = [("play", 3, "LEAD_PLAY", 4), ("pass", 2, "FOLLOW_PLAY", 0),
             ("pass", 1, "FOLLOW_PLAY", 0), ("pass", 0, "FOLLOW_PLAY", 0)]
    replay = _replay(specs, (27, 27, 27, 0))
    assert replay.emptied == frozenset({3})
    assert replay.alive == frozenset({0, 1, 2})
    assert replay.trick_open is False
    assert replay.expected_leader == 1
    # The empty seat is skipped by the rotation of the next trick.
    next_replay = _replay(specs + [("play", 1, "LEAD_PLAY", 2),
                                   ("pass", 0, "FOLLOW_PLAY", 0)],
                          (27, 27, 27, 0))
    assert next_replay.trick_open is True
    assert next_replay.expected_actor == 2
    assert next_replay.passed == frozenset({0})


def test_replay_rejects_commit_after_terminal():
    # Team seats 1 and 3 are both emptied by the single play; no commit may follow.
    specs = [("play", 0, "LEAD_PLAY", 1)]
    replay = _replay(specs, (0, 0, 27, 0))
    assert replay.terminal is True
    assert replay.phase == "TERMINAL"
    assert replay.expected_actor is None
    with pytest.raises(ModelBackendError) as exc:
        _replay(specs + [("pass", 2, "FOLLOW_PLAY", 0)], (0, 0, 27, 0))
    assert exc.value.code == "MODEL_INPUT_UNSUPPORTED"


def test_replay_rejects_pass_on_closed_trick():
    specs = [("play", 1, "LEAD_PLAY", 1), ("pass", 0, "FOLLOW_PLAY", 0),
             ("pass", 3, "FOLLOW_PLAY", 0), ("pass", 2, "FOLLOW_PLAY", 0)]
    with pytest.raises(ModelBackendError):
        _replay(specs + [("pass", 1, "FOLLOW_PLAY", 0)], (27, 27, 27, 27))


def test_replay_rejects_first_commit_that_cannot_open_a_trick():
    # A pass can never open a trick (only LEAD_PLAY may), and an actor with no
    # cards at all cannot be the first lead of the window.
    with pytest.raises(ModelBackendError) as exc:
        _replay([("pass", 0, "FOLLOW_PLAY", 0)], (27, 27, 27, 27))
    assert exc.value.code == "MODEL_INPUT_UNSUPPORTED"


def test_replay_rejects_corrupted_remaining_bounds():
    with pytest.raises(ModelBackendError):
        replay_trick_topology(_steps_from_specs([("play", 0, "LEAD_PLAY", 1)]),
                              remaining_counts=(27, 27, 27))
    with pytest.raises(ModelBackendError):
        replay_trick_topology(_steps_from_specs([("play", 0, "LEAD_PLAY", 1)]),
                              remaining_counts=(27, 27, 27, 29))


# ---------------------------------------------------------------------------
# Differential: replay vs the real domain state engine.
# ---------------------------------------------------------------------------

def _apply_action(state, *, actor, kind, cards=()):
    """Ask the domain itself for the legal post-action state via atomic clones."""
    for next_player in (*_ALL, None):
        resets = (None,) if kind == "play" else (None, 0, 1, 2)
        for reset in resets:
            candidate = state.clone()
            try:
                candidate.apply_action(ActionEvent(
                    player=actor, kind=kind, cards=tuple(cards),
                    combo_type=1 if kind == "play" else None,
                    next_player=next_player, reset_flag=reset))
            except StateError:
                continue
            return candidate
    return None


def _fuzz_game(seed):
    rng = random.Random(seed)
    deck = full_deck()
    supplies = [list(deck[0:27]), list(deck[27:54]), list(deck[54:81]),
                list(deck[81:108])]
    state = GameState(f"trick-replay-diff-{seed}")
    state.apply_deal(DealEvent(my_seat=-1, level=1 + (seed % 13), hand=tuple(deck[:27])))
    state.apply_start(StartEvent(first_seat=seed % 4))
    events = []
    comparisons = 0
    terminal_observed = False
    while state.phase == "active" and len(events) < 512:
        actor = state.current_seat
        free_lead = state.snapshot().free_lead
        if not free_lead and rng.random() < 0.4:
            kind, cards = "pass", ()
        else:
            remaining = state.remaining[actor]
            pool = supplies[actor]
            if remaining <= 0 or not pool:
                break
            size = min(remaining, len(pool), rng.choice((1, 1, 2, 2, 3, 4, 5, 7)))
            kind, cards = "play", tuple(pool[:size])
            del pool[:size]
        phase = "LEAD_PLAY" if free_lead else "FOLLOW_PLAY"
        applied = _apply_action(state, actor=actor, kind=kind, cards=cards)
        if applied is None:
            break
        state = applied
        if kind == "pass":
            events.append(MarvelEvent("PUBLIC_COMMIT", "FOLLOW_PLAY", actor,
                                      tokens=(PASS_TOKEN,)))
        else:
            events.extend(_play_events(actor, phase, cards))
        steps = tuple(_step(event) for event in events)
        replay = replay_trick_topology(steps, remaining_counts=tuple(state.remaining))
        assert replay.phase == state._v2_phase.value
        assert replay.terminal == (state.phase != "active")
        assert replay.trick_open == (state.lead_action is not None)
        assert replay.expected_actor == state.current_seat
        assert replay.expected_leader == (state.current_seat
                                          if replay.trick_open is False
                                          and replay.terminal is False else None)
        assert replay.passed == frozenset(state._passed)
        assert replay.trailing_passes == state.consecutive_passes
        assert replay.current_leader == state.leader_seat
        assert replay.current_winner == (state._v2_winning.actor
                                         if state._v2_winning is not None else None)
        assert replay.alive == frozenset(seat for seat in _ALL
                                         if state.remaining[seat] > 0)
        assert replay.emptied == frozenset(seat for seat in _ALL
                                           if state.remaining[seat] == 0)
        comparisons += 1
        terminal_observed = terminal_observed or replay.terminal
    return comparisons, terminal_observed


def test_replay_matches_domain_state_rules_differential():
    total = 0
    terminals = 0
    for seed in range(24):
        comparisons, terminal = _fuzz_game(seed)
        total += comparisons
        terminals += int(terminal)
    assert total > 400, total
    assert terminals > 0, terminals
