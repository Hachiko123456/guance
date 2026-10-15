"""Application model lifecycle facade using only stable ports and contracts."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import threading
from typing import Any

from guance.contracts import (
    CanonicalAdviceResult, CanonicalPublicGameView, GameObservation, ModelContractError,
    ModelLifecycleState, ModelStatusSnapshot, decode_canonical_advice,
)
from guance.ports.model import (
    MODEL_MESSAGES, ModelBackendError, ModelClient, ModelClientFactory, ModelSource,
    ResultCallback, StatusCallback, public_error, unavailable_client_factory,
)


class ModelService:
    """Own one selection epoch; detach eligibility before any fallible operation.

    The state mutex only protects in-memory ownership. Port calls and callbacks
    run outside it. A request admitted before revocation may finish, but its
    epoch/request identity cannot deliver into a later selection.
    """

    def __init__(self, source: ModelSource | None = None, *,
                 on_result: ResultCallback | None = None, on_status: StatusCallback | None = None,
                 client_factory: ModelClientFactory | None = None,
                 timeout_seconds: float = 2.0, threads: int = 2) -> None:
        self._on_result = on_result or (lambda result: None)
        self._on_status = on_status or (lambda status: None)
        self._factory = client_factory if client_factory is not None else unavailable_client_factory
        self._timeout, self._threads = timeout_seconds, threads
        self._lock = threading.RLock()
        self._calls = threading.RLock()
        self._client: ModelClient | None = None
        self._source: Path | None = None
        self._requested_source: ModelSource | None = None
        self._epoch = 0
        self._closed = False
        self._selection_failed = False
        self._cleanup_errors: list[str] = []
        self._latest_key: tuple[str, str, int] | None = None
        self._latest_request_id: str | None = None
        self._submission_token = 0
        self._pending_submission: int | None = None
        self._early_result: tuple[int, CanonicalAdviceResult] | None = None
        self._status = ModelStatusSnapshot(state=ModelLifecycleState.UNAVAILABLE,
            error_code='MODEL_NOT_LOADED', message=MODEL_MESSAGES['MODEL_NOT_LOADED'])
        if source is not None:
            self._select(source, start=False)

    @property
    def status(self) -> ModelStatusSnapshot:
        with self._lock:
            return self._status

    @property
    def selected_source(self) -> Path | None:
        with self._lock:
            return self._source

    @property
    def requested_source(self) -> ModelSource | None:
        with self._lock:
            return self._requested_source

    @property
    def active_source(self) -> Path | None:
        with self._lock:
            return (self._source if self._client is not None and not self._selection_failed
                    and self._status.state is ModelLifecycleState.READY else None)

    @property
    def selection_generation(self) -> int:
        with self._lock:
            return self._epoch

    @property
    def cleanup_errors(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._cleanup_errors)

    def _clear_requests(self) -> None:
        self._latest_key = None
        self._latest_request_id = None
        self._pending_submission = None
        self._early_result = None

    def _cleanup(self, client: ModelClient | None) -> bool:
        if client is None:
            return True
        errors = []
        acquired = self._calls.acquire(timeout=2.0)
        if acquired:
            try:
                for operation in (client.invalidate, client.close):
                    try:
                        operation()
                    except Exception:
                        errors.append('MODEL_CLEANUP_FAILED')
            finally:
                self._calls.release()
        else:
            errors.append('MODEL_CLEANUP_FAILED')
        if errors:
            with self._lock:
                self._cleanup_errors.extend(errors)
        return not errors

    def fail_selection(self, code: str, *, epoch: int | None = None,
                       state: ModelLifecycleState = ModelLifecycleState.ERROR,
                       metadata=None) -> None:
        code = code if code in MODEL_MESSAGES else 'MODEL_LOAD_FAILED'
        with self._lock:
            if self._closed or (epoch is not None and epoch != self._epoch):
                return
            self._epoch += 1
            self._selection_failed = True
            self._clear_requests()
            client, self._client = self._client, None
            args = dict(state=state, error_code=code, message=MODEL_MESSAGES[code], epoch=self._epoch)
            if metadata is not None:
                args['metadata'] = metadata
            self._status = notification = ModelStatusSnapshot(**args)
        # Publish revocation before potentially slow physical teardown.
        self._on_status(notification)
        self._cleanup(client)

    def _receive_status(self, epoch: int, status: Mapping[str, Any] | ModelStatusSnapshot) -> None:
        event = status.get('event') if isinstance(status, Mapping) else None
        try:
            snapshot = ModelStatusSnapshot.from_wire(status)
        except (TypeError, ValueError, ModelContractError):
            snapshot = ModelStatusSnapshot(state=ModelLifecycleState.ERROR,
                error_code='MODEL_OUTPUT_INVALID', message=MODEL_MESSAGES['MODEL_OUTPUT_INVALID'])
        with self._lock:
            if self._closed or self._selection_failed or epoch != self._epoch:
                return
            if event == 'stale_advice_dropped':
                notification = ModelStatusSnapshot(state=self._status.state,
                    message='已丢弃过期推荐', event=event, epoch=epoch)
            else:
                code = snapshot.error_code
                failed = snapshot.state in {ModelLifecycleState.ERROR,
                    ModelLifecycleState.UNAVAILABLE, ModelLifecycleState.TIMEOUT}
                if failed:
                    code = code or 'MODEL_INFERENCE_FAILED'
                message = MODEL_MESSAGES.get(code, '模型状态已更新')
                notification = ModelStatusSnapshot(state=snapshot.state, error_code=code,
                    message=message, metadata=snapshot.metadata, epoch=epoch,
                    generation=snapshot.generation)
                if not failed:
                    self._status = notification
        if event != 'stale_advice_dropped' and failed:
            self.fail_selection(code, epoch=epoch, state=snapshot.state, metadata=snapshot.metadata)
        else:
            self._on_status(notification)

    def _parse_result(self, result: CanonicalAdviceResult | Mapping[str, Any]) -> CanonicalAdviceResult:
        if isinstance(result, CanonicalAdviceResult):
            result.to_dict()
            return result
        if not isinstance(result, Mapping):
            raise ModelContractError('MODEL_OUTPUT_INVALID')
        return decode_canonical_advice(result)

    def _receive_result(self, epoch: int, result: CanonicalAdviceResult | Mapping[str, Any]) -> None:
        with self._lock:
            if self._closed or self._selection_failed or epoch != self._epoch:
                return
        try:
            parsed = self._parse_result(result)
        except Exception:
            self.fail_selection('MODEL_OUTPUT_INVALID', epoch=epoch)
            return
        with self._lock:
            if (self._closed or self._selection_failed or epoch != self._epoch
                    or parsed.key != self._latest_key):
                return
            if self._latest_request_id is None:
                if self._pending_submission is not None:
                    self._early_result = (self._pending_submission, parsed)
                return
            if parsed.request_id != self._latest_request_id:
                return
            invalid = parsed.status != 'ready' or bool(parsed.error_message) or bool(parsed.error_code)
        if invalid:
            self.fail_selection('MODEL_OUTPUT_INVALID', epoch=epoch)
            return
        self._on_result(parsed)

    def _select(self, source: ModelSource, *, start: bool) -> None:
        with self._lock:
            if self._closed:
                raise ModelBackendError('MODEL_CLOSED')
            self._epoch += 1
            epoch = self._epoch
            self._selection_failed = False
            self._requested_source = source
            self._source = None
            self._clear_requests()
            old, self._client = self._client, None
            self._status = notification = ModelStatusSnapshot(
                state=ModelLifecycleState.LOADING if start else ModelLifecycleState.UNINITIALIZED,
                epoch=epoch)
        self._on_status(notification)
        if not self._cleanup(old):
            self.fail_selection('MODEL_CLEANUP_FAILED', epoch=epoch)
            return
        try:
            if not isinstance(source, (str, Path)) or not str(source).strip():
                raise ModelBackendError('MODEL_SOURCE_INVALID')
            selected = Path(source).resolve()
        except Exception:
            self.fail_selection('MODEL_SOURCE_INVALID', epoch=epoch)
            return
        with self._lock:
            if self._closed or self._selection_failed or epoch != self._epoch:
                return
            self._source = selected
        try:
            client = self._factory(selected,
                on_result=lambda result: self._receive_result(epoch, result),
                on_status=lambda status: self._receive_status(epoch, status),
                timeout_seconds=self._timeout, threads=self._threads)
            with self._lock:
                accepted = not self._closed and not self._selection_failed and epoch == self._epoch
                if accepted:
                    self._client = client
            if not accepted:
                self._cleanup(client)
            elif start:
                self.start()
        except Exception as exc:
            self.fail_selection(public_error(exc, 'MODEL_LOAD_FAILED')['error_code'], epoch=epoch)

    def start(self) -> None:
        if not self._calls.acquire(timeout=2.0):
            self.fail_selection('MODEL_SELECTION_BUSY')
            return
        try:
            with self._lock:
                if self._closed:
                    raise ModelBackendError('MODEL_CLOSED')
                if self._selection_failed:
                    return
                client, epoch = self._client, self._epoch
            if client is None:
                self._receive_status(epoch, {'model_status': 'unavailable', 'error_code': 'MODEL_NOT_LOADED'})
            else:
                try:
                    client.start()
                except Exception as exc:
                    self.fail_selection(public_error(exc, 'MODEL_LOAD_FAILED')['error_code'], epoch=epoch)
        finally:
            self._calls.release()

    def submit(self, observation: GameObservation | CanonicalPublicGameView) -> None:
        with self._lock:
            if self._closed:
                return
            failed = self._selection_failed
            code = self._status.error_code
            client, epoch = self._client, self._epoch
        if failed:
            self.fail_selection(code, epoch=epoch, state=ModelLifecycleState.UNAVAILABLE)
            return
        if client is None:
            self._receive_status(epoch, {'model_status': 'unavailable', 'error_code': 'MODEL_NOT_LOADED'})
            return
        try:
            view = (CanonicalPublicGameView.from_observation(observation)
                    if isinstance(observation, GameObservation) else observation)
            view = CanonicalPublicGameView.from_dict(view.to_dict())
            with self._lock:
                if self._closed or self._selection_failed or epoch != self._epoch:
                    return
                self._submission_token += 1
                token = self._submission_token
                self._pending_submission = token
                self._early_result = None
                self._latest_key = view.key
                self._latest_request_id = None
            if not self._calls.acquire(timeout=2.0):
                self.fail_selection('MODEL_SELECTION_BUSY', epoch=epoch)
                return
            try:
                with self._lock:
                    if self._closed or self._selection_failed or epoch != self._epoch:
                        return
                request_id = client.submit(view)
            finally:
                self._calls.release()
            early = None
            with self._lock:
                if epoch == self._epoch and self._pending_submission == token:
                    self._latest_request_id = request_id if isinstance(request_id, str) and request_id else None
                    self._pending_submission = None
                    if self._early_result is not None and self._early_result[0] == token:
                        early = self._early_result[1]
                    self._early_result = None
            if early is not None and isinstance(request_id, str) and early.request_id == request_id:
                self._receive_result(epoch, early)
        except Exception as exc:
            self.fail_selection(public_error(exc, 'MODEL_INPUT_INVALID')['error_code'], epoch=epoch)

    def invalidate(self) -> None:
        with self._lock:
            self._clear_requests()
            client, epoch = self._client, self._epoch
        if client is not None:
            try:
                client.invalidate()
            except Exception:
                self.fail_selection('MODEL_CLEANUP_FAILED', epoch=epoch)

    def import_model(self, source: ModelSource) -> None:
        self._select(source, start=True)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._epoch += 1
            self._clear_requests()
            client, self._client = self._client, None
            self._status = ModelStatusSnapshot(state=ModelLifecycleState.CLOSED, epoch=self._epoch)
        self._cleanup(client)
