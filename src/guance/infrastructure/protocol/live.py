"""Parsing of the observed live Daguandan WebSocket aggregate protocol.

This module is deliberately offline/pure: it accepts bytes and returns immutable
protocol events. It does not open sockets, alter proxies, or replay traffic.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import replace
import math
import struct

from guance.domain.events import (
    ActionEvent, DealEvent, ExchangeCardSource, ExchangeInitializationEvent,
    ExchangeSummaryEvent, InformationalEvent, OwnSeatCandidateEvent, ProtocolEvent,
    ReturnSelectionEvent, SettlementEvent, SourceMeta, StartEvent, TributeSelectionEvent,
    TributeSummaryEntry, TributeSummaryEvent, UNCONFIRMED_SEAT,
)
from guance.public_state_contract import PublicStateContractError, wire_card_to_guandan_id
from . import wire

GAME_HOST = "hlxyxws.huanle.qq.com"
PB_RECORD_TYPE = 15815
COMMAND_VARIANTS = {
    14: 22, 112: 21, 3000: 1, 3001: 2, 3002: 3, 3003: 4, 3004: 5,
    3005: 6, 3006: 7, 3007: 8, 3008: 9, 3009: 10, 3010: 11, 3011: 12,
    3012: 13, 3013: 14, 3014: 15, 3015: 16, 3019: 26,
}
INFORMATIONAL_COMMANDS = {14, 112, 3000, 3002, 3003, 3007, 3009, 3010, 3019}
PROTOCOL_IDENTITY = "TencentDaguandanObservedProtocol.v20261014"

_STRICT_SHAPES = {
    3004: {"1:0,2:0,3:0,4:0", "2:0,3:0,4:0"},
    3005: {
        "2:0,3:0,4:0,5:0,6:0,7:2,8:0,9:0,11:2",
        "3:0,4:0,5:0,6:0,7:2,8:0,9:0,11:2",
        "2:0,4:0,5:0,6:0,7:2,8:0,9:0,10:2,11:2",
        "2:0,3:0,4:0,5:0,6:0,7:2,8:0,9:0,10:2,11:2",
        "1:0,2:0,3:0,4:0,6:0,7:2,8:0,9:0,11:2",
        "1:0,2:0,4:0,6:0,7:2,8:0,9:0,11:2",
        "1:0,3:0,4:0,6:0,7:2,8:0,9:0,11:2",
        "2:0,4:0,5:0,6:0,7:2,8:0,9:0,11:2",
    },
    3006: {
        "2:0,3:0,5:0", "2:0,5:0,6:2", "3:0,5:0",
        "2:0,3:0,4:0,5:0", "2:0,4:0,5:0,6:2", "3:0,4:0,5:0",
        "2:0,3:0,5:0,6:2", "2:0,3:0,4:0,5:0,6:2", "2:0,4:0,5:0",
        "2:0,5:0",
    },
    3011: {"1:0,2:0,3:2", "4:2,4:2,4:2,4:2"},
    3012: {"2:0,3:0,4:2,5:2,6:2", "3:0,4:2,5:2,6:2"},
    3013: {"1:0,2:2,3:2,3:2", "1:0,2:2,3:2", "1:0,2:2,3:2,3:2,4:0"},
    3014: {"2:0,3:0,4:0,5:2", "3:0,4:0,5:2", "2:0,3:0,5:2"},
    3015: {"1:0,2:2,3:2,4:2,5:2,5:2", "1:0,2:2,3:2,4:2,5:2"},
    3019: {"2:0,3:2", "3:2"},
}
_ENUM_5_9 = frozenset({1, 2, 5, 10, 15, 20})
_SENTINEL = (1 << 64) - 1
_FIELD11_LENGTH_V20261014 = 20


def _optional_3003_evidence(
    group: dict[int, list[tuple[int, int | bytes]]],
) -> tuple[tuple[int, ...] | None, tuple[float, ...] | None, int | None]:
    """Extract only structurally safe, neutral 3003 evidence.

    Field 4 is retained as a four-byte seat-indexed snapshot, while field 5
    is retained only as a finite pair of fixed64 values plus an optional flag.
    Neither shape is assigned a doubling/per-player meaning here. Any
    missing, duplicate, malformed, or non-finite value becomes ``None``.
    """
    seat_snapshot: tuple[int, ...] | None = None
    field4 = group.get(4, [])
    if len(field4) == 1 and field4[0][0] == 2 and isinstance(field4[0][1], bytes):
        raw = field4[0][1]
        if len(raw) == 4:
            seat_snapshot = tuple(raw)

    reported_values: tuple[float, ...] | None = None
    reported_flag: int | None = None
    field5 = group.get(5, [])
    if len(field5) == 1 and field5[0][0] == 2 and isinstance(field5[0][1], bytes):
        try:
            nested = wire.fields(field5[0][1])
            first = nested.get(1, [])
            second = nested.get(2, [])
            flag = nested.get(4, [])
            if (
                len(first) == 1 and first[0][0] == 1 and isinstance(first[0][1], bytes)
                and len(second) == 1 and second[0][0] == 1 and isinstance(second[0][1], bytes)
                and len(first[0][1]) == 8 and len(second[0][1]) == 8
                and len(flag) <= 1 and (not flag or flag[0][0] == 0)
            ):
                values = (
                    struct.unpack("<d", first[0][1])[0],
                    struct.unpack("<d", second[0][1])[0],
                )
                if all(math.isfinite(value) for value in values):
                    reported_values = values
                    if flag:
                        assert isinstance(flag[0][1], int)
                        reported_flag = flag[0][1]
        except (ValueError, struct.error, TypeError):
            pass
    return seat_snapshot, reported_values, reported_flag


def bundle_at(raw: bytes, offset: int) -> list[dict[str, object]] | None:
    if offset + 4 > len(raw) or int.from_bytes(raw[offset:offset + 2], "big") != len(raw) - offset:
        return None
    count = int.from_bytes(raw[offset + 2:offset + 4], "big")
    if not 1 <= count <= 128:
        return None
    position = offset + 4
    records: list[dict[str, object]] = []
    for sub_index in range(count):
        if position + 18 > len(raw):
            return None
        size = int.from_bytes(raw[position:position + 2], "big")
        end = position + 2 + size
        if size < 16 or end > len(raw):
            return None
        payload = raw[position + 18:end]
        declared = int.from_bytes(raw[position + 14:position + 18], "big")
        if declared != len(payload) + 2:
            return None
        records.append({"sub_index": sub_index, "type": int.from_bytes(raw[position + 2:position + 4], "big"),
                        "version": int.from_bytes(raw[position + 4:position + 6], "big"),
                        "payload": payload})
        position = end
    return records if position == len(raw) else None


def extract_bundle(raw: bytes) -> tuple[int, list[dict[str, object]]] | None:
    wire.require(len(raw) >= 10 and int.from_bytes(raw[:2], "big") == len(raw), "outer length mismatch")
    if raw[8:10] != b"\xff\xfd":
        return None
    candidates = []
    for offset in range(24, min(512, len(raw) - 3)):
        records = bundle_at(raw, offset)
        if records is not None:
            candidates.append((offset, records))
    wire.require(len(candidates) <= 1, "game aggregate offset is ambiguous")
    return candidates[0] if candidates else None


def _meta(meta: SourceMeta, *, message_index: int = -1, sub_index: int = -1) -> SourceMeta:
    return replace(meta, message_index=message_index, sub_index=sub_index)


def _optional_nested_scalar(group: dict[int, list[tuple[int, int | bytes]]], number: int) -> tuple[bool, int | None]:
    present, value = wire.optional_scalar(group, number)
    return present, value


def _nested_field3_cards(group: dict[int, list[tuple[int, int | bytes]]]) -> tuple[int, ...] | None:
    """Decode 3019's nested card references without assigning seat meaning."""
    if 3 not in group:
        return None
    try:
        nested = wire.fields(wire.one_blob(group, 3))
        cards: list[int] = []
        for values in nested.values():
            for wire_type, blob in values:
                wire.require(wire_type == 2 and isinstance(blob, bytes), "invalid 3019 nested entry")
                entry = wire.fields(blob)
                if 2 in entry:
                    cards.extend(wire.packed_field(entry, 2))
        return tuple(cards)
    except (ValueError, TypeError):
        return None


