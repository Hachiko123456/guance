"""Offline deterministic abstract cache lifecycle for MARVEL V2.

This state machine owns only identities, generations and metadata receipts. It
never creates, stores or claims parity for a real model KV tensor.
"""
from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from threading import RLock

from .history import ExactHistoryJournal, SpeculativeHistoryBranch
from .lifecycle_contracts import (
    MAX_PENDING_REQUESTS, MAX_RECEIPTS, CacheLifecycleError, CacheReceipt, CacheRequestIdentity,
    HistoryIdentity, LifecycleDiagnostic, bounded_count, bounded_id,
    digest_fields, fail, sha256_hex,
)

HistorySource = ExactHistoryJournal | SpeculativeHistoryBranch
ReceiptBuilder = Callable[[CacheRequestIdentity, HistoryIdentity], CacheReceipt]


def _locked(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped


class AbstractCacheLifecycle:
    """Generation-based stale barrier for deterministic abstract receipts."""

    def __init__(self, *, lifecycle_id: str, session_id: str, game_id: str,
                 artifact_id: str, artifact_sha256: str,
                 max_receipts: int = MAX_RECEIPTS,
                 max_pending: int = MAX_PENDING_REQUESTS,
                 initial_sequence: int = 0) -> None:
        self._lifecycle_id = bounded_id(lifecycle_id, "cache_lifecycle_id_invalid")
        self._session_id = bounded_id(session_id, "cache_session_id_invalid")
        self._game_id = bounded_id(game_id, "cache_game_id_invalid")
        self._artifact_id = bounded_id(artifact_id, "cache_artifact_id_invalid")
        self._artifact_sha256 = sha256_hex(artifact_sha256, "cache_artifact_hash_invalid")
        self._max_receipts = bounded_count(max_receipts, 1, MAX_RECEIPTS,
                                           "cache_receipt_limit_invalid")
        self._max_pending = bounded_count(max_pending, 1, MAX_PENDING_REQUESTS,
                                          "cache_pending_limit_invalid")
        self._lock = RLock()
        self._generation = 0
        self._sequence = bounded_count(initial_sequence, 0, 2**31 - 2,
                                       "cache_request_sequence_invalid")
        self._status = "active"
        self._pending: dict[str, CacheRequestIdentity] = {}
        self._latest_by_key: dict[str, str] = {}
        self._receipts: dict[str, CacheReceipt] = {}
        self._last_diagnostic = self._diagnostic("initialized", "initialize")

    @classmethod
    def from_history(cls, source: HistorySource, *, lifecycle_id: str,
                     session_id: str, game_id: str, artifact_id: str,
                     artifact_sha256: str,
                     request_key: str = "initial", **kwargs) -> "AbstractCacheLifecycle":
        lifecycle = cls(lifecycle_id=lifecycle_id, session_id=session_id,
                        game_id=game_id, artifact_id=artifact_id,
                        artifact_sha256=artifact_sha256, **kwargs)
        lifecycle.run_rebuild(source, request_key=request_key)
        return lifecycle

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def receipt_count(self) -> int:
        with self._lock:
            return len(self._receipts)

    @property
    def last_diagnostic(self) -> LifecycleDiagnostic:
        with self._lock:
            return self._last_diagnostic

    @_locked
    def is_pending(self, request: CacheRequestIdentity) -> bool:
        return self._pending.get(request.request_id) == request

    @staticmethod
    def _history_identity(source: HistorySource) -> HistoryIdentity:
        if type(source) is ExactHistoryJournal:
            return source.identity
        if type(source) is SpeculativeHistoryBranch:
            return source.identity
        fail("cache_history_source_invalid")

    def _ensure_active(self) -> None:
        if self._status != "active":
            fail(f"cache_lifecycle_{self._status}")

    def _ensure_history_owner(self, history: HistoryIdentity) -> None:
        history.validate()
        if (history.session_id != self._session_id or history.game_id != self._game_id
                or history.artifact_id != self._artifact_id
                or history.artifact_sha256 != self._artifact_sha256):
            fail("cache_history_identity_mismatch")

    def _diagnostic(self, code: str, operation: str,
                    history: HistoryIdentity | None = None,
                    request: CacheRequestIdentity | None = None) -> LifecycleDiagnostic:
        result = LifecycleDiagnostic(
            code=code, operation=operation, status=self._status,
            generation=self._generation,
            lifecycle_sha256=digest_fields(
                "guance-marvel-v2-lifecycle-diagnostic-v1",
                (("lifecycle", self._lifecycle_id),),
            ),
            request_sha256=request.request_id if request is not None else None,
            history_sha256=history.history_sha256 if history is not None else None,
            branch_sha256=(digest_fields("guance-marvel-v2-branch-diagnostic-v1",
                                        (("branch", history.branch_id),))
                           if history is not None else None),
            event_count=history.event_count if history is not None else 0,
            token_step=history.token_step if history is not None else 0,
            committed_step=history.committed_step if history is not None else 0,
            pending_count=len(self._pending), receipt_count=len(self._receipts),
        )
        result.validate(); return result

    def _record(self, code: str, operation: str,
                history: HistoryIdentity | None = None,
                request: CacheRequestIdentity | None = None) -> LifecycleDiagnostic:
        self._last_diagnostic = self._diagnostic(code, operation, history, request)
        return self._last_diagnostic

    @staticmethod
    def _request_slot(request_key: str, history: HistoryIdentity) -> str:
        return digest_fields("guance-marvel-v2-request-slot-v1", (
            ("key", request_key), ("cache_key", history.cache_key),
        ))

    @_locked
    def begin_request(self, source: HistorySource, *, request_key: str) -> CacheRequestIdentity:
        self._ensure_active()
        history = self._history_identity(source)
        self._ensure_history_owner(history)
        key = bounded_id(request_key, "cache_request_key_invalid")
        slot = self._request_slot(key, history)
        previous = self._latest_by_key.get(slot)
        if previous is not None:
            self._pending.pop(previous, None)
        if len(self._pending) >= self._max_pending:
            fail("cache_pending_overflow")
        if self._sequence >= 2**31 - 1:
            fail("cache_request_sequence_overflow")
        self._sequence += 1
        request = CacheRequestIdentity.create(
            self._lifecycle_id, key, self._sequence, self._generation, history
        )
        self._pending[request.request_id] = request
        self._latest_by_key[slot] = request.request_id
        self._record("request_started", "begin_request", history, request)
        return request

    def _discard_request(self, request: CacheRequestIdentity) -> None:
        self._pending.pop(request.request_id, None)
        slot = self._request_slot(request.request_key, request.history)
        if self._latest_by_key.get(slot) == request.request_id:
            self._latest_by_key.pop(slot, None)

    @_locked
    def build_receipt(self, request: CacheRequestIdentity,
                      source: HistorySource) -> CacheReceipt:
        history = self._history_identity(source)
        self._ensure_history_owner(history)
        if request.history != history:
            fail("cache_rebuild_history_mismatch")
        return CacheReceipt.rebuild(request, history)

    @_locked
    def complete(self, request: CacheRequestIdentity, receipt: CacheReceipt) -> CacheReceipt:
        if type(request) is not CacheRequestIdentity or type(receipt) is not CacheReceipt:
            fail("cache_result_type_invalid")
        request.validate(); receipt.validate()
        self._ensure_active()
        pending = self._pending.get(request.request_id)
        if pending != request:
            fail("cache_result_stale_or_foreign")
        slot = self._request_slot(request.request_key, request.history)
        if self._latest_by_key.get(slot) != request.request_id:
            self._pending.pop(request.request_id, None)
            self._record("request_superseded", "complete", request.history, request)
            fail("cache_request_superseded")
        if request.generation != self._generation:
            self._pending.pop(request.request_id, None)
            fail("cache_generation_stale")
        if (receipt.request_id != request.request_id
                or receipt.lifecycle_id != request.lifecycle_id
                or receipt.request_key_sha256 != request.request_key_sha256
                or receipt.generation != request.generation
                or receipt.history != request.history):
            fail("cache_result_identity_mismatch")
        cache_key = request.history.cache_key
        if cache_key not in self._receipts and len(self._receipts) >= self._max_receipts:
            self._pending.pop(request.request_id, None)
            if self._latest_by_key.get(slot) == request.request_id:
                self._latest_by_key.pop(slot, None)
            self._record("receipt_overflow", "complete", request.history, request)
            fail("cache_receipt_overflow")
        self._receipts[cache_key] = receipt
        self._pending.pop(request.request_id, None)
        if self._latest_by_key.get(slot) == request.request_id:
            self._latest_by_key.pop(slot, None)
        self._record("request_completed", "complete", request.history, request)
        return receipt

    def run_rebuild(self, source: HistorySource, *, request_key: str,
                    builder: ReceiptBuilder | None = None) -> CacheReceipt:
        """Register before invoking a possibly synchronous callback."""
        request = self.begin_request(source, request_key=request_key)
        history = self._history_identity(source)
        try:
            receipt = (CacheReceipt.rebuild(request, history) if builder is None
                       else builder(request, history))
            return self.complete(request, receipt)
        except CacheLifecycleError:
            with self._lock:
                self._discard_request(request)
            raise
        except Exception:
            with self._lock:
                self._discard_request(request)
                self._record("request_builder_failed", "run_rebuild", history, request)
            fail("cache_receipt_builder_failed")


    @_locked
    def receipt_for(self, source: HistorySource) -> CacheReceipt:
        self._ensure_active()
        history = self._history_identity(source)
        self._ensure_history_owner(history)
        receipt = self._receipts.get(history.cache_key)
        if receipt is None or receipt.generation != self._generation:
            fail("cache_receipt_unavailable")
        return receipt

    def _transition(self, operation: str, *, status: str = "active") -> LifecycleDiagnostic:
        if self._generation >= 2**31 - 1:
            fail("cache_generation_overflow")
        self._generation += 1
        self._pending.clear(); self._latest_by_key.clear(); self._receipts.clear()
        self._status = status
        return self._record(f"cache_{operation}", operation)

    @_locked
    def invalidate(self) -> LifecycleDiagnostic:
        self._ensure_active()
        return self._transition("invalidated")

    @_locked
    def reload(self, *, artifact_id: str, artifact_sha256: str) -> LifecycleDiagnostic:
        self._ensure_active()
        next_id = bounded_id(artifact_id, "cache_artifact_id_invalid")
        next_hash = sha256_hex(artifact_sha256, "cache_artifact_hash_invalid")
        if self._generation >= 2**31 - 1:
            fail("cache_generation_overflow")
        self._generation += 1
        self._pending.clear(); self._latest_by_key.clear(); self._receipts.clear()
        self._artifact_id = next_id; self._artifact_sha256 = next_hash
        return self._record("cache_reloaded", "reloaded")

    @_locked
    def restart(self) -> LifecycleDiagnostic:
        if self._status == "closed":
            fail("cache_lifecycle_closed")
        return self._transition("restarted", status="active")

    @_locked
    def close(self) -> LifecycleDiagnostic:
        if self._status == "closed":
            fail("cache_lifecycle_closed")
        return self._transition("closed", status="closed")

    @_locked
    def crash(self) -> LifecycleDiagnostic:
        if self._status == "closed":
            fail("cache_lifecycle_closed")
        return self._transition("crashed", status="crashed")

    @_locked
    def timeout(self, request: CacheRequestIdentity) -> LifecycleDiagnostic:
        self._ensure_active()
        if self._pending.get(request.request_id) != request:
            fail("cache_timeout_request_stale")
        return self._transition("timeout")


CacheLifecycle = AbstractCacheLifecycle
AbstractCacheReceipt = CacheReceipt
CacheRequest = CacheRequestIdentity

__all__ = [
    "AbstractCacheLifecycle", "CacheLifecycle", "AbstractCacheReceipt", "CacheRequest",
    "HistorySource", "ReceiptBuilder",
]
