from __future__ import annotations

import guance.domain.session as session_module
from guance.contracts import PublicAction, SourceFrame
from guance.domain.events import ActionEvent, DealEvent, InformationalEvent, SettlementEvent, StartEvent
from guance.domain.cards import full_deck
from guance.domain.session import SessionEngine
from guance.infrastructure.protocol.live import GAME_HOST


def frame(payload: bytes, sequence: int, direction: str) -> SourceFrame:
    return SourceFrame(
        host=GAME_HOST,
        payload=payload,
        received_at=float(sequence),
        connection_id="connection-1",
        sequence=sequence,
        direction=direction,
    )


def test_client_frames_are_transport_only_and_do_not_enter_parser(monkeypatch) -> None:
    def unexpected_parse(_payload: bytes, _meta=None):
        raise AssertionError("client frame must not enter protocol parser")

    monkeypatch.setattr(session_module, "parse_aggregate", unexpected_parse)
    engine = SessionEngine("test")

    assert engine.feed(frame(b"client", 1, "client")) == []
    assert engine.state is None
    assert not engine.paused


def test_unknown_direction_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(session_module, "parse_aggregate", lambda _payload, _meta=None: ())
    engine = SessionEngine("test")

    updates = engine.feed(frame(b"unknown", 1, "unknown"))

    assert len(updates) == 1
    assert updates[0].kind == "error"
    assert updates[0].error_code == "frame_direction_unknown"
    assert engine.paused
    assert engine.feed(frame(b"server", 2, "server"))[0].error_code == "engine_paused"


def test_unconfirmed_seat_suppresses_observation_until_own_public_play(
    monkeypatch,
) -> None:
    hand = tuple(full_deck()[:27])
    opponent_card = full_deck()[27]
    events = {
        b"deal": (DealEvent(my_seat=-1, level=5, hand=hand),),
        b"start": (StartEvent(first_seat=1),),
        b"opponent-play": (
            ActionEvent(
                player=1,
                kind="play",
                cards=(opponent_card,),
                combo_type=1,
                next_player=0,
                remaining_after=26,
            ),
        ),
    }
    monkeypatch.setattr(session_module, "parse_aggregate", lambda payload, _meta=None: events[payload])
    engine = SessionEngine("test")

    engine.feed(frame(b"deal", 1, "server"))
    start_updates = engine.feed(frame(b"start", 2, "server"))

    assert not any(update.kind == "observation" for update in start_updates)
    assert engine.latest_observation is None
    assert engine.state is not None
    assert engine.state.my_seat == -1
    assert engine.state.current_seat == 1

    action_updates = engine.feed(frame(b"opponent-play", 3, "server"))

    assert engine.state is not None
    assert engine.state.hand == set(hand)
    assert engine.state.current_seat == 0
    assert engine.state.my_seat == -1
    assert not any(update.kind == "observation" for update in action_updates)
    assert engine.latest_observation is None


def _event_engine(monkeypatch, events_by_payload: dict[bytes, tuple[object, ...]]) -> SessionEngine:
    monkeypatch.setattr(session_module, "parse_aggregate",
                        lambda payload, _meta=None: events_by_payload[payload])
    return SessionEngine("multi-game")


def _feed(engine: SessionEngine, payload: bytes, sequence: int, connection: str = "connection-1"):
    return engine.feed(SourceFrame(host=GAME_HOST, payload=payload, received_at=float(sequence),
                                   connection_id=connection, sequence=sequence, direction="server"))


def _seed_terminal_state(engine: SessionEngine) -> SettlementEvent:
    """Create a coherent near-terminal fixture without replaying 100+ actions."""
    assert engine.state is not None
    state = engine.state
    deck = full_deck()
    state.remaining = [0, 1, 0, 1]
    state.my_seat = 0
    state.hand.clear()
    state._played_by_seat = [
        list(deck[0:27]),
        list(deck[27:53]),
        list(deck[54:81]),
        list(deck[81:107]),
    ]
    state._played_public = set(deck[0:27] + deck[27:53] + deck[54:81] + deck[81:107])
    state._finished = [0, 2]
    state.current_seat = 1
    state.lead_action = PublicAction(index=0, player=1, kind="play", cards=(deck[27],),
                                      combo_type=1, next_player=1, trick=0)
    state.free_lead = False
    return SettlementEvent(
        ranks_by_seat=(1, 3, 2, 4),
        remaining_hands=((), (), (), (deck[107],)),
    )


