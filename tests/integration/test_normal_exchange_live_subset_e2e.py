from __future__ import annotations

import json
from pathlib import Path

import pytest

from guance.application.controller import GuanceController
from guance.infrastructure.capture.live_subset import NormalExchangeLiveSubsetProvider
from guance.contracts import SourceFrame
from guance.infrastructure.capture.canonical_capture import CanonicalCaptureConverter, validate_v2_candidate
from guance.infrastructure.protocol.live import COMMAND_VARIANTS, GAME_HOST, parse_aggregate
from guance.presentation.viewmodel import GuanceViewModel
from guance.public_state_contract import guandan_card_id_to_wire
from tests.protocol.test_live_protocol import aggregate, field, record, varint

# Frozen materialized runtime source (hash-verified GD-RULES, Phase 2 evidence);
# the live Guandan worktree is deliberately never used as a rules/runtime root.
SOURCE = Path(r"D:\project\guance\build\acceptance\guance-model-platform-20261016"
              r"\phase2\runtime\marvel-e5c82e7c-d7acbc8d\source")
pytestmark = pytest.mark.skipif(not SOURCE.is_dir(), reason="verified Guandan source unavailable")
SENTINEL = (1 << 64) - 1


class FakeModel:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def invalidate(self) -> None:
        self.calls.append("invalidate")

    def submit(self, _observation) -> None:
        self.calls.append("submit")

    def close(self) -> None:
        self.calls.append("close")

    def start(self) -> None:
        self.calls.append("start")


def paths(tmp_path: Path) -> dict[str, Path]:
    data = tmp_path / "data"
    return {
        "data_dir": data, "config_dir": data / "config", "logs_dir": data / "logs",
        "cert_dir": data / "config" / "mitmproxy", "runtime_dir": tmp_path / "runtime",
        "exports_dir": data / "exports", "models_dir": data / "models",
        "root_dir": tmp_path,
    }


def packed(values) -> bytes:
    return b"".join(varint(value) for value in values)


def deal_body(hand_ids: list[int], *, level: int = 5) -> bytes:
    hand = [guandan_card_id_to_wire(card_id) for card_id in hand_ids]
    return (
        field(1, 0, 27) + field(2, 2, packed(hand)) + field(3, 0, 1)
        + field(4, 0, level) + field(5, 2, b"opaque") + field(6, 2, b"")
    )


def observer_body(seat: int, hand_ids: list[int]) -> bytes:
    cards = packed(guandan_card_id_to_wire(card_id) for card_id in hand_ids)
    entry = field(1, 0, len(hand_ids)) + field(2, 2, cards)
    return field(2, 0, seat) + field(3, 2, field(1, 2, entry))


def start_body(first_seat: int) -> bytes:
    first = b"" if first_seat == 0 else field(1, 0, first_seat)
    return first + field(2, 0, 20) + field(3, 0, 2) + field(4, 0, 35)


def exchange_init_body() -> bytes:
    return field(1, 0, 10) + field(2, 0, 2) + field(3, 2, b"\x0e\x0a\x0e\x0a")


def information_3002_body() -> bytes:
    return field(1, 0, 1000) + field(3, 0, 1) + field(4, 2, packed((2, 20, 13, 21)))


def play_body(actor: int, next_actor: int | None, cards: list[int], *, remaining: int,
              combo: int, finish: int | None = None) -> bytes:
    body = field(2, 0, actor) + field(3, 0, SENTINEL if next_actor is None else next_actor)
    if finish is not None:
        body = field(1, 0, finish) + body
    body += field(4, 0, combo)
    if remaining:
        body += field(5, 0, remaining)
    body += (
        field(6, 0, len(cards))
        + field(7, 2, packed(guandan_card_id_to_wire(card_id) for card_id in cards))
        + field(8, 0, 1) + field(9, 0, 1) + field(11, 2, b"x" * 20)
    )
    return body


def pass_body(actor: int, next_actor: int, *, reset: int | None = None) -> bytes:
    body = field(2, 0, actor) + field(3, 0, next_actor)
    if reset is not None:
        body += field(4, 0, reset)
    return body + field(5, 0, 1)