def _required_scalar(group: dict[int, list[tuple[int, int | bytes]]], number: int) -> int:
    present, value = wire.optional_scalar(group, number)
    wire.require(present and type(value) is int, "required scalar missing or invalid")
    assert isinstance(value, int)
    return value


def _group_shape(group: dict[int, list[tuple[int, int | bytes]]]) -> str:
    return ",".join(f"{number}:{wire_type}" for number in sorted(group)
                    for wire_type, _value in group[number])


def _strict_shape(command: int, group: dict[int, list[tuple[int, int | bytes]]]) -> str:
    shape = _group_shape(group)
    allowed = _STRICT_SHAPES.get(command)
    if allowed is not None:
        wire.require(shape in allowed, f"unsupported {command} group shape")
    return shape


def _default_zero_scalar(group: dict[int, list[tuple[int, int | bytes]]], number: int) -> int:
    present, value = wire.optional_scalar(group, number)
    if not present:
        return 0
    wire.require(type(value) is int, f"field {number} is not a scalar")
    assert isinstance(value, int)
    return value


def _physical_card(value: int) -> int:
    try:
        wire_card_to_guandan_id(value)
    except PublicStateContractError:
        raise ValueError("invalid physical card") from None
    return value


def _private_cards(group: dict[int, list[tuple[int, int | bytes]]], number: int) -> tuple[int, ...] | None:
    if number not in group:
        return None
    cards = tuple(wire.packed_field(group, number))
    wire.require(0 < len(cards) <= 28, "private hand shape invalid")
    for card in cards:
        _physical_card(card)
    return cards