def test_last_action_ends_game_clears_observation_and_settlement_is_idempotent(monkeypatch) -> None:
    hand = tuple(full_deck()[:27])
    settlement = SettlementEvent(ranks_by_seat=(1, 3, 2, 4), remaining_hands=((), (), (), (full_deck()[107],)))
    events = {
        b"deal": (DealEvent(my_seat=-1, level=5, hand=hand),),
        b"start": (StartEvent(first_seat=1),),
        b"last": (ActionEvent(player=1, kind="play", cards=(full_deck()[53],),
                               combo_type=1, next_player=None, remaining_after=0),),
        b"settle": (settlement,),
    }
    engine = _event_engine(monkeypatch, events)

    _feed(engine, b"deal", 1)
    _feed(engine, b"start", 2)
    # Seed only the bounded terminal fixture after the normal deal/start path.
    expected_settlement = _seed_terminal_state(engine)
    last_updates = _feed(engine, b"last", 3)

    assert engine.state is not None
    assert engine.state.phase == "ended"
    assert engine.latest_observation is None
    assert any(update.data.get("phase") == "ended" for update in last_updates if update.kind == "state")
    assert next(update for update in last_updates if update.kind == "state").data["source_sequence"] == 3

    # Use the coherent settlement generated from the same state.
    events[b"settle"] = (expected_settlement,)
    settle_updates = _feed(engine, b"settle", 4)
    assert engine.state.phase == "settled"
    settled_version = engine.state.state_version
    assert engine.latest_observation is None
    assert any(update.data.get("phase") == "settled" for update in settle_updates if update.kind == "state")

    duplicate_updates = _feed(engine, b"settle", 5)
    assert engine.state.phase == "settled"
    assert engine.state.state_version == settled_version
    assert not any(update.kind == "error" for update in duplicate_updates)


def test_same_connection_deal_after_settled_creates_unique_next_game(monkeypatch) -> None:
    hand = tuple(full_deck()[:27])
    events = {
        b"deal-1": (DealEvent(my_seat=-1, level=5, hand=hand),),
        b"start-1": (StartEvent(first_seat=1),),
        b"last-1": (ActionEvent(player=1, kind="play", cards=(full_deck()[53],),
                                 combo_type=1, next_player=None, remaining_after=0),),
        b"deal-2": (DealEvent(my_seat=-1, level=6, hand=tuple(full_deck()[27:54])),),
    }
    engine = _event_engine(monkeypatch, events)

    _feed(engine, b"deal-1", 1)
    _feed(engine, b"start-1", 2)
    _seed_terminal_state(engine)
    _feed(engine, b"last-1", 3)
    assert engine.state.phase == "ended"

    deal_updates = _feed(engine, b"deal-2", 4)
    assert engine.state.phase == "waiting_start"
    assert engine.state.game_id == "game-0002"
    assert engine.latest_observation is None
    assert any(update.data.get("game_id") == "game-0002" for update in deal_updates if update.kind == "state")
    assert next(update for update in deal_updates if update.kind == "state").data["source_sequence"] == 4


def test_new_deal_during_active_game_fails_closed_without_replacement(monkeypatch) -> None:
    hand = tuple(full_deck()[:27])
    events = {
        b"deal": (DealEvent(my_seat=-1, level=5, hand=hand),),
        b"start": (StartEvent(first_seat=0),),
        b"unexpected-deal": (DealEvent(my_seat=-1, level=6, hand=tuple(full_deck()[27:54])),),
    }
    engine = _event_engine(monkeypatch, events)

    _feed(engine, b"deal", 1)
    _feed(engine, b"start", 2)
    old_id = engine.state.game_id
    updates = _feed(engine, b"unexpected-deal", 3)

    assert engine.state is not None
    assert engine.state.game_id == old_id
    assert engine.paused
    assert updates[0].error_code == "state_invalid"