def hand_blob(cards: list[int]) -> bytes:
    if not cards:
        return field(1, 0, 0)
    return field(1, 0, len(cards)) + field(
        2, 2, packed(guandan_card_id_to_wire(card_id) for card_id in cards),
    )


def settlement_body(ranks: tuple[int, int, int, int], hands: list[list[int]]) -> bytes:
    body = field(2, 0, 1) + field(3, 2, packed(ranks))
    body += field(8, 0, 1) + field(9, 0, 1)
    body += field(10, 2, b"") + field(11, 2, b"") + field(12, 2, b"")
    body += b"".join(field(13, 2, hand_blob(hand)) for hand in hands)
    body += field(15, 2, b"") + field(16, 0, 1) + field(17, 2, b"") + field(19, 2, b"")
    body += b"".join(field(21, 2, b"") for _ in range(4))
    body += b"".join(field(22, 2, b"") for _ in range(4))
    return body


def tribute_3012(donor: int, card_id: int, recipient: int, private_after: list[int]) -> bytes:
    card = guandan_card_id_to_wire(card_id)
    return (
        field(2, 0, donor) + field(3, 0, card)
        + field(4, 2, packed((card, 0)))
        + field(5, 2, packed((recipient, SENTINEL)))
        + field(6, 2, packed(guandan_card_id_to_wire(item) for item in private_after))
    )


def tribute_3013(donor: int, card_id: int, recipient: int) -> bytes:
    entry = field(1, 0, guandan_card_id_to_wire(card_id)) + field(2, 0, donor)
    if recipient:
        entry += field(3, 0, recipient)
    return field(1, 0, 20) + field(2, 2, b"\x0f\x0c\x0f\x0c") + field(3, 2, entry)


def return_3014(source: int, card_id: int, target: int, private_after: list[int]) -> bytes:
    body = b"" if source == 0 else field(2, 0, source)
    body += field(3, 0, guandan_card_id_to_wire(card_id))
    if target:
        body += field(4, 0, target)
    body += field(5, 2, packed(guandan_card_id_to_wire(item) for item in private_after))
    return body


def summary_3015(tribute_id: int, donor: int, return_id: int, returner: int) -> bytes:
    cards = (guandan_card_id_to_wire(return_id), 0, 0, guandan_card_id_to_wire(tribute_id))
    seats = (returner, SENTINEL, SENTINEL, donor)
    nested = field(1, 0, guandan_card_id_to_wire(return_id))
    if returner:
        nested += field(2, 0, returner)
    nested += field(3, 0, donor)
    return (
        field(1, 0, 2) + field(2, 2, packed(cards)) + field(3, 2, packed(seats))
        + field(4, 2, b"\x0f\x0d\x0f\x0d") + field(5, 2, nested)
    )


class Driver:
    def __init__(self, controller: GuanceController) -> None:
        self.controller = controller
        self.sequence = 0

    def send(self, command: int, body: bytes) -> None:
        payload = aggregate([record(command, COMMAND_VARIANTS[command], body)])
        self.controller._on_frame(SourceFrame(
            host=GAME_HOST, payload=payload, received_at=float(self.sequence + 1),
            connection_id="normal-exchange-connection", sequence=self.sequence,
            direction="server",
        ))
        self.sequence += 1
def _next_after_pass(state, actor: int) -> tuple[int, int | None]:
    assert state.lead_action is not None
    alive = {seat for seat, count in enumerate(state.remaining) if count > 0}
    passed = set(state._passed) | {actor}
    winner = state.lead_action.player
    if alive - {winner} <= passed:
        next_actor = winner if winner in alive else (winner + 2) % 4
        if next_actor not in alive:
            next_actor = next(seat for offset in range(1, 5)
                              if (seat := (winner - offset) % 4) in alive)
        return next_actor, 1 if winner in alive else 2
    next_actor = (actor - 1) % 4
    while next_actor not in alive:
        next_actor = (next_actor - 1) % 4
    return next_actor, None


def pass_until_free(driver: Driver) -> None:
    while True:
        state = driver.controller.session.state
        assert state is not None
        if state.phase == "ended" or state.free_lead:
            return
        actor = state.current_seat
        assert actor is not None
        next_actor, reset = _next_after_pass(state, actor)
        driver.send(3006, pass_body(actor, next_actor, reset=reset))


