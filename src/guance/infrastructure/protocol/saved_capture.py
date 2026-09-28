"""Offline saved-capture reader for server WebSocket frames."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from guance.contracts import SourceFrame
from . import wire
from .live import GAME_HOST


def iter_game_frames(path: str | Path) -> Iterator[SourceFrame]:
    """Yield server binary WebSocket frames from a saved mitmproxy capture.

    This is intentionally an offline reader. It never opens a network socket,
    starts mitmproxy, changes system proxy settings, or replays a request.
    """
    capture = Path(path)
    sequence = 0
    connections = 0
    with capture.open("rb") as stream:
        while True:
            try:
                flow = wire.tnet_read(stream)
            except EOFError:
                break
            if not isinstance(flow, dict):
                continue
            request = wire.mapping_get(flow, "request")
            if not isinstance(request, dict) or wire.text(wire.mapping_get(request, "host", "")) != GAME_HOST:
                continue
            websocket = wire.mapping_get(flow, "websocket")
            if not isinstance(websocket, dict):
                continue
            connections += 1
            connection_id = f"{GAME_HOST}#{connections}"
            for message in wire.mapping_get(websocket, "messages", []) or []:
                if not isinstance(message, list) or len(message) < 6:
                    raise ValueError("unexpected saved WebSocket message format")
                opcode, from_client, body, timestamp, dropped, injected = message[:6]
                if from_client or dropped or injected or opcode != 2:
                    continue
                if not isinstance(body, bytes):
                    raise ValueError("server WebSocket payload is not bytes")
                yield SourceFrame(host=GAME_HOST, payload=body, received_at=float(timestamp),
                                  connection_id=connection_id, sequence=sequence)
                sequence += 1
    if connections == 0:
        raise ValueError("no target Daguandan WebSocket found in capture")
