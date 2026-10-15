from __future__ import annotations

import pytest

from guance.domain.events import (
    ExchangeSummaryEvent, ReturnSelectionEvent, TributeSelectionEvent, TributeSummaryEvent,
)
from guance.infrastructure.protocol import wire
from guance.infrastructure.protocol.live import parse_command
from guance.public_state_contract import guandan_card_id_to_wire
from guance.domain.events import SourceMeta

META = SourceMeta(record_type=15815, record_version=2, connection_id="c", sequence=1,
                  sub_index=0)
SENTINEL = (1 << 64) - 1


def scalar(value: int):
    return [(0, value)]


def packed(values):
    return [(2, b"".join(wire.encode_varint(value) for value in values))]


def nested(card: int, source: int | None, target: int | None) -> bytes:
    parts = [wire.build_field(1, 0, card)]
    if source is not None:
        parts.append(wire.build_field(2, 0, source))
    if target is not None:
        parts.append(wire.build_field(3, 0, target))
    return wire.build_message(*parts)


def private_hand(count: int = 27):
    return packed(tuple(guandan_card_id_to_wire(card_id) for card_id in range(count)))


def test_3012_defaults_donor_zero_only_in_frozen_shape_and_discards_private_hand():
    card = guandan_card_id_to_wire(10)
    event = parse_command(3012, {
        3: scalar(card), 4: packed((card, 0)), 5: packed((2, SENTINEL)), 6: private_hand(),
    }, META)
    assert isinstance(event, TributeSelectionEvent)
    assert event.donor_seat == 0
    assert event.tribute_card == card
    assert event.tribute_cards == (card,)
    assert event.recipient_seats == (2,)
    assert "hand" not in repr(event).lower()
    with pytest.raises(ValueError, match="shape"):
        parse_command(3012, {3: scalar(card), 4: packed((card, 0)), 5: packed((2, SENTINEL))}, META)


def test_3013_nested_defaults_are_donor_and_recipient_seats_not_indices():
    first = guandan_card_id_to_wire(3)
    second = guandan_card_id_to_wire(7)
    event = parse_command(3013, {
        1: scalar(20), 2: [(2, b"\x0f\x0c\x0f\x0c")],
        3: [(2, nested(first, None, 1)), (2, nested(second, 2, 3))],
        4: scalar(275),
    }, META)
    assert isinstance(event, TributeSummaryEvent)
    assert [(item.tribute_card, item.donor_seat, item.recipient_seat)
            for item in event.entries] == [(first, 0, 1), (second, 2, 3)]


def test_3014_bounded_default_zero_shapes():
    card = guandan_card_id_to_wire(8)
    missing_source = parse_command(3014, {
        3: scalar(card), 4: scalar(3), 5: private_hand(26),
    }, META)
    assert missing_source == ReturnSelectionEvent(0, card, 3, missing_source.meta)
    missing_target = parse_command(3014, {
        2: scalar(1), 3: scalar(card), 5: private_hand(26),
    }, META)
    assert missing_target.source_seat == 1 and missing_target.target_seat == 0


def test_3015_reconciles_card_source_slots_and_return_entries():
    tribute = guandan_card_id_to_wire(9)
    returned = guandan_card_id_to_wire(12)
    event = parse_command(3015, {
        1: scalar(2), 2: packed((returned, tribute, 0, 0)),
        3: packed((0, 1, SENTINEL, SENTINEL)),
        4: [(2, b"\x0f\x0d\x0f\x0d")],
        5: [(2, nested(returned, None, 1))],
    }, META)
    assert isinstance(event, ExchangeSummaryEvent)
    assert {(item.card, item.source_seat) for item in event.card_sources} == {
        (returned, 0), (tribute, 1),
    }
    assert [(item.source_seat, item.target_seat, item.return_card)
            for item in event.return_entries] == [(0, 1, returned)]


@pytest.mark.parametrize("command,group", [
    (3011, {1: scalar(10), 2: scalar(2)}),
    (3013, {1: scalar(20), 2: [(2, b"1234")]}),
    (3015, {1: scalar(2), 2: packed((0, 0, 0, 0)), 3: packed((SENTINEL,) * 4)}),
])
def test_exchange_unknown_shapes_fail_closed(command, group):
    with pytest.raises(ValueError, match="shape"):
        parse_command(command, group, META)
