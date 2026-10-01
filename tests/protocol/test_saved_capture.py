from __future__ import annotations

from guance.infrastructure.protocol.live import GAME_HOST
from guance.infrastructure.protocol.saved_capture import iter_game_frames


def tnet(value) -> bytes:
    if isinstance(value, bytes):
        body, tag = value, b","
    elif isinstance(value, str):
        body, tag = value.encode("utf-8"), b";"
    elif isinstance(value, bool):
        body, tag = (b"true" if value else b"false"), b"!"
    elif isinstance(value, int):
        body, tag = str(value).encode("ascii"), b"#"
    elif isinstance(value, float):
        body, tag = repr(value).encode("ascii"), b"^"
    elif isinstance(value, list):
        body, tag = b"".join(tnet(item) for item in value), b"]"
    elif isinstance(value, dict):
        body = b"".join(tnet(key) + tnet(item) for key, item in value.items())
        tag = b"}"
    else:
        raise TypeError(type(value).__name__)
    return str(len(body)).encode("ascii") + b":" + body + tag


def test_saved_capture_marks_server_frames_explicitly(tmp_path) -> None:
    capture = {
        "request": {"host": GAME_HOST},
        "websocket": {
            "messages": [
                [2, False, b"server-payload", 123.5, False, False],
                [2, True, b"client-payload", 124.0, False, False],
            ]
        },
    }
    path = tmp_path / "capture.mitm"
    path.write_bytes(tnet(capture))

    frames = list(iter_game_frames(path))

    assert len(frames) == 1
    assert frames[0].payload == b"server-payload"
    assert frames[0].direction == "server"
