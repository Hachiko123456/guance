"""Bounded, redacted diagnostics used by runtime components.

This module is intentionally independent of the file logger in
``guance.diagnostics``.  Capture helper output is untrusted and must be kept in
memory in a small ring before it is forwarded to the UI.
"""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


_SECRET_KEY = re.compile(
    r"(?:authorization|cookie|token|secret|password|passwd|"
    r"private[_-]?key|api[_-]?key|session|raw[_-]?(?:body|payload))",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?i)(bearer\s+|basic\s+|"
    r"(?:access[_-]?token|refresh[_-]?token|authorization|cookie)\s*[:=]\s*)"
    r"[^\s,;]+"
)
_QUERY = re.compile(r"(?i)(https?://[^\s?]+)\?[^\s]+")
_PEM = re.compile(r"-----BEGIN[^-]*PRIVATE KEY-----.*?-----END[^-]*PRIVATE KEY-----", re.DOTALL)
_URL_CREDENTIALS = re.compile(r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@")


def redact_text(value: str, limit: int = 512) -> str:
    """Redact credential-shaped values and bound the returned text."""
    value = _PEM.sub("<private-key-redacted>", str(value))
    value = _SECRET_VALUE.sub(lambda match: f"{match.group(1)}<redacted>", value)
    value = _QUERY.sub(r"\1?<redacted>", value)
    value = _URL_CREDENTIALS.sub(r"\1<redacted>@", value)
    if len(value) > limit:
        return f"{value[: max(0, limit - 17)]}…<truncated>"
    return value


def redact_value(value: Any, *, depth: int = 0, item_limit: int = 32) -> Any:
    """Recursively redact dictionaries and bound diagnostic values."""
    if depth > 4:
        return "<depth-limit>"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= item_limit:
                result["<more>"] = "<truncated>"
                break
            key_text = redact_text(str(key), 96)
            result[key_text] = "<redacted>" if _SECRET_KEY.search(key_text) else redact_value(
                item, depth=depth + 1, item_limit=item_limit
            )
        return result
    if isinstance(value, (list, tuple, set)):
        values = list(value)
        result = [
            redact_value(item, depth=depth + 1, item_limit=item_limit)
            for item in values[:item_limit]
        ]
        if len(values) > item_limit:
            result.append("<truncated>")
        return result
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_text(repr(value))


@dataclass(frozen=True, slots=True)
class DiagnosticRecord:
    timestamp: float
    level: str
    code: str
    message: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "data": self.data,
        }


class BoundedDiagnostics:
    """Thread-safe ring buffer for small, exportable diagnostic records."""

    def __init__(self, *, max_records: int = 200, max_message_chars: int = 512) -> None:
        if max_records < 1:
            raise ValueError("max_records must be positive")
        if max_message_chars < 32:
            raise ValueError("max_message_chars must be at least 32")
        self._records: deque[DiagnosticRecord] = deque(maxlen=max_records)
        self._max_message_chars = max_message_chars
        self._lock = threading.Lock()

    def add(
        self,
        level: str,
        code: str,
        message: str,
        data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = DiagnosticRecord(
            timestamp=time.time(),
            level=redact_text(str(level), 32),
            code=redact_text(str(code), 96),
            message=redact_text(str(message), self._max_message_chars),
            data=redact_value(dict(data or {})),
        )
        value = record.to_dict()
        with self._lock:
            self._records.append(record)
        return value

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [record.to_dict() for record in self._records]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


__all__ = ["BoundedDiagnostics", "DiagnosticRecord", "redact_text", "redact_value"]