def test_controller_normal_exchange_subset_produces_eligible_v2_capture(tmp_path: Path):
    vm = GuanceViewModel()
    subset = NormalExchangeLiveSubsetProvider(SOURCE)
    controller = GuanceController(
        vm, paths=paths(tmp_path), aggregate_parser=parse_aggregate,
        accepted_host=GAME_HOST, live_subset_provider=subset,
        canonical_converter_factory=CanonicalCaptureConverter,
    )
    controller._model.close()
    controller._model = FakeModel()
    controller.preferences["debug_capture_enabled"] = True
    controller._open_debug_recorder()
    assert controller._debug_recorder is not None and controller._debug_recorder.path is not None
    capture_dir = controller._debug_recorder.path.parent
    capture_id = capture_dir.name
    driver = Driver(controller)

    deck = list(range(108))
    first_hands = [deck[0:27], deck[27:54], deck[54:81], deck[81:108]]
    driver.send(3001, deal_body(first_hands[1]))
    driver.send(3019, observer_body(1, first_hands[1]))
    driver.send(3011, exchange_init_body())
    driver.send(3002, information_3002_body())
    driver.send(3004, start_body(0))
    driver.send(3005, play_body(0, 3, first_hands[0], remaining=0, combo=8, finish=1))
    driver.send(3005, play_body(3, 2, first_hands[3], remaining=0, combo=8, finish=2))
    driver.send(3005, play_body(2, None, first_hands[2], remaining=0, combo=8, finish=3))
    driver.send(3008, settlement_body((1, 4, 3, 2), [[], first_hands[1], [], []]))
    assert controller.session.state is not None and controller.session.state.phase == "settled"

    def rank_ids(rank: int) -> list[int]:
        base = (rank - 3) * 4
        return [base, base + 1, base + 2, base + 3,
                base + 54, base + 55, base + 56, base + 57]

    bombs = [rank_ids(rank)[:6] for rank in (3, 4, 6, 8)]
    observer_initial = [card for group in bombs for card in group] + [24, 28, 53]
    assert len(observer_initial) == len(set(observer_initial)) == 27
    remaining = [card for card in deck if card not in observer_initial]
    seat0 = [16] + [card for card in remaining if card != 16][:26]
    left = [card for card in remaining if card not in seat0]
    seat2, seat3 = left[:27], left[27:54]
    assert len(seat0) == len(seat2) == len(seat3) == 27
    second_hands = [seat0, observer_initial, seat2, seat3]

    tribute_id, return_id = 53, 16
    observer_after_tribute = [card for card in observer_initial if card != tribute_id]
    observer_after_exchange = observer_after_tribute + [return_id]
    seat0_after_exchange = [card for card in seat0 if card != return_id] + [tribute_id]

    driver.send(3001, deal_body(observer_initial))
    driver.send(3019, observer_body(1, observer_initial))
    driver.send(3011, exchange_init_body())
    driver.send(3012, tribute_3012(1, tribute_id, 0, observer_after_tribute))
    driver.send(3013, tribute_3013(1, tribute_id, 0))
    driver.send(3014, return_3014(0, return_id, 1, seat0_after_exchange))
    driver.send(3015, summary_3015(tribute_id, 1, return_id, 0))
    driver.send(3002, information_3002_body())
    driver.send(3004, start_body(1))
    assert subset.model_adapter_eligible_count >= 1

    plays = [*bombs, [16], [24], [28]]
    remaining_count = 27
    for index, cards in enumerate(plays):
        remaining_count -= len(cards)
        final = index + 1 == len(plays)
        driver.send(3005, play_body(
            1, 0, cards, remaining=remaining_count,
            combo=10 if len(cards) == 6 else 1,
            finish=1 if final else None,
        ))
        pass_until_free(driver)
    state = controller.session.state
    assert state is not None and state.current_seat == 3 and state.free_lead

    seat3_cards = list(seat3)
    for index, card_id in enumerate(seat3_cards):
        final = index + 1 == len(seat3_cards)
        driver.send(3005, play_body(
            3, None if final else 2, [card_id],
            remaining=len(seat3_cards) - index - 1, combo=1,
            finish=2 if final else None,
        ))
        if not final:
            pass_until_free(driver)
    state = controller.session.state
    assert state is not None and state.phase == "ended"
    driver.send(3008, settlement_body(
        (3, 1, 4, 2), [seat0_after_exchange, [], seat2, []],
    ))
    assert controller.session.state is not None and controller.session.state.phase == "settled"

    controller._close_debug_recorder()
    assert controller._last_canonical_close is not None
    assert controller._last_canonical_close.status == "eligible"
    assert controller._last_canonical_close.decision_count == 7
    assert subset.model_adapter_eligible_count == 7
    manifest = json.loads((capture_dir / "phase7_acceptance_input.json").read_text(encoding="utf-8"))
    assert manifest["capture_id"] == capture_id
    assert manifest["protocol_identity"] == "TencentDaguandanObservedProtocol.v20261014"
    assert manifest["eligibility"] == "eligible"
    assert manifest["decision_count"] == controller._last_canonical_close.decision_count
    summary = validate_v2_candidate(capture_dir, capture_id)
    assert summary["eligible"] is True
    assert not (capture_dir / "phase7_canonical_decisions.jsonl.tmp").exists()

