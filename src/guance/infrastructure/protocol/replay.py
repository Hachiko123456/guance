"""Offline historical replay parser for the observed game-log schema."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
import zlib

from guance.domain.cards import card_parts
from guance.domain.events import ActionEvent, DealEvent, ProtocolEvent, SettlementEvent, SourceMeta
from . import wire

SOURCE_HOST = "guandangamelog.huanle.qq.com"
SOURCE_PREFIX = "/guandan/gamelog/raw/"


def extract_replay_bodies(path: str | Path) -> list[bytes]:
    """Extract replay HTTP bodies from a saved .mitm file, without network access."""
    source = Path(path)
    if source.suffix.lower() != ".mitm":
        return [source.read_bytes()]
    matches: list[bytes] = []
    with source.open("rb") as stream:
        while True:
            try:
                flow = wire.tnet_read(stream)
            except EOFError:
                break
            if not isinstance(flow, dict):
                continue
            request, response = wire.mapping_get(flow, "request"), wire.mapping_get(flow, "response")
            if not isinstance(request, dict) or not isinstance(response, dict):
                continue
            if wire.text(wire.mapping_get(request, "host", "")) != SOURCE_HOST:
                continue
            if not wire.text(wire.mapping_get(request, "path", "")).startswith(SOURCE_PREFIX):
                continue
            wire.require(wire.mapping_get(response, "status_code") == 200, "replay response is not HTTP 200")
            body = wire.mapping_get(response, "content", b"") or b""
            headers = {wire.text(k).lower(): wire.text(v).lower()
                       for k, v in (wire.mapping_get(response, "headers", []) or [])}
            encoding = headers.get("content-encoding", "identity")
            if encoding in ("gzip", "deflate"):
                decoder = zlib.decompressobj(31 if encoding == "gzip" else 15)
                body = decoder.decompress(body, wire.MAX_RECORD + 1)
                wire.require(len(body) <= wire.MAX_RECORD and decoder.eof, "incomplete replay compression")
            else:
                wire.require(encoding in ("", "identity"), "unsupported replay content encoding")
            matches.append(body)
    wire.require(bool(matches), "no successful replay download in capture")
    return matches


def decode_card_message(body: bytes) -> int:
    group = wire.fields(body)
    suit, rank, copy_index = (wire.scalar(group, n) for n in (1, 2, 3))
    return (copy_index << 8) | (suit << 4) | rank


def iter_replay_events(body: bytes, observer_seat: int | None = None) -> Iterator[ProtocolEvent]:
    """Yield normalized replay events from one decoded replay response body."""
    top = wire.fields(body)
    metadata = wire.fields(wire.one_blob(top, 1))
    teams = [wire.fields(item) for item in wire.blobs(metadata, 2)]
    frames = [wire.fields(item) for item in wire.blobs(top, 2)]
    initial: dict[int, tuple[int, ...]] = {}
    action_index = 0
    for frame_index, frame in enumerate(frames):
        for envelope in wire.blobs(frame, 1):
            event = wire.fields(envelope)
            wire.require(len(event) == 1, "unexpected replay event envelope")
            variant = next(iter(event))
            payload = wire.fields(wire.one_blob(event, variant))
            if variant == 11:
                for raw_hand in wire.blobs(payload, 1):
                    group = wire.fields(raw_hand)
                    seat = wire.scalar(group, 1)
                    cards = tuple(decode_card_message(raw) for raw in wire.blobs(group, 2))
                    for code in cards:
                        card_parts(code)
                    initial[seat] = cards
                continue
            if variant in (16, 18):
                if variant == 18:
                    yield SettlementEvent(ranks_by_seat=tuple(wire.scalar(payload, n) for n in (3,)),
                                           meta=SourceMeta(sequence=frame_index))
                continue
            if variant != 17:
                raise ValueError(f"unsupported replay event variant: {variant}")
            has_play, has_pass = 11 in payload, 12 in payload
            wire.require(has_play != has_pass, "ambiguous replay action")
            seat = wire.scalar(payload, 1)
            next_seat = wire.scalar(payload, 5)
            if has_play:
                play = wire.fields(wire.one_blob(payload, 11))
                cards = tuple(decode_card_message(raw) for raw in wire.blobs(play, 2))
                yield ActionEvent(player=seat, kind="play", cards=cards,
                                  combo_type=wire.scalar(play, 1), next_player=next_seat,
                                  source_index=action_index, trick=wire.scalar(payload, 3),
                                  meta=SourceMeta(sequence=frame_index))
            else:
                yield ActionEvent(player=seat, kind="pass", next_player=next_seat,
                                  source_index=action_index, trick=wire.scalar(payload, 3),
                                  meta=SourceMeta(sequence=frame_index))
            action_index += 1


def decode_replay_summary(body: bytes) -> dict[str, Any]:
    """Return a sanitized, non-secret structural summary for diagnostics/tests."""
    events = tuple(iter_replay_events(body))
    return {"event_count": len(events), "play_count": sum(getattr(e, "kind", "") == "play" for e in events),
            "pass_count": sum(getattr(e, "kind", "") == "pass" for e in events),
            "has_settlement": any(isinstance(e, SettlementEvent) for e in events)}
