"""Parsing of the observed live Daguandan WebSocket aggregate protocol.

This module is deliberately offline/pure: it accepts bytes and returns immutable
protocol events. It does not open sockets, alter proxies, or replay traffic.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import replace
import math
import struct

from guance.domain.events import (ActionEvent, DealEvent, InformationalEvent,
                                   OwnSeatCandidateEvent, ProtocolEvent, SettlementEvent,
                                   SourceMeta, StartEvent, UNCONFIRMED_SEAT)
from . import wire

GAME_HOST = "hlxyxws.huanle.qq.com"
PB_RECORD_TYPE = 15815
COMMAND_VARIANTS = {
    14: 22, 112: 21, 3000: 1, 3001: 2, 3002: 3, 3003: 4, 3004: 5,
    3005: 6, 3006: 7, 3007: 8, 3008: 9, 3009: 10, 3010: 11, 3011: 12, 3019: 26,
}
INFORMATIONAL_COMMANDS = {14, 112, 3000, 3002, 3003, 3007, 3009, 3010, 3011, 3019}


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


def parse_command(command: int, group: dict[int, list[tuple[int, int | bytes]]], meta: SourceMeta) -> ProtocolEvent:
    if command == 3001:
        hand = tuple(wire.packed_field(group, 2))
        wire.require(wire.scalar(group, 1) == len(hand) == 27, "invalid initial hand")
        hint_present, hint = _optional_nested_scalar(group, 3)
        secondary = None
        secondary_present = False
        if 6 in group:
            try:
                nested = wire.fields(wire.one_blob(group, 6))
                secondary_present, secondary = _optional_nested_scalar(nested, 3)
            except (ValueError, TypeError):
                raise ValueError("invalid 3001 seat hint")
        if hint_present and secondary_present and hint != secondary:
            raise ValueError("conflicting 3001 seat hints")
        return DealEvent(
            my_seat=UNCONFIRMED_SEAT, level=wire.scalar(group, 4), hand=hand,
            first_seat_hint=hint if hint_present else secondary,
            first_seat_hint_present=hint_present or secondary_present,
            first_seat_hint_secondary=secondary if secondary_present else None,
            meta=meta,
        )
    if command == 3004:
        present, first_seat = _optional_nested_scalar(group, 1)
        return StartEvent(first_seat=first_seat if present else 0,
                          first_seat_present=present, meta=meta)
    if command == 3019:
        cards = _nested_field3_cards(group)
        if cards is None or not cards:
            return InformationalEvent(command=command, meta=meta)
        present, seat = _optional_nested_scalar(group, 2)
        return OwnSeatCandidateEvent(
            candidate_seat=seat if present else 0,
            candidate_seat_present=present,
            referenced_cards=cards,
            meta=meta,
        )
    if command in (3005, 3006):
        actor = wire.scalar(group, 2)
        raw_next = wire.scalar(group, 3)
        next_player = None if raw_next == (1 << 64) - 1 else raw_next
        cards = tuple(wire.packed_field(group, 7 if command == 3005 else 0))
        # 3006 has no card field in the observed protocol.
        if command == 3005:
            wire.require(len(cards) == wire.scalar(group, 6) and bool(cards), "play card count mismatch")
            remaining_after = wire.scalar(group, 5)
            combo = wire.scalar(group, 4)
            finish_present, finish_rank = wire.optional_scalar(group, 1)
            if not finish_present:
                finish_rank = None
            reset_flag = None
        else:
            remaining_after = None
            combo = None
            finish_rank = None
            reset_flag = wire.scalar(group, 4)
        return ActionEvent(player=actor, kind="play" if command == 3005 else "pass", cards=cards,
                           combo_type=combo, next_player=next_player,
                           remaining_after=remaining_after, finish_rank=finish_rank,
                           reset_flag=reset_flag, meta=meta)
    if command == 3008:
        hands: list[tuple[int, ...]] = []
        for raw_hand in wire.blobs(group, 13):
            item = wire.fields(raw_hand)
            cards = tuple(wire.packed_field(item, 2))
            wire.require(wire.scalar(item, 1) == len(cards), "settlement hand count mismatch")
            hands.append(cards)
        wire.require(len(hands) in (0, 4), "settlement must contain four hands when present")
        ranks = tuple(wire.packed_field(group, 3))
        return SettlementEvent(ranks_by_seat=ranks, remaining_hands=tuple(hands), meta=meta)
    if command == 3003:
        seat_snapshot, reported_values, reported_flag = _optional_3003_evidence(group)
        return InformationalEvent(command=command,
                                  reported_seat_snapshot=seat_snapshot,
                                  reported_values=reported_values,
                                  reported_flag=reported_flag,
                                  meta=meta)
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
        source = replace(meta, sub_index=int(record["sub_index"]))
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