from guance.domain.session import SessionEngine


class EngineDriver:
    def __init__(self, engine: SessionEngine) -> None:
        self.engine = engine
        self.sequence = 0
        self.last_updates = []

    def send(self, command: int, body: bytes, *, connection: str = "normal-exchange-connection"):
        payload = aggregate([record(command, COMMAND_VARIANTS[command], body)])
        self.last_updates = self.engine.feed(SourceFrame(
            host=GAME_HOST, payload=payload, received_at=float(self.sequence + 1),
            connection_id=connection, sequence=self.sequence, direction="server",
        ))
        self.sequence += 1
        return self.last_updates


def _first_settled_game(driver: EngineDriver) -> list[list[int]]:
    deck = list(range(108))
    hands = [deck[0:27], deck[27:54], deck[54:81], deck[81:108]]
    driver.send(3001, deal_body(hands[1]))
    driver.send(3019, observer_body(1, hands[1]))
    driver.send(3011, exchange_init_body())
    driver.send(3002, information_3002_body())
    driver.send(3004, start_body(0))
    driver.send(3005, play_body(0, 3, hands[0], remaining=0, combo=8, finish=1))
    driver.send(3005, play_body(3, 2, hands[3], remaining=0, combo=8, finish=2))
    driver.send(3005, play_body(2, None, hands[2], remaining=0, combo=8, finish=3))
    driver.send(3008, settlement_body((1, 4, 3, 2), [[], hands[1], [], []]))
    assert driver.engine.state is not None and driver.engine.state.phase == "settled"
    return hands


def _negative_engine() -> tuple[NormalExchangeLiveSubsetProvider, EngineDriver]:
    subset = NormalExchangeLiveSubsetProvider(SOURCE)
    engine = SessionEngine(
        "desktop", aggregate_parser=parse_aggregate, accepted_host=GAME_HOST,
        event_adapter=subset.adapt_event, event_observer=subset.observe_applied,
        reset_observer=subset.reset_session,
    )
    return subset, EngineDriver(engine)


def _second_game_exchange_prefix(driver: EngineDriver, observer_hand: list[int]) -> tuple[int, int]:
    tribute_id = 53
    return_id = 16
    driver.send(3001, deal_body(observer_hand))
    driver.send(3019, observer_body(1, observer_hand))
    driver.send(3011, exchange_init_body())
    return tribute_id, return_id


def test_first_observed_game_is_bounded_ineligible():
    subset, driver = _negative_engine()
    hand = list(range(27, 54))
    subset.bind_capture("first-game-only")
    driver.send(3001, deal_body(hand))
    driver.send(3019, observer_body(1, hand))
    driver.send(3011, exchange_init_body())
    driver.send(3002, information_3002_body())
    driver.send(3004, start_body(1))
    assert subset.model_adapter_eligible_count == 0
    assert subset.drain_decisions() == ()