def test_connection_change_after_terminal_accepts_next_deal_but_active_change_pauses(monkeypatch) -> None:
    hand = tuple(full_deck()[:27])
    events = {
        b"deal": (DealEvent(my_seat=-1, level=5, hand=hand),),
        b"start": (StartEvent(first_seat=0),),
        b"next-deal": (DealEvent(my_seat=-1, level=6, hand=tuple(full_deck()[27:54])),),
    }
    engine = _event_engine(monkeypatch, events)
    _feed(engine, b"deal", 1, "conn-a")
    _feed(engine, b"start", 2, "conn-a")
    # A live connection switch cannot be safely spliced into an active game.
    updates = _feed(engine, b"next-deal", 1, "conn-b")
    assert engine.paused
    assert updates[0].error_code == "capture_paused"

    # A separate engine demonstrates the safe terminal boundary.
    engine = _event_engine(monkeypatch, events)
    _feed(engine, b"deal", 1, "conn-a")
    _feed(engine, b"start", 2, "conn-a")
    _seed_terminal_state(engine)
    # Directly mark the bounded fixture terminal to exercise the transport boundary.
    engine.state.phase = "settled"
    updates = _feed(engine, b"next-deal", 1, "conn-b")
    assert not engine.paused
    assert engine.state.game_id == "game-0002"
    assert any(update.kind == "state" for update in updates)


def test_state_update_exposes_unconfirmed_table_before_first_own_play(monkeypatch) -> None:
    hand = tuple(full_deck()[:27])
    events = {
        b"deal": (DealEvent(my_seat=-1, level=5, hand=hand),),
        b"start": (StartEvent(first_seat=1),),
        b"opponent": (ActionEvent(
            player=1, kind="play", cards=(full_deck()[27],), combo_type=1,
            next_player=0, remaining_after=26,
        ),),
    }
    engine = _event_engine(monkeypatch, events)

    deal_updates = _feed(engine, b"deal", 1)
    deal_state = next(update for update in deal_updates if update.kind == "state")
    assert deal_state.data["table"]["phase"] == "waiting_start"
    assert deal_state.data["table"]["my_seat"] == -1
    assert deal_state.data["table"]["seat_confirmed"] is False
    assert deal_state.data["table"]["counter_unseen_total"] == 108

    _feed(engine, b"start", 2)
    opponent_updates = _feed(engine, b"opponent", 3)
    table = next(update for update in opponent_updates if update.kind == "state").data["table"]
    assert table["current_seat"] == 0
    assert table["my_seat"] == -1
    assert table["seat_confirmed"] is False
    assert table["history"][0]["player"] == 1
    assert engine.latest_observation is None


def test_new_deal_resets_table_counter_and_history_for_next_game(monkeypatch) -> None:
    hand_one = tuple(full_deck()[:27])
    hand_two = tuple(full_deck()[27:54])
    events = {
        b"deal-one": (DealEvent(my_seat=-1, level=5, hand=hand_one),),
        b"deal-two": (DealEvent(my_seat=-1, level=9, hand=hand_two),),
    }
    engine = _event_engine(monkeypatch, events)

    first = next(update for update in _feed(engine, b"deal-one", 1)
                 if update.kind == "state").data["table"]
    engine.reset()
    second = next(update for update in _feed(engine, b"deal-two", 2)
                  if update.kind == "state").data["table"]

    assert first["game_id"] == "game-0001"
    assert second["game_id"] == "game-0002"
    assert second["level"] == 9
    assert second["hand"] == sorted(hand_two)
    assert second["history"] == []
    assert second["counter_unseen_total"] == 108


def test_3003_evidence_updates_display_table_without_advancing_game_state(monkeypatch) -> None:
    hand = tuple(full_deck()[:27])
    events = {
        b"deal": (DealEvent(my_seat=-1, level=5, hand=hand),),
        b"evidence": (InformationalEvent(
            command=3003,
            reported_seat_snapshot=(1, 1, 1, 1),
            reported_values=(1.0, 1.0),
            reported_flag=1,
        ),),
    }
    engine = _event_engine(monkeypatch, events)
    _feed(engine, b"deal", 1)
    before = engine.state.state_version

    updates = _feed(engine, b"evidence", 2)
    table = next(update for update in updates if update.kind == "state").data["table"]

    assert engine.state.state_version == before
    assert table["multiplier_evidence"] == {
        "reported_seat_snapshot": [1, 1, 1, 1],
        "reported_values": [1.0, 1.0],
        "reported_flag": 1,
    }
    assert not any(update.kind == "observation" for update in updates)
