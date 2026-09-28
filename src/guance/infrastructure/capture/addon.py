"""The standalone mitmdump addon used by the runtime supervisor.

The addon deliberately emits only WebSocket payloads and safe metadata.  It
never emits headers, cookies, URLs with query strings, or proxy credentials.
The supervisor treats this stdout line protocol as untrusted input and bounds
it before forwarding a ``SourceFrame``.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from collections import defaultdict

try:
    from mitmproxy import http  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - loaded only by mitmdump
    http = object()  # type: ignore[assignment,misc]

_PREFIX = "GUANCE_FRAME "
_DIAG_PREFIX = "GUANCE_DIAG "
_MAX_DEFAULT = 1_048_576


def _allowed_hosts() -> set[str]:
    value = os.environ.get("GUANCE_ALLOWED_HOSTS", "")
    return {item.strip().lower() for item in value.split(",") if item.strip()}


def _max_frame_bytes() -> int:
    try:
        return max(256, int(os.environ.get("GUANCE_MAX_FRAME_BYTES", _MAX_DEFAULT)))
    except ValueError:
        return _MAX_DEFAULT


class GuanceAddon:
    def __init__(self) -> None:
        self._sequence: defaultdict[str, int] = defaultdict(int)
        self._allowed = _allowed_hosts()
        self._max_frame_bytes = _max_frame_bytes()

    def _emit(self, record: dict[str, object]) -> None:
        sys.stdout.write(_PREFIX + json.dumps(record, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    def websocket_message(self, flow: object) -> None:  # pragma: no cover - executed by mitmdump
        request = getattr(flow, "request", None)
        websocket = getattr(flow, "websocket", None)
        host = str(getattr(request, "pretty_host", "")).lower()
        if self._allowed and host not in self._allowed:
            return
        messages = getattr(websocket, "messages", ())
        if not messages:
            return
        message = messages[-1]
        content = getattr(message, "content", b"")
        if isinstance(content, str):
            content = content.encode("utf-8", errors="replace")
        content_size = len(content) if isinstance(content, (bytes, bytearray)) else 0
        if not isinstance(content, bytes) or content_size > self._max_frame_bytes:
            sys.stdout.write(
                _DIAG_PREFIX
                + json.dumps(
                    {
                        "level": "warning",
                        "code": "frame_too_large",
                        "bytes": content_size,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
            sys.stdout.flush()
            return
        connection_id = str(getattr(flow, "id", ""))[:96]
        sequence = self._sequence[connection_id]
        self._sequence[connection_id] += 1
        self._emit(
            {
                "host": host,
                "payload_b64": base64.b64encode(content).decode("ascii"),
                "received_at": time.time(),
                "connection_id": connection_id,
                "sequence": sequence,
                "direction": "client" if bool(getattr(message, "from_client", False)) else "server",
            }
        )


addons = [GuanceAddon()]
