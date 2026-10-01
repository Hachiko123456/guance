from __future__ import annotations

import math
import struct


def varint(value: int) -> bytes:
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def field(number: int, wire: int, value: bytes | int) -> bytes:
    out = bytearray(varint(number * 8 + wire))
    if wire == 0:
        out.extend(varint(int(value)))
    else:
        assert isinstance(value, bytes)
        out.extend(varint(len(value)))
        out.extend(value)
    return bytes(out)


def record(command: int, variant: int, body: bytes) -> bytes:
    envelope = field(1, 0, command) + field(3, 2, field(variant, 2, body))
    payload = envelope
    sub_size = 16 + len(payload)
    # Live record header fields are structural; only the declared payload length
    # and type/version are relevant to the pure parser.
    sub = bytearray()
    sub.extend(sub_size.to_bytes(2, "big"))
    sub.extend((15815).to_bytes(2, "big"))
    sub.extend((2).to_bytes(2, "big"))
    sub.extend(b"\x00" * 8)
    sub.extend((len(payload) + 2).to_bytes(4, "big"))
    sub.extend(payload)
    return bytes(sub)


def aggregate(records: list[bytes]) -> bytes:
    body = len(records).to_bytes(2, "big") + b"".join(records)
    aggregate_body = (len(body) + 2).to_bytes(2, "big") + body
    raw = bytearray(24 + len(aggregate_body))
    raw[8:10] = b"\xff\xfd"
    raw[24:] = aggregate_body
    raw[0:2] = len(raw).to_bytes(2, "big")
    return bytes(raw)

def test_live_parser_reads_deal_and_start() -> None:
    from guance.infrastructure.protocol.live import parse_aggregate
    # Card codes: H2, S3, C4, D5 plus filler to satisfy the observed 27-card contract.
    hand = [((2 << 4) | 2), ((1 << 4) | 3), ((3 << 4) | 4), ((4 << 4) | 5)]
    hand.extend(((1 << 4) | (6 + i)) for i in range(23))
    packed = b"".join(varint(x) for x in hand)
    # The 3001 hint intentionally differs from 3004 to prove it is not
    # treated as the observer seat.
    deal = field(1, 0, 27) + field(2, 2, packed) + field(3, 0, 1) + field(4, 0, 5)
    start = field(1, 0, 2)
    raw = aggregate([record(3001, 2, deal), record(3004, 5, start)])
    events = parse_aggregate(raw)
    assert events[0].level == 5
    assert events[0].my_seat == -1
    assert events[0].hand[0] == ((2 << 4) | 2)
    assert events[0].first_seat_hint == 1
    assert events[1].first_seat == 2


def test_live_parser_extracts_3019_own_hand_candidate_without_confirming_it() -> None:
    from guance.domain.events import OwnSeatCandidateEvent
    from guance.infrastructure.protocol.live import parse_aggregate

    hand = [((1 << 4) | (2 + i)) for i in range(27)]
    packed = b"".join(varint(x) for x in hand)
    entry = field(1, 0, len(hand)) + field(2, 2, packed)
    nested = field(2, 2, entry)
    body = field(2, 0, 3) + field(3, 2, nested)
    events = parse_aggregate(aggregate([record(3019, 26, body)]))

    assert len(events) == 1
    assert isinstance(events[0], OwnSeatCandidateEvent)
    assert events[0].candidate_seat == 3
    assert events[0].candidate_seat_present is True
    assert events[0].referenced_cards == tuple(hand)


def test_live_parser_preserves_3019_default_zero_presence() -> None:
    from guance.domain.events import OwnSeatCandidateEvent
    from guance.infrastructure.protocol.live import parse_aggregate

    hand = [((1 << 4) | (2 + i)) for i in range(27)]
    packed = b"".join(varint(x) for x in hand)
    entry = field(1, 0, len(hand)) + field(2, 2, packed)
    body = field(3, 2, field(2, 2, entry))
    events = parse_aggregate(aggregate([record(3019, 26, body)]))

    assert isinstance(events[0], OwnSeatCandidateEvent)
    assert events[0].candidate_seat == 0
    assert events[0].candidate_seat_present is False


