"""GMV2 Phase 7 strict protobuf presence and shape guards."""
import pytest

from guance.domain.events import ActionEvent, InformationalEvent, SourceMeta
from guance.infrastructure.protocol import wire
from guance.infrastructure.protocol.live import parse_command
from guance.public_state_contract import guandan_card_id_to_wire

META = SourceMeta()


def scalar(value):
    return [(0, value)]


def packed(values):
    return [(2, b"".join(wire.encode_varint(value) for value in values))]


def play_body(*, actor=0, next_actor=3, remaining=26, finish=None, private=False):
    value = {
        2: scalar(actor), 3: scalar(next_actor), 4: scalar(1), 5: scalar(remaining),
        6: scalar(1), 7: packed((guandan_card_id_to_wire(0),)),
        8: scalar(1), 9: scalar(1), 11: [(2, b"x" * 20)],
    }
    if finish is not None:
        value[1] = scalar(finish)
        if remaining == 0:
            value.pop(5)
    if private:
        value[10] = packed((guandan_card_id_to_wire(1),))
    return value


def pass_body(*, actor=0, next_actor=3, reset=None, private=False):
    value = {2: scalar(actor), 3: scalar(next_actor), 5: scalar(1)}
    if reset is not None:
        value[4] = scalar(reset)
    if private:
        value[6] = packed((guandan_card_id_to_wire(1),))
    return value


def observer_body():
    entry = wire.build_field(2, 2, wire.encode_varint(guandan_card_id_to_wire(0)))
    return {2: scalar(0), 3: [(2, wire.build_field(1, 2, entry))]}


@pytest.mark.parametrize("command,group,field", [
    (3004, {1: scalar(0), 2: scalar(20), 3: scalar(2), 4: scalar(35)}, 2),
    (3004, {1: scalar(0), 2: scalar(20), 3: scalar(2), 4: scalar(35)}, 3),
    (3004, {1: scalar(0), 2: scalar(20), 3: scalar(2), 4: scalar(35)}, 4),
    (3005, play_body(), 4), (3005, play_body(), 6), (3005, play_body(), 7),
    (3005, play_body(), 8), (3005, play_body(), 9), (3005, play_body(), 11),
    (3006, pass_body(), 5),
])
@pytest.mark.parametrize("invalid", ["missing", "duplicate", "wrong-wire"])
def test_required_scalar_or_field_presence_shape(command, group, field, invalid):
    group = {key: list(values) for key, values in group.items()}
    if invalid == "missing":
        del group[field]
    elif invalid == "duplicate":
        group[field] *= 2
    else:
        group[field] = [(2, b"PRIVATE_SENTINEL")]
    with pytest.raises(ValueError) as caught:
        parse_command(command, group, META)
    assert "PRIVATE_SENTINEL" not in str(caught.value)


@pytest.mark.parametrize("field2", [[(0, 0), (0, 0)], [(2, b"PRIVATE_SENTINEL")]])
def test_3019_present_seat_must_keep_the_exact_scalar_shape(field2):
    group = observer_body()
    group[2] = field2
    with pytest.raises(ValueError, match="shape"):
        parse_command(3019, group, META)


def test_3004_default_zero_is_bounded_to_frozen_shape():
    event = parse_command(3004, {2: scalar(20), 3: scalar(2), 4: scalar(35)}, META)
    assert event.first_seat == 0 and event.first_seat_present
    with pytest.raises(ValueError):
        parse_command(3004, {2: scalar(19), 3: scalar(2), 4: scalar(35)}, META)


def test_3005_actor_and_next_default_zero_only_for_observed_shapes():
    missing_actor = play_body(actor=0)
    del missing_actor[2]
    assert parse_command(3005, missing_actor, META).player == 0
    missing_next = play_body(next_actor=0, private=True)
    del missing_next[3]
    assert parse_command(3005, missing_next, META).next_player == 0
    malformed = play_body()
    del malformed[2]
    malformed[10] = packed((guandan_card_id_to_wire(1),))
    with pytest.raises(ValueError, match="shape"):
        parse_command(3005, malformed, META)