def _nested_transfer(blob: bytes, *, command: int) -> tuple[int, int, int]:
    nested = wire.fields(blob)
    shape = _group_shape(nested)
    wire.require(shape in {"1:0,2:0,3:0", "1:0,2:0", "1:0,3:0"},
                 f"unsupported {command} nested shape")
    card = _physical_card(_required_scalar(nested, 1))
    source = _default_zero_scalar(nested, 2)
    target = _default_zero_scalar(nested, 3)
    wire.require(source in range(4) and target in range(4), "invalid exchange seat")
    return card, source, target


def parse_command(command: int, group: dict[int, list[tuple[int, int | bytes]]], meta: SourceMeta) -> ProtocolEvent:
    shape = _strict_shape(command, group)
    if not meta.group_shape:
        meta = replace(meta, command_variant=COMMAND_VARIANTS.get(command, 0), group_shape=shape)
    if command == 3001:
        hand = tuple(wire.packed_field(group, 2))
        wire.require(wire.scalar(group, 1) == len(hand) == 27, "invalid initial hand")
        for card in hand:
            _physical_card(card)
        hint_present, hint = _optional_nested_scalar(group, 3)
        secondary = None
        secondary_present = False
        if 6 in group:
            try:
                nested = wire.fields(wire.one_blob(group, 6))
                secondary_present, secondary = _optional_nested_scalar(nested, 3)
            except (ValueError, TypeError):
                raise ValueError("invalid 3001 seat hint")
        if hint_present:
            wire.require(type(hint) is int and hint in range(4), "invalid 3001 seat hint")
        if secondary_present:
            wire.require(type(secondary) is int and secondary in range(4),
                         "invalid 3001 secondary seat hint")
        # 3001 field3 and nested field6.3 are unresolved seat-like values in
        # this protocol version and are not equal to 3004.first_seat. Preserve
        # shape validation above, but never project them as start facts.
        return DealEvent(
            my_seat=UNCONFIRMED_SEAT, level=wire.scalar(group, 4), hand=hand,
            first_seat_hint=None, first_seat_hint_present=False,
            first_seat_hint_secondary=None, meta=meta,
        )
    if command == 3004:
        first_seat = _default_zero_scalar(group, 1)
        wire.require(first_seat in range(4), "invalid first seat")
        wire.require((_required_scalar(group, 2), _required_scalar(group, 3),
                      _required_scalar(group, 4)) == (20, 2, 35),
                     "unsupported 3004 constants")
        return StartEvent(first_seat=first_seat, first_seat_present=True, meta=meta)
    if command == 3019:
        cards = _nested_field3_cards(group)
        if cards is None or not cards or shape == "3:2":
            return InformationalEvent(command=command, meta=meta)
        seat = _required_scalar(group, 2)
        wire.require(seat in range(4), "invalid candidate seat")
        return OwnSeatCandidateEvent(
            candidate_seat=seat, candidate_seat_present=True, referenced_cards=cards, meta=meta,
        )
    if command == 3005:
        actor = _default_zero_scalar(group, 2)
        raw_next = _default_zero_scalar(group, 3)
        wire.require(actor in range(4), "invalid actor")
        wire.require(raw_next in range(4) or raw_next == _SENTINEL, "invalid next actor")
        next_player = None if raw_next == _SENTINEL else raw_next
        cards = tuple(wire.packed_field(group, 7))
        for card in cards:
            _physical_card(card)
        wire.require(len(cards) == _required_scalar(group, 6) and bool(cards), "play card count mismatch")
        remaining_after = _default_zero_scalar(group, 5)
        combo = _required_scalar(group, 4)
        wire.require(0 <= remaining_after <= 27 and 1 <= combo <= 11, "invalid play scalars")
        finish_present, finish_rank = wire.optional_scalar(group, 1)
        if finish_present:
            wire.require(type(finish_rank) is int and finish_rank in range(1, 5), "invalid finish rank")
        else:
            finish_rank = None
        wire.require(_required_scalar(group, 8) == 1, "unsupported 3005 field8")
        wire.require(_required_scalar(group, 9) in _ENUM_5_9, "unsupported 3005 field9")
        wire.require(len(wire.one_blob(group, 11)) == _FIELD11_LENGTH_V20261014, "unsupported 3005 field11")
        own_hand = _private_cards(group, 10)
        return ActionEvent(player=actor, kind="play", cards=cards, combo_type=combo,
                           next_player=next_player, remaining_after=remaining_after,
                           finish_rank=finish_rank, own_hand_snapshot=own_hand, meta=meta)
    if command == 3006:
        actor = _default_zero_scalar(group, 2)
        raw_next = _default_zero_scalar(group, 3)
        wire.require(actor in range(4), "invalid actor")
        wire.require(raw_next in range(4) or raw_next == _SENTINEL, "invalid next actor")
        next_player = None if raw_next == _SENTINEL else raw_next
        reset_present, reset_flag = wire.optional_scalar(group, 4)
        if reset_present:
            wire.require(type(reset_flag) is int and reset_flag in (1, 2), "invalid pass reset")
        else:
            reset_flag = None
        wire.require(_required_scalar(group, 5) in _ENUM_5_9, "unsupported 3006 field5")
        own_hand = _private_cards(group, 6)
        return ActionEvent(player=actor, kind="pass", cards=(), next_player=next_player,
                           reset_flag=reset_flag, own_hand_snapshot=own_hand, meta=meta)
    if command == 3011:
        if shape == "1:0,2:0,3:2":
            wire.require(_required_scalar(group, 1) == 10 and _required_scalar(group, 2) == 2
                         and len(wire.one_blob(group, 3)) == 4, "unsupported 3011 values")
        else:
            wire.require(len(wire.blobs(group, 4)) == 4, "unsupported 3011 values")
        return ExchangeInitializationEvent(meta=meta)
    if command == 3012:
        donor = _default_zero_scalar(group, 2)
        wire.require(donor in range(4), "invalid tribute donor")
        tribute_card = _physical_card(_required_scalar(group, 3))
        card_slots = tuple(wire.packed_field(group, 4))
        target_slots = tuple(wire.packed_field(group, 5))
        wire.require(len(card_slots) == len(target_slots) == 2, "invalid 3012 slots")
        tribute_cards = tuple(_physical_card(card) for card in card_slots if card != 0)
        recipient_seats = tuple(seat for seat in target_slots if seat != _SENTINEL)
        wire.require(all(seat in range(4) for seat in recipient_seats), "invalid tribute recipient")
        wire.require((not tribute_cards and not recipient_seats)
                     or len(tribute_cards) == len(recipient_seats), "3012 slot mismatch")
        _private_cards(group, 6)  # validate ephemerally; never retain or expose
        return TributeSelectionEvent(donor, tribute_card, tribute_cards, recipient_seats, meta)
    if command == 3013:
        wire.require(_required_scalar(group, 1) == 20 and len(wire.one_blob(group, 2)) == 4,
                     "unsupported 3013 values")
        entries = tuple(TributeSummaryEntry(*_nested_transfer(blob, command=3013))
                        for blob in wire.blobs(group, 3))
        wire.require(1 <= len(entries) <= 2, "invalid 3013 entries")
        if 4 in group:
            wire.require(_required_scalar(group, 4) == 275, "unsupported 3013 field4")
        return TributeSummaryEvent(entries, meta)
    if command == 3014:
        source = _default_zero_scalar(group, 2)
        card = _physical_card(_required_scalar(group, 3))
        target = _default_zero_scalar(group, 4)
        wire.require(source in range(4) and target in range(4), "invalid return seat")
        _private_cards(group, 5)
        return ReturnSelectionEvent(source, card, target, meta)
    if command == 3015:
        wire.require(_required_scalar(group, 1) == 2 and len(wire.one_blob(group, 4)) == 4,
                     "unsupported 3015 values")
        cards = tuple(wire.packed_field(group, 2))
        seats = tuple(wire.packed_field(group, 3))
        wire.require(len(cards) == len(seats) == 4, "invalid 3015 slots")
        sources: list[ExchangeCardSource] = []
        for card, seat in zip(cards, seats):
            empty_card, empty_seat = card == 0, seat == _SENTINEL
            wire.require(empty_card == empty_seat, "3015 slot sentinel mismatch")
            if not empty_card:
                sources.append(ExchangeCardSource(_physical_card(card), seat))
                wire.require(seat in range(4), "invalid 3015 source seat")
        returns = tuple(ReturnSelectionEvent(source, card, target, meta)
                        for card, source, target in
                        (_nested_transfer(blob, command=3015) for blob in wire.blobs(group, 5)))
        wire.require(1 <= len(returns) <= 2, "invalid 3015 return entries")
        return ExchangeSummaryEvent(tuple(sources), returns, meta)
    if command == 3008:
        hands: list[tuple[int, ...]] = []
        for raw_hand in wire.blobs(group, 13):
            item = wire.fields(raw_hand)
            cards = tuple(wire.packed_field(item, 2))
            wire.require(wire.scalar(item, 1) == len(cards), "settlement hand count mismatch")
            for card in cards:
                _physical_card(card)
            hands.append(cards)
        wire.require(len(hands) in (0, 4), "settlement must contain four hands when present")
        wire.require(3 in group, "settlement ranking missing")
        ranks = tuple(wire.packed_field(group, 3))
        wire.require(len(ranks) == 4 and sorted(ranks) == [1, 2, 3, 4],
                     "invalid settlement ranking")
        return SettlementEvent(ranks_by_seat=ranks, remaining_hands=tuple(hands), meta=meta)
    if command == 3003:
        seat_snapshot, reported_values, reported_flag = _optional_3003_evidence(group)
        return InformationalEvent(command=command, reported_seat_snapshot=seat_snapshot,
                                  reported_values=reported_values, reported_flag=reported_flag, meta=meta)
    if command in INFORMATIONAL_COMMANDS or command in COMMAND_VARIANTS:
        return InformationalEvent(command=command, meta=meta)
    raise ValueError(f"unsupported live command: {command}")


