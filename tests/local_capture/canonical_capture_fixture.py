from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from guance.application.controller import DebugFrameRecorder
from guance.contracts import SourceFrame
from guance.domain.cards import full_deck
from guance.domain.events import DealEvent, SourceMeta, StartEvent
from guance.domain.state import GameState
from guance.infrastructure.capture.canonical_capture import (
    CanonicalCaptureConverter, ConfirmedDecisionInput,
)
from guance.infrastructure.model.marvel_v2.action_decoder import ActionCandidate
from guance.infrastructure.model.marvel_v2.action_protocol import ActionProtocolSession
from guance.infrastructure.model.marvel_v2.contracts import (
    ActionDeclaration, CardMapping, ExchangeFacts, MarvelEvent, MarvelState, card_face,
)
from guance.infrastructure.model.marvel_v2.history import ExactHistoryJournal
from guance.infrastructure.model.marvel_v2.lifecycle_contracts import CacheRequestIdentity
from guance.public_state_contract import (
    ExchangeState, Knowledge, guandan_card_id_to_wire, wire_card_to_guandan_id,
)

ARTIFACT_SHA = "a" * 64


def create_capture(tmp_path: Path):
    recorder = DebugFrameRecorder(tmp_path / "captures")
    source = recorder.start()
    capture_id = source.parent.name
    recorder.append(SourceFrame(
        host="hlxyxws.huanle.qq.com", payload=b"synthetic-frame-1", received_at=1.0,
        connection_id="connection-1", sequence=1, direction="server",
    ))
    recorder.begin_game("game-one", 1)
    converter = CanonicalCaptureConverter(source.parent, capture_id)
    return recorder, converter, source, capture_id


def _public_state(state_version_delta: int = 0):
    meta = SourceMeta(sequence=0, connection_id="synthetic-connection", message_index=0, sub_index=0)
    game = GameState("game-one")
    game.apply_deal(DealEvent(-1, 5, full_deck()[:27], meta=meta))
    game.apply_start(StartEvent(0, meta=replace(meta, sequence=1)))
    game.confirm_my_seat_relative("self")
    value = game.public_state_v2("session-one")
    evidence = value.observer_seat.evidence
    assert evidence is not None
    value = replace(value,
        previous_result=Knowledge.absent(evidence),
        exchange=ExchangeState(*(Knowledge.absent(evidence) for _ in range(6))),
    )
    if state_version_delta:
        value = replace(value, identity=replace(
            value.identity,
            state_version=value.identity.state_version + state_version_delta,
            public_state_revision=value.identity.public_state_revision + state_version_delta,
        ))
    value.validate()
    return value


def decision_input(capture_id: str, *, frame_sequence: int = 1,
                   state_version_delta: int = 0, request_sequence: int = 1):
    public_state = _public_state(state_version_delta)
    own_hand = tuple(wire_card_to_guandan_id(card) for card in public_state.own_hand.value)
    rank, _suit, _deck = card_face(own_hand[0])
    physical = CardMapping.to_wire(own_hand[0])
    declaration = ActionDeclaration(
        family="SINGLE", declared_ranks=(rank,), comparison_rank=rank, suit=None, length=1,
        canonical_cards=(physical,), canonical_action_cards=(physical,),
    )
    candidate = ActionCandidate.play("LEAD_PLAY", declaration, 5)
    history = ExactHistoryJournal.from_events(
        session_id="session-one", game_id="game-one",
        state_id=f"state-{public_state.identity.state_version}",
        artifact_id="iter000100", artifact_sha256=ARTIFACT_SHA,
        events=(MarvelEvent("RESET", "LEAD_PLAY", 0, level=5),),
    )
    session = ActionProtocolSession(
        "session-one", public_state.identity.state_version, "LEAD_PLAY", own_hand,
        (candidate,), public_state, token_step=history.identity.token_step,
        committed_step=history.identity.committed_step,
    )
    current = MarvelState(
        observer_seat=0, active_seat=0, leader_seat=0, current_winner_seat=None,
        phase="LEAD_PLAY", level=5, own_hand=own_hand, current_winning_cards=(),
        self_exchange_cards=(), private_prefix=(), remaining_counts=(27, 27, 27, 27),
        finishing_ranks=(0, 0, 0, 0), emptied_seats=(), consecutive_passes=0,
        current_family=None, current_comparison_rank=None, current_winning_size=0,
        previous_result=None, exchange=ExchangeFacts(), token_step=history.identity.token_step,
        committed_step=history.identity.committed_step, history_complete=True,
    )
    request = CacheRequestIdentity.create(
        "lifecycle-one", f"decision-{request_sequence}", request_sequence, 0,
        history.identity,
    )
    return ConfirmedDecisionInput(
        capture_id=capture_id, frame_sequence=frame_sequence,
        source_connection_ordinal=1, source_sequence=frame_sequence,
        public_state=public_state, marvel_state=current, history=history,
        action_session=session, cache_request=request, transitions=(),
    )


def finish_capture(recorder: DebugFrameRecorder, sequence: int = 1) -> None:
    recorder.finish_game("game-one", sequence)
    recorder.close()

def record_confirmed(converter: CanonicalCaptureConverter, item: ConfirmedDecisionInput) -> str:
    converter.observe_decision_opportunity(
        frame_sequence=item.frame_sequence,
        state_version=item.public_state.identity.state_version,
        public_action_index=len(item.public_state.history.actions),
    )
    return converter.record_decision(item)