def test_3005_field11_is_exactly_20_bytes_for_v20261014():
    assert parse_command(3005, play_body(), META).kind == "play"
    for length in (0, 18, 19, 21):
        group = play_body()
        group[11] = [(2, b"x" * length)]
        with pytest.raises(ValueError, match="field11"):
            parse_command(3005, group, META)


def test_3005_observed_field3_omissions_default_only_under_exact_shapes():
    finished = play_body(actor=1, next_actor=0, remaining=0, finish=2)
    del finished[3]
    assert set(finished) == {1, 2, 4, 6, 7, 8, 9, 11}
    event = parse_command(3005, finished, META)
    assert event.player == 1 and event.next_player == 0 and event.remaining_after == 0

    continuing = play_body(actor=2, next_actor=0, remaining=7)
    del continuing[3]
    assert set(continuing) == {2, 4, 5, 6, 7, 8, 9, 11}
    event = parse_command(3005, continuing, META)
    assert event.player == 2 and event.next_player == 0 and event.remaining_after == 7


def test_3005_finish_rank_is_optional_and_zero_remaining_uses_frozen_omission():
    unfinished = parse_command(3005, play_body(), META)
    assert unfinished.finish_rank is None and unfinished.remaining_after == 26
    finished = parse_command(3005, play_body(remaining=0, finish=1), META)
    assert finished.finish_rank == 1 and finished.remaining_after == 0


def test_3006_actor_next_defaults_and_reset_presence_are_strict():
    missing_actor = pass_body(actor=0, next_actor=3)
    del missing_actor[2]
    event = parse_command(3006, missing_actor, META)
    assert event.player == 0 and event.next_player == 3 and event.reset_flag is None
    missing_next = pass_body(actor=2, next_actor=0, private=True)
    del missing_next[3]
    event = parse_command(3006, missing_next, META)
    assert event.player == 2 and event.next_player == 0 and event.reset_flag is None
    assert parse_command(3006, pass_body(reset=1), META).reset_flag == 1
    assert parse_command(3006, pass_body(reset=2), META).reset_flag == 2
    with pytest.raises(ValueError):
        parse_command(3006, pass_body(reset=0), META)


def test_3006_observed_actor_only_shape_defaults_next_without_inventing_reset_or_private_hand():
    group = pass_body(actor=2, next_actor=0)
    del group[3]
    assert set(group) == {2, 5}
    event = parse_command(3006, group, META)
    assert event.player == 2 and event.next_player == 0
    assert event.reset_flag is None and event.own_hand_snapshot is None


@pytest.mark.parametrize("ranks", [None, (), (1, 2, 3), (1, 2, 3, 3), (1, 2, 3, 5), (0, 1, 2, 3)])
def test_settlement_ranking_presence_shape(ranks):
    group = {} if ranks is None else {3: packed(ranks)}
    with pytest.raises(ValueError):
        parse_command(3008, group, META)


def test_valid_ranking_and_terminal_next_and_remaining_zero():
    event = parse_command(3008, {3: packed((1, 3, 2, 4))}, META)
    assert event.ranks_by_seat == (1, 3, 2, 4)
    group = play_body(remaining=0, finish=1)
    group[3] = scalar((1 << 64) - 1)
    event = parse_command(3005, group, META)
    assert event.next_player is None and event.remaining_after == 0
    assert type(event) is ActionEvent
    assert not hasattr(event, "declaration")


@pytest.mark.parametrize("command", [3002, 3003, 3007])
def test_unverified_information_never_becomes_exchange_or_declaration(command):
    event = parse_command(command, {99: [(2, b"PRIVATE_SENTINEL")]}, META)
    assert type(event) is InformationalEvent
    assert "PRIVATE_SENTINEL" not in repr(event)
    assert "cards" not in repr(event)