def test_live_parser_accepts_3007_as_non_authoritative_information() -> None:
    from guance.domain.events import InformationalEvent
    from guance.infrastructure.protocol.live import parse_aggregate

    # The live command carries auxiliary fields whose business meaning is not
    # established here.  Parsing must accept the command but expose no cards or
    # other payload to the domain/UI layers.
    auxiliary = field(1, 0, 8) + field(2, 2, varint(7) + varint(6))
    events = parse_aggregate(aggregate([record(3007, 8, auxiliary)]))

    assert len(events) == 1
    assert isinstance(events[0], InformationalEvent)
    assert events[0].command == 3007
    assert 'cards=' not in repr(events[0])
    assert 'payload=' not in repr(events[0])


def test_live_parser_preserves_end_auxiliary_settlement_order_in_one_aggregate() -> None:
    from guance.domain.events import ActionEvent, InformationalEvent, SettlementEvent
    from guance.infrastructure.protocol.live import parse_aggregate

    last_play = (
        field(2, 0, 0)
        + field(3, 0, (1 << 64) - 1)
        + field(4, 0, 1)
        + field(1, 0, 2)
        + field(5, 0, 0)
        + field(6, 0, 1)
        + field(7, 2, varint(17))
    )
    empty_hand = field(1, 0, 0)
    settlement = field(3, 2, b"\x02\x04\x01\x03") + b"".join(
        field(13, 2, empty_hand) for _ in range(4)
    )
    raw = aggregate([
        record(3005, 6, last_play),
        record(3007, 8, b""),
        record(3008, 9, settlement),
    ])

    events = parse_aggregate(raw)

    assert [type(event) for event in events] == [ActionEvent, InformationalEvent, SettlementEvent]
    assert events[0].next_player is None
    assert events[0].finish_rank == 2
    assert events[1].command == 3007
    assert events[2].ranks_by_seat == (2, 4, 1, 3)


def fixed64_field(number: int, value: float) -> bytes:
    return varint(number * 8 + 1) + struct.pack("<d", value)


def test_live_parser_preserves_neutral_3003_evidence_without_semantic_claims() -> None:
    from guance.domain.events import InformationalEvent, SourceMeta
    from guance.infrastructure.protocol.live import parse_command

    field5 = (
        fixed64_field(1, 1.5)
        + fixed64_field(2, 2.0)
        + field(4, 0, 1)
    )
    event = parse_command(3003, {
        1: [(0, 1)],
        3: [(0, 1)],
        4: [(2, b"\x01\x02\x01\x02")],
        5: [(2, field5)],
    }, SourceMeta())

    assert isinstance(event, InformationalEvent)
    assert event.command == 3003
    assert event.reported_seat_snapshot == (1, 2, 1, 2)
    assert event.reported_values == (1.5, 2.0)
    assert event.reported_flag == 1
    assert not hasattr(event, "doubling_success")


def test_live_parser_drops_malformed_3003_evidence_without_fabricating_values() -> None:
    from guance.domain.events import InformationalEvent, SourceMeta
    from guance.infrastructure.protocol.live import parse_command

    nan_field5 = fixed64_field(1, math.nan) + fixed64_field(2, 1.0)
    event = parse_command(3003, {
        4: [(2, b"\x01\x02")],
        5: [(2, nan_field5), (2, nan_field5)],
    }, SourceMeta())

    assert isinstance(event, InformationalEvent)
    assert event.reported_seat_snapshot is None
    assert event.reported_values is None
    assert event.reported_flag is None


def test_live_parser_accepts_existing_3002_structure_as_information_only() -> None:
    from guance.domain.events import InformationalEvent, SourceMeta
    from guance.infrastructure.protocol.live import parse_command

    event = parse_command(3002, {
        2: [(0, 12)],
        4: [(2, b"\x00\x0e\x0e\x00")],
        5: [(0, 1)],
    }, SourceMeta())

    assert isinstance(event, InformationalEvent)
    assert event.command == 3002
    assert event.reported_seat_snapshot is None
    assert event.reported_values is None