def parse_aggregate(raw: bytes, meta: SourceMeta = SourceMeta()) -> tuple[ProtocolEvent, ...]:
    bundle = extract_bundle(raw)
    if bundle is None:
        return ()
    _, records = bundle
    events: list[ProtocolEvent] = []
    for record in records:
        if record["type"] != PB_RECORD_TYPE:
            continue
        wire.require(record["version"] == 2, "unsupported protobuf record version")
        payload = record["payload"]
        assert isinstance(payload, bytes)
        outer = wire.fields(payload)
        command = wire.scalar(outer, 1)
        expected_variant = COMMAND_VARIANTS.get(command)
        wire.require(expected_variant is not None, f"unrecognized live command: {command}")
        wrapper = wire.fields(wire.one_blob(outer, 3))
        wire.require(set(wrapper) == {expected_variant}, "command/variant mismatch")
        group = wire.fields(wire.one_blob(wrapper, expected_variant))
        source = replace(
            meta, sub_index=int(record["sub_index"]), record_type=PB_RECORD_TYPE,
            record_version=2, command_variant=expected_variant, group_shape=_group_shape(group),
        )
        event = parse_command(command, group, source)
        events.append(event)
    return tuple(events)


def iter_events(frames: Iterable[object]) -> Iterator[ProtocolEvent]:
    for frame in frames:
        meta = SourceMeta(received_at=float(getattr(frame, "received_at", 0.0)),
                          sequence=int(getattr(frame, "sequence", 0)),
                          connection_id=str(getattr(frame, "connection_id", "")),
                          message_index=int(getattr(frame, "sequence", 0)))
        payload = getattr(frame, "payload")
        yield from parse_aggregate(payload, meta)