@pytest.mark.parametrize("case", ["anti_or_skip", "missing_3013", "missing_3015", "transfer_mismatch"])
def test_incomplete_or_mismatched_exchange_is_exactly_unsupported(case: str):
    subset, driver = _negative_engine()
    _first_settled_game(driver)
    hand = list(range(27, 54))
    tribute_id, return_id = _second_game_exchange_prefix(driver, hand)
    if case != "anti_or_skip":
        driver.send(3012, tribute_3012(1, tribute_id, 0,
                                      [card for card in hand if card != tribute_id]))
    if case not in {"anti_or_skip", "missing_3013"}:
        driver.send(3013, tribute_3013(1, tribute_id, 0))
        driver.send(3014, return_3014(0, return_id, 1, list(range(27))))
    if case == "transfer_mismatch":
        updates = driver.send(3015, summary_3015(tribute_id, 1, 17, 0))
    else:
        updates = driver.send(3004, start_body(1))
    errors = [update for update in updates if update.kind == "error"]
    assert errors and errors[-1].error_code == "LIVE_EXCHANGE_VARIANT_UNSUPPORTED"
    assert subset.drain_decisions() == ()


def test_ambiguous_declaration_invalidates_only_current_game():
    subset, driver = _negative_engine()
    _first_settled_game(driver)
    hand = list(dict.fromkeys([8, 9, 10, 24, 25, 53, *range(27, 54)]))[:27]
    assert 53 in hand and all(card in hand for card in (8, 9, 10, 24, 25))
    tribute_id, return_id = _second_game_exchange_prefix(driver, hand)
    after_tribute = [card for card in hand if card != tribute_id]
    driver.send(3012, tribute_3012(1, tribute_id, 0, after_tribute))
    driver.send(3013, tribute_3013(1, tribute_id, 0))
    driver.send(3014, return_3014(0, return_id, 1, list(range(27))))
    driver.send(3015, summary_3015(tribute_id, 1, return_id, 0))
    driver.send(3002, information_3002_body())
    driver.send(3004, start_body(1))
    updates = driver.send(3005, play_body(
        1, 0, [8, 9, 10, 24, 25], remaining=22, combo=5,
    ))
    errors = [update for update in updates if update.kind == "error"]
    assert errors and errors[-1].error_code == "LIVE_DECLARATION_AMBIGUOUS"
    assert driver.engine.paused is False


def test_gap_and_connection_switch_remain_transport_fail_closed():
    _subset, driver = _negative_engine()
    hand = list(range(27))
    driver.send(3001, deal_body(hand))
    payload = aggregate([record(3019, COMMAND_VARIANTS[3019], observer_body(0, hand))])
    updates = driver.engine.feed(SourceFrame(
        host=GAME_HOST, payload=payload, received_at=2.0,
        connection_id="normal-exchange-connection", sequence=driver.sequence + 1,
        direction="server",
    ))
    assert updates[-1].error_code == "frame_sequence_gap"

    _subset, driver = _negative_engine()
    driver.send(3001, deal_body(hand))
    updates = driver.send(3019, observer_body(0, hand), connection="different-connection")
    assert updates[-1].error_code == "capture_paused"



def test_3010_informational_does_not_sever_verified_same_connection_chain():
    subset, driver = _negative_engine()
    subset.bind_capture("information-3010")
    _first_settled_game(driver)
    driver.send(3010, b"")
    hand = list(range(27, 54))
    driver.send(3001, deal_body(hand))
    driver.send(3019, observer_body(1, hand))
    driver.send(3011, exchange_init_body())
    assert subset.continuous_chain is not None
    assert subset.continuous_chain.connection_id == "normal-exchange-connection"

def test_observer_conflict_is_fail_closed():
    _subset, driver = _negative_engine()
    hand = list(range(27))
    driver.send(3001, deal_body(hand))
    driver.send(3019, observer_body(0, hand))
    updates = driver.send(3019, observer_body(2, hand))
    assert updates[-1].error_code == "seat_evidence_conflict"


