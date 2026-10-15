"""GMV2-S2-004/007/008: injected synthetic transport, no model/capture IO."""
from dataclasses import replace

import pytest

from guance.contracts import SourceFrame
from guance.domain.cards import full_deck
from guance.domain.events import (
    ActionEvent, DealEvent, DeclaredActionCommitted, InformationalEvent, PhaseObserved,
    PublicResultObserved, SettlementEvent, StartEvent,
)
from guance.domain.session import SessionEngine
from guance.public_state_contract import (
    KnowledgeStatus as KS, PublicDeclaration, WildAssignment,
    wire_rank_to_guandan_rank,
)

DECK = full_deck()
EVENTS = {
    b"deal": (DealEvent(-1, 5, DECK[:27]),),
    b"start": (StartEvent(0),),
    b"play": (ActionEvent(0, "play", DECK[:27], combo_type=1, next_player=3, finish_rank=1),),
    b"pass": (ActionEvent(3, "pass", next_player=2, reset_flag=0),),
    b"end": (ActionEvent(2, "play", DECK[54:81], combo_type=1, next_player=None, finish_rank=2),),
    b"settle": (SettlementEvent((1, 3, 2, 4), ((), DECK[27:54], (), DECK[81:])),),
    b"info": (InformationalEvent(3003),),
    b"reconnect": (InformationalEvent(3010),),
    b"phase": (PhaseObserved("TRIBUTE", 0),),
    b"bad-state": (StartEvent(0, False),),
}


def engine():
    def parse(payload, meta):
        if payload not in EVENTS:
            raise ValueError("SECRET_PARSER_PAYLOAD_MUST_NOT_BE_ECHOED")
        return tuple(replace(event, meta=meta) for event in EVENTS[payload])
    return SessionEngine("synthetic-session", aggregate_parser=parse, accepted_host="synthetic-host")


def feed(target, payload, sequence, connection="synthetic-connection", direction="server"):
    return target.feed(SourceFrame("synthetic-host", payload, 0., connection, sequence, direction))


def settled(connection="synthetic-connection"):
    target = engine()
    for seq, payload in enumerate((b"deal", b"start", b"play", b"pass", b"end", b"settle")):
        updates = feed(target, payload, seq, connection)
        assert not any(u.kind == "error" for u in updates)
    return target


def test_public_projection_available_before_seat_and_on_other_turn_terminal():
    target = engine()
    feed(target, b"deal", 0)
    assert target.latest_observation is None
    assert target.latest_public_state_v2.observer_seat.status is KS.UNKNOWN
    feed(target, b"start", 1)
    target.confirm_my_seat_relative("self")
    assert target.latest_public_state_v2.observer_seat.value == 0
    feed(target, b"play", 2)
    assert target.latest_public_state_v2.active_seat.value == 3
    feed(target, b"pass", 3)
    feed(target, b"end", 4)
    assert target.latest_observation is None
    assert target.latest_public_state_v2.phase.value == "TERMINAL"


def test_same_connection_settlement_information_deal_keeps_previous_unknown():
    target = settled()
    terminal = target.latest_public_state_v2
    candidate = target.state.settled_result_candidate().value
    assert terminal.previous_result.status is KS.UNKNOWN
    feed(target, b"info", 6)
    assert target.latest_public_state_v2 == terminal
    feed(target, b"deal", 7)
    view = target.latest_public_state_v2
    assert view.identity.game_id != terminal.identity.game_id
    assert view.previous_result.status is KS.UNKNOWN
    assert view.history.actions == ()
    assert view.history.committed_step.status is KS.UNSUPPORTED

    EVENTS[b"explicit-previous"] = (PublicResultObserved(candidate),)
    try:
        feed(target, b"explicit-previous", 8)
    finally:
        del EVENTS[b"explicit-previous"]
    result = target.latest_public_state_v2.previous_result.value
    assert result.source_game_id.value == terminal.identity.game_id
    assert result.ranking_by_seat == (1, 3, 2, 4)
    assert result.winner_team == 0 and result.outcome_class == "DOUBLE_DOWN"
    assert result.tribute_cancelled.status is KS.UNSUPPORTED


@pytest.mark.parametrize("method", ["switch", "reset", "missing_connection"])
def test_previous_result_not_reused_across_uncertain_connection_or_reset(method):
    target = settled()
    if method == "reset":
        target.reset()
        assert target.latest_public_state_v2 is None
        feed(target, b"deal", 0)
    else:
        feed(target, b"deal", 0, "other-connection" if method == "switch" else "")
    assert not target.paused
    assert target.latest_public_state_v2.previous_result.status is KS.UNKNOWN


def test_missing_connection_cannot_associate_previous():
    target = settled("")
    feed(target, b"deal", 6, "")
    assert target.latest_public_state_v2.previous_result.status is KS.UNKNOWN


@pytest.mark.parametrize("first,second,expected", [(0, 0, "frame_sequence_repeated"),
                                                 (1, 0, "frame_sequence_repeated"),
                                                 (0, 2, "frame_sequence_gap")])
def test_sequence_zero_backward_and_gap_clear_public_projection(first, second, expected):
    target = engine()
    feed(target, b"deal", first)
    old = target.latest_public_state_v2
    updates = feed(target, b"start", second)
    assert target.paused and target.latest_public_state_v2 is None
    assert target.latest_observation is None
    assert updates[0].error_code == expected
    assert old.phase.status is KS.UNKNOWN  # detached historical value, not current


@pytest.mark.parametrize("payload,code", [(b"unknown", "protocol_rejected"),
                                         (b"bad-state", "state_invalid")])
