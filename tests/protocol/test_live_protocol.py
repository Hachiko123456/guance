from __future__ import annotations


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
    deal = field(1, 0, 27) + field(2, 2, packed) + field(3, 0, 2) + field(4, 0, 5)
    start = field(1, 0, 2)
    raw = aggregate([record(3001, 2, deal), record(3004, 5, start)])
    events = parse_aggregate(raw)
    assert events[0].level == 5
    assert events[0].my_seat == 2
    assert events[0].hand[0] == ((2 << 4) | 2)
    assert events[1].first_seat == 2