def test_terminal_connection_epoch_switch_starts_independent_previous_result_scope():
    subset, driver = _negative_engine()
    subset.bind_capture("two-epoch-negative")
    _first_settled_game(driver)
    driver.sequence = 0
    hand = list(range(27, 54))
    driver.send(3001, deal_body(hand), connection="second-connection")
    driver.send(3019, observer_body(1, hand), connection="second-connection")
    driver.send(3011, exchange_init_body(), connection="second-connection")
    driver.send(3002, information_3002_body(), connection="second-connection")
    driver.send(3004, start_body(1), connection="second-connection")
    assert driver.engine.paused is False
    assert subset.continuous_chain is None
    assert subset.model_adapter_eligible_count == 0
    assert subset.drain_decisions() == ()


def _pass_until_free_engine(driver: EngineDriver) -> None:
    while True:
        state = driver.engine.state
        assert state is not None
        if state.phase == "ended" or state.free_lead:
            return
        actor = state.current_seat
        assert actor is not None
        next_actor, reset = _next_after_pass(state, actor)
        driver.send(3006, pass_body(actor, next_actor, reset=reset))


def _second_eligible_game(driver: EngineDriver) -> None:
    """Fixed synthetic second game that reaches an observer decision opportunity."""
    _first_settled_game(driver)

    def rank_ids(rank: int) -> list[int]:
        base = (rank - 3) * 4
        return [base, base + 1, base + 2, base + 3,
                base + 54, base + 55, base + 56, base + 57]

    deck = list(range(108))
    bombs = [rank_ids(rank)[:6] for rank in (3, 4, 6, 8)]
    observer_initial = [card for group in bombs for card in group] + [24, 28, 53]
    assert len(observer_initial) == len(set(observer_initial)) == 27
    remaining = [card for card in deck if card not in observer_initial]
    seat0 = [16] + [card for card in remaining if card != 16][:26]
    left = [card for card in remaining if card not in seat0]
    seat2, seat3 = left[:27], left[27:54]
    assert len(seat0) == len(seat2) == len(seat3) == 27

    tribute_id, return_id = 53, 16
    observer_after_tribute = [card for card in observer_initial if card != tribute_id]
    seat0_after_exchange = [card for card in seat0 if card != return_id] + [tribute_id]

    driver.send(3001, deal_body(observer_initial))
    driver.send(3019, observer_body(1, observer_initial))
    driver.send(3011, exchange_init_body())
    driver.send(3012, tribute_3012(1, tribute_id, 0, observer_after_tribute))
    driver.send(3013, tribute_3013(1, tribute_id, 0))
    driver.send(3014, return_3014(0, return_id, 1, seat0_after_exchange))
    driver.send(3015, summary_3015(tribute_id, 1, return_id, 0))
    driver.send(3002, information_3002_body())
    driver.send(3004, start_body(1))

    plays = [*bombs, [16], [24], [28]]
    remaining_count = 27
    for index, cards in enumerate(plays):
        remaining_count -= len(cards)
        final = index + 1 == len(plays)
        driver.send(3005, play_body(
            1, 0, cards, remaining=remaining_count,
            combo=10 if len(cards) == 6 else 1,
            finish=1 if final else None,
        ))
        _pass_until_free_engine(driver)
    state = driver.engine.state
    assert state is not None and state.phase in {"active", "ended"}


def test_runtime_session_binding_produces_decisions_without_capture_evidence():
    """The recommendation producer is enabled by the runtime session, not by debug files."""
    subset, driver = _negative_engine()
    assert subset.decision_session_id == ""
    assert subset.model_adapter_eligible_count == 0
    subset.bind_runtime_session("runtime-session")
    assert subset.decision_session_id == "runtime-session"
    _second_eligible_game(driver)
    assert subset.model_adapter_eligible_count >= 1
    decisions = subset.drain_decisions()
    assert decisions
    assert all(decision.capture_id == "runtime-session" for decision in decisions)
    assert subset.drain_decisions() == ()
    # Unbinding the runtime session stops decision production even though the
    # capture binding API was never used (evidence recording stays independent).
    subset.unbind_runtime_session()
    assert subset.decision_session_id == ""
    assert subset.drain_decisions() == ()


def test_unbound_provider_produces_no_decisions_for_the_same_eligible_sequence():
    subset, driver = _negative_engine()
    _second_eligible_game(driver)
    assert subset.model_adapter_eligible_count == 0
    assert subset.drain_decisions() == ()