def test_unknown_or_state_failure_clears_latest(payload, code):
    target = engine()
    feed(target, b"deal", 0)
    before = target.state.snapshot()
    updates = feed(target, payload, 1)
    assert target.latest_public_state_v2 is None
    assert target.paused
    assert updates[0].error_code == code
    assert "SECRET" not in repr(updates)
    assert target.state.snapshot() == before


@pytest.mark.parametrize("connection", ["different-connection", ""])
def test_live_connection_change_invalidates_instead_of_reusing_state(connection):
    target = engine()
    feed(target, b"deal", 0)
    updates = feed(target, b"start", 1, connection)
    assert target.paused and target.latest_public_state_v2 is None
    assert updates[0].error_code == "capture_paused"


def test_client_frames_count_for_local_sequence_but_not_semantics():
    target = engine()
    feed(target, b"deal", 0)
    before = target.latest_public_state_v2
    assert feed(target, b"must-not-parse", 1, direction="client") == []
    assert target.latest_public_state_v2 == before
    feed(target, b"start", 2)
    assert not target.paused
    assert target.latest_public_state_v2.history.transport_contiguous.status is KS.UNSUPPORTED


def test_information_does_not_change_revision_or_invent_exchange_phase():
    target = engine()
    feed(target, b"deal", 0)
    before = target.latest_public_state_v2
    feed(target, b"info", 1)
    assert target.latest_public_state_v2 == before
    feed(target, b"phase", 2)
    assert target.latest_public_state_v2.phase.value == "TRIBUTE"
    assert target.latest_public_state_v2.identity.state_version == before.identity.state_version
    assert target.latest_public_state_v2.identity.public_state_revision > before.identity.public_state_revision


def test_lazy_parser_error_does_not_apply_prefix_or_echo_payload():
    def parse(payload, meta):
        yield replace(EVENTS[b"deal"][0], meta=meta)
        raise ValueError("PRIVATE_SENTINEL")
    target = SessionEngine("synthetic-session", aggregate_parser=parse)
    updates = feed(target, b"anything", 0)
    assert target.state is None
    assert target.latest_public_state_v2 is None
    assert "PRIVATE_SENTINEL" not in repr(updates)


def test_state_failure_later_in_frame_never_returns_earlier_trusted_updates():
    target = SessionEngine("synthetic-session", aggregate_parser=lambda payload, meta:
                           (replace(EVENTS[b"deal"][0], meta=meta), StartEvent(0, False, meta)))
    updates = feed(target, b"anything", 0)
    assert [update.kind for update in updates] == ["error"]
    assert target.latest_public_state_v2 is None


def test_3010_is_informational_and_does_not_impersonate_a_reconnect_marker():
    target = settled()
    state = target.state
    projection = target.latest_public_state_v2
    assert feed(target, b"reconnect", 6) == []
    assert target.state is state
    assert target.latest_public_state_v2 == projection
    feed(target, b"deal", 7)
    assert target.latest_public_state_v2.previous_result.status is KS.UNKNOWN


@pytest.mark.parametrize("assigned_suit", [pytest.param([], id="assigned-suit-list"),
                                            pytest.param({}, id="assigned-suit-dict")])
def test_invalid_assigned_suit_session_failure_clears_projection_without_mutation(assigned_suit):
    card = DECK[0]
    rank = wire_rank_to_guandan_rank(card & 15)
    declaration = PublicDeclaration(
        "SINGLE", (rank,), rank, None, 1, (card,),
        (WildAssignment(card, rank, assigned_suit, True),),
        canonical_action_cards=(card,),
    )

    def parse(payload, meta):
        if payload == b"deal-invalid-suit":
            return (DealEvent(-1, 5, DECK[:27], meta=meta),)
        if payload == b"start-invalid-suit":
            return (StartEvent(0, meta=meta),)
        if payload == b"invalid-suit-action":
            action = ActionEvent(
                0, "play", (card,), combo_type=903, next_player=3,
                remaining_after=26, meta=meta,
            )
            return (DeclaredActionCommitted(action, declaration),)
        raise ValueError("PRIVATE_SESSION_INPUT_SENTINEL")

    target = SessionEngine(
        "synthetic-session", aggregate_parser=parse, accepted_host="synthetic-host",
    )
    assert not any(update.kind == "error"
                   for update in feed(target, b"deal-invalid-suit", 0))
    assert not any(update.kind == "error"
                   for update in feed(target, b"start-invalid-suit", 1))
    target.confirm_my_seat_relative("self")
    assert target.latest_public_state_v2 is not None

    state = target.state
    before = {
        "snapshot": state.snapshot(),
        "hand": frozenset(state.hand),
        "remaining": tuple(state.remaining),
        "legacy_history": tuple(state.history),
        "v2_history": tuple(state._v2_history),
        "winning": state._v2_winning,
        "state_version": state.state_version,
        "public_state_revision": state.public_state_revision,
        "projection": state.public_state_v2(target.session_id),
    }
    updates = feed(target, b"invalid-suit-action", 2)
    after = {
        "snapshot": state.snapshot(),
        "hand": frozenset(state.hand),
        "remaining": tuple(state.remaining),
        "legacy_history": tuple(state.history),
        "v2_history": tuple(state._v2_history),
        "winning": state._v2_winning,
        "state_version": state.state_version,
        "public_state_revision": state.public_state_revision,
        "projection": state.public_state_v2(target.session_id),
    }
    assert before == after
    assert target.paused
    assert target.latest_public_state_v2 is None
    assert target.latest_observation is None
    assert [update.kind for update in updates] == ["error"]
    assert updates[0].error_code == "state_invalid"
    assert updates[0].message == "declared action semantic invalid"
    assert "PRIVATE_SESSION_INPUT_SENTINEL" not in repr(updates)
    assert "assigned_suit" not in repr(updates)
