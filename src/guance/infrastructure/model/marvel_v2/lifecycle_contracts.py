"""Pure contracts for offline MARVEL V2 history/cache lifecycle work.

The receipts in this module are deterministic metadata receipts. They never
contain model tensors, probabilities, event payloads, cards, prefixes, or
credentials and are not a real model KV cache.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from string import ascii_letters, digits
from typing import Iterable, Mapping

from .contracts import MarvelProtocolError, PHASE_TOKEN

MAX_ID_LENGTH = 128
MAX_HISTORY_STEPS = 4096
MAX_BRANCHES = 16
MAX_RECEIPTS = 32
MAX_PENDING_REQUESTS = 64
MAX_DIAGNOSTIC_CODE = 64
_SAFE_ID_CHARS = frozenset(ascii_letters + digits + "._:@-")
_HEX_CHARS = frozenset("0123456789abcdef")


class CacheLifecycleError(MarvelProtocolError):
    """A bounded lifecycle error that deliberately exposes only a safe code."""


def fail(code: str) -> None:
    if not isinstance(code, str) or not code or len(code) > MAX_DIAGNOSTIC_CODE:
        code = "cache_lifecycle_invalid"
    raise CacheLifecycleError(code)


def bounded_id(value: object, code: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > MAX_ID_LENGTH
            or any(char not in _SAFE_ID_CHARS for char in value)):
        fail(code)
    return value


def bounded_count(value: object, low: int, high: int, code: str) -> int:
    if type(value) is not int or not low <= value <= high:
        fail(code)
    return value


def sha256_hex(value: object, code: str) -> str:
    if (not isinstance(value, str) or len(value) != 64
            or any(char not in _HEX_CHARS for char in value)):
        fail(code)
    return value


def digest_fields(schema: str, fields: Iterable[tuple[str, str | int | bool]]) -> str:
    """Hash length-delimited scalar metadata without serialising secret payloads."""
    h = sha256()
    h.update(schema.encode("ascii"))
    for name, value in fields:
        key = name.encode("ascii")
        body = str(int(value) if isinstance(value, bool) else value).encode("utf-8")
        h.update(len(key).to_bytes(2, "big")); h.update(key)
        h.update(len(body).to_bytes(4, "big")); h.update(body)
    return h.hexdigest()


@dataclass(frozen=True, slots=True)
class HistoryCursor:
    phase: str
    actor: int
    token_step: int
    committed_step: int
    prefix_stage: int
    pending_exchange_count: int = 0
    context_bound: bool = True
    awaiting_play: bool = False

    def validate(self) -> None:
        if self.phase not in PHASE_TOKEN:
            fail("history_cursor_phase_invalid")
        bounded_count(self.actor, 0, 3, "history_cursor_actor_invalid")
        bounded_count(self.token_step, 0, 8191, "history_token_step_invalid")
        bounded_count(self.committed_step, 0, 8191, "history_committed_step_invalid")
        bounded_count(self.prefix_stage, 0, 32, "history_prefix_stage_invalid")
        bounded_count(self.pending_exchange_count, 0, 8,
                      "history_pending_exchange_invalid")
        if type(self.context_bound) is not bool:
            fail("history_context_bound_invalid")
        if type(self.awaiting_play) is not bool:
            fail("history_awaiting_play_invalid")
        if self.awaiting_play and self.pending_exchange_count:
            fail("history_awaiting_play_exchange_invalid")
        if self.prefix_stage and not self.context_bound:
            fail("history_prefix_context_invalid")
        if self.committed_step > self.token_step:
            fail("history_counter_order_invalid")


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    session_id: str
    game_id: str
    state_id: str

    def validate(self) -> None:
        bounded_id(self.session_id, "session_id_invalid")
        bounded_id(self.game_id, "game_id_invalid")
        bounded_id(self.state_id, "state_id_invalid")


@dataclass(frozen=True, slots=True)
class ModelArtifactIdentity:
    artifact_id: str
    artifact_sha256: str

    def validate(self) -> None:
        bounded_id(self.artifact_id, "artifact_id_invalid")
        sha256_hex(self.artifact_sha256, "artifact_hash_invalid")


@dataclass(frozen=True, slots=True)
class HistoryIdentity:
    session_id: str
    game_id: str
    state_id: str
    artifact_id: str
    artifact_sha256: str
    history_sha256: str
    branch_id: str
    event_count: int
    cursor: HistoryCursor
    exact: bool = True

    def validate(self) -> None:
        bounded_id(self.session_id, "history_session_id_invalid")
        bounded_id(self.game_id, "history_game_id_invalid")
        bounded_id(self.state_id, "history_state_id_invalid")
        bounded_id(self.artifact_id, "history_artifact_id_invalid")
        bounded_id(self.branch_id, "history_branch_id_invalid")
        sha256_hex(self.artifact_sha256, "history_artifact_hash_invalid")
        sha256_hex(self.history_sha256, "history_hash_invalid")
        bounded_count(self.event_count, 1, MAX_HISTORY_STEPS, "history_event_count_invalid")
        if type(self.cursor) is not HistoryCursor:
            fail("history_cursor_invalid")
        self.cursor.validate()
        if self.exact is not True:
            fail("exact_observer_history_required")

    @property
    def token_step(self) -> int:
        return self.cursor.token_step

    @property
    def committed_step(self) -> int:
        return self.cursor.committed_step

    @property
    def cache_key(self) -> str:
        self.validate()
        return digest_fields("guance-marvel-v2-cache-key-v1", (
            ("session", self.session_id), ("game", self.game_id),
            ("state", self.state_id), ("artifact", self.artifact_id),
            ("artifact_sha256", self.artifact_sha256),
            ("history_sha256", self.history_sha256), ("branch", self.branch_id),
            ("token_step", self.token_step), ("committed_step", self.committed_step),
            ("context_bound", self.cursor.context_bound),
            ("awaiting_play", self.cursor.awaiting_play),
        ))


@dataclass(frozen=True, slots=True)
class CacheRequestIdentity:
    request_id: str
    lifecycle_id: str
    request_key: str
    sequence: int
    generation: int
    history: HistoryIdentity

    @classmethod
    def create(cls, lifecycle_id: str, request_key: str, sequence: int,
               generation: int, history: HistoryIdentity) -> "CacheRequestIdentity":
        lifecycle_id = bounded_id(lifecycle_id, "cache_lifecycle_id_invalid")
        request_key = bounded_id(request_key, "cache_request_key_invalid")
        history.validate()
        result = cls(
            digest_fields("guance-marvel-v2-cache-request-v1", (
                ("lifecycle", lifecycle_id),
                ("session", history.session_id), ("game", history.game_id),
                ("artifact", history.artifact_id),
                ("artifact_sha256", history.artifact_sha256),
                ("history", history.history_sha256), ("branch", history.branch_id),
                ("key", request_key), ("generation", generation),
                ("sequence", sequence),
            )),
            lifecycle_id, request_key, sequence, generation, history,
        )
        result.validate(); return result

    def validate(self) -> None:
        sha256_hex(self.request_id, "cache_request_id_invalid")
        bounded_id(self.lifecycle_id, "cache_lifecycle_id_invalid")
        bounded_id(self.request_key, "cache_request_key_invalid")
        bounded_count(self.sequence, 1, 2**31 - 1, "cache_request_sequence_invalid")
        bounded_count(self.generation, 0, 2**31 - 1, "cache_generation_invalid")
        if type(self.history) is not HistoryIdentity:
            fail("cache_request_history_invalid")
        self.history.validate()
        expected = type(self).create_unchecked(
            self.lifecycle_id, self.request_key, self.sequence, self.generation, self.history
        )
        if self.request_id != expected:
            fail("cache_request_identity_invalid")

    @staticmethod
    def create_unchecked(lifecycle_id: str, request_key: str, sequence: int,
                         generation: int, history: HistoryIdentity) -> str:
        return digest_fields("guance-marvel-v2-cache-request-v1", (
            ("lifecycle", lifecycle_id),
            ("session", history.session_id), ("game", history.game_id),
            ("artifact", history.artifact_id),
            ("artifact_sha256", history.artifact_sha256),
            ("history", history.history_sha256), ("branch", history.branch_id),
            ("key", request_key), ("generation", generation),
            ("sequence", sequence),
        ))

    @property
    def request_key_sha256(self) -> str:
        return digest_fields("guance-marvel-v2-request-key-v1", (("key", self.request_key),))


@dataclass(frozen=True, slots=True)
class CacheReceipt:
    request_id: str
    lifecycle_id: str
    request_key_sha256: str
    generation: int
    history: HistoryIdentity
    content_sha256: str
    receipt_sha256: str

    @staticmethod
    def _content_hash(history: HistoryIdentity) -> str:
        return digest_fields("guance-marvel-v2-abstract-cache-content-v1", (
            ("cache_key", history.cache_key), ("history_sha256", history.history_sha256),
            ("events", history.event_count), ("token_step", history.token_step),
            ("committed_step", history.committed_step),
            ("phase", history.cursor.phase), ("actor", history.cursor.actor),
            ("prefix_stage", history.cursor.prefix_stage),
            ("pending_exchange_count", history.cursor.pending_exchange_count),
            ("context_bound", history.cursor.context_bound),
            ("awaiting_play", history.cursor.awaiting_play),
        ))

    @staticmethod
    def _receipt_hash(request_id: str, request_key_sha256: str, generation: int,
                      content_sha256: str) -> str:
        return digest_fields("guance-marvel-v2-abstract-cache-receipt-v1", (
            ("request_id", request_id), ("request_key", request_key_sha256),
            ("generation", generation), ("content", content_sha256),
        ))

    @classmethod
    def rebuild(cls, request: CacheRequestIdentity, history: HistoryIdentity) -> "CacheReceipt":
        request.validate(); history.validate()
        if request.history != history:
            fail("cache_rebuild_history_mismatch")
        content = cls._content_hash(history)
        receipt = cls._receipt_hash(
            request.request_id, request.request_key_sha256, request.generation, content
        )
        result = cls(request.request_id, request.lifecycle_id, request.request_key_sha256,
                     request.generation, history, content, receipt)
        result.validate()
        return result

    def validate(self) -> None:
        sha256_hex(self.request_id, "cache_receipt_request_invalid")
        bounded_id(self.lifecycle_id, "cache_lifecycle_id_invalid")
        sha256_hex(self.request_key_sha256, "cache_receipt_key_invalid")
        bounded_count(self.generation, 0, 2**31 - 1, "cache_receipt_generation_invalid")
        if type(self.history) is not HistoryIdentity:
            fail("cache_receipt_history_invalid")
        self.history.validate()
        sha256_hex(self.content_sha256, "cache_content_hash_invalid")
        sha256_hex(self.receipt_sha256, "cache_receipt_hash_invalid")
        expected_content = self._content_hash(self.history)
        expected_receipt = self._receipt_hash(
            self.request_id, self.request_key_sha256, self.generation, expected_content
        )
        if self.content_sha256 != expected_content or self.receipt_sha256 != expected_receipt:
            fail("cache_receipt_integrity_invalid")


@dataclass(frozen=True, slots=True)
class LifecycleDiagnostic:
    code: str
    operation: str
    status: str
    generation: int
    lifecycle_sha256: str
    request_sha256: str | None
    history_sha256: str | None
    branch_sha256: str | None
    event_count: int
    token_step: int
    committed_step: int
    pending_count: int
    receipt_count: int

    def validate(self) -> None:
        bounded_id(self.code, "diagnostic_code_invalid")
        bounded_id(self.operation, "diagnostic_operation_invalid")
        if self.status not in {"active", "closed", "crashed"}:
            fail("diagnostic_status_invalid")
        bounded_count(self.generation, 0, 2**31 - 1, "diagnostic_generation_invalid")
        sha256_hex(self.lifecycle_sha256, "diagnostic_lifecycle_hash_invalid")
        for value, code in ((self.request_sha256, "diagnostic_request_hash_invalid"),
                            (self.history_sha256, "diagnostic_history_hash_invalid"),
                            (self.branch_sha256, "diagnostic_branch_hash_invalid")):
            if value is not None:
                sha256_hex(value, code)
        for value, high, code in (
            (self.event_count, MAX_HISTORY_STEPS, "diagnostic_event_count_invalid"),
            (self.token_step, 8191, "diagnostic_token_step_invalid"),
            (self.committed_step, 8191, "diagnostic_committed_step_invalid"),
            (self.pending_count, MAX_PENDING_REQUESTS, "diagnostic_pending_count_invalid"),
            (self.receipt_count, MAX_RECEIPTS, "diagnostic_receipt_count_invalid"),
        ):
            bounded_count(value, 0, high, code)

    def as_dict(self) -> Mapping[str, str | int | None]:
        self.validate()
        return {
            "code": self.code, "operation": self.operation, "status": self.status,
            "generation": self.generation, "lifecycle_sha256": self.lifecycle_sha256,
            "request_sha256": self.request_sha256,
            "history_sha256": self.history_sha256, "branch_sha256": self.branch_sha256,
            "event_count": self.event_count, "token_step": self.token_step,
            "committed_step": self.committed_step,
            "pending_count": self.pending_count, "receipt_count": self.receipt_count,
        }


__all__ = [
    "MAX_BRANCHES", "MAX_HISTORY_STEPS", "MAX_PENDING_REQUESTS", "MAX_RECEIPTS",
    "CacheLifecycleError", "CacheReceipt", "CacheRequestIdentity", "HistoryCursor",
    "HistoryIdentity", "LifecycleDiagnostic", "ModelArtifactIdentity",
    "SessionIdentity", "bounded_count", "bounded_id", "digest_fields",
    "fail", "sha256_hex",
]
