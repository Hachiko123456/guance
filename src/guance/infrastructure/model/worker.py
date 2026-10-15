"""Generic JSONL worker. Provider selection belongs exclusively to the factory."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import os
import sys
from typing import Any, Callable, TextIO, TypeVar

from guance.contracts import (CanonicalPublicGameView, ModelMetadata, decode_canonical_advice,
                               validate_advice_for_view)
from guance.ports.model import ModelAdvisor, ModelBackendError, public_error
from .codec import (IDENTITY_FIELDS, MAX_LINE, WORKER_PROTOCOL_VERSION, dumps, envelope,
                    identity, read_message, safe_metadata as _metadata, validate_envelope)
from .factory import ModelFactory

# Legacy imports from worker remain compatibility aliases; the implementation lives in codec.py.
PROTOCOL_VERSION = WORKER_PROTOCOL_VERSION
_T = TypeVar("_T")


def _provider_call(call: Callable[[], _T]) -> _T:
    # Providers are in-process trusted code, but accidental diagnostics are not wire data.
    with open(os.devnull, "w", encoding="utf-8") as sink:
        with redirect_stdout(sink), redirect_stderr(sink):
            return call()


def run(weights_path: str = "", threads: int = 2, *, input_stream: TextIO | None = None,
        output_stream: TextIO | None = None, factory: ModelFactory | None = None) -> int:
    """Optional startup source retains the old CLI hook; new clients send an explicit load."""
    for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[key] = str(max(1, min(threads, 4)))
    source = input_stream if input_stream is not None else sys.stdin
    target = output_stream if output_stream is not None else sys.stdout
    factory = factory if factory is not None else ModelFactory()
    advisor: ModelAdvisor | None = None
    control = {"request_id": "startup", "session_id": "", "game_id": "", "state_version": 0}

    def emit(kind: str, ids: dict[str, Any], **body: Any) -> None:
        target.write(dumps(envelope(kind, ids, **body)))
        target.flush()

    def unload() -> None:
        nonlocal advisor
        old, advisor = advisor, None
        if old is not None:
            try:
                _provider_call(old.close)
            except Exception:
                pass  # Never publish cleanup exceptions or continue using the old provider.

    def load(model_source: str, ids: dict[str, Any]) -> bool:
        nonlocal advisor
        unload()
        emit("status", ids, model_status="loading")
        try:
            advisor = _provider_call(lambda: factory.create(model_source))
            metadata = _provider_call(advisor.load)
            emit("loaded", ids, metadata=_metadata(metadata))
            return True
        except Exception as exc:
            unload()
            emit("error", ids, fatal=False, **public_error(exc, "MODEL_LOAD_FAILED"))
            return False

    try:
        if weights_path and not load(weights_path, control):
            return 2
        while True:
            ids = control
            try:
                item = read_message(source)
                if item is None:
                    return 0
                ids = identity(item)
                kind = item["type"]
                extra = {"load": {"source"}, "reload": {"source"},
                         "recommend": {"observation"}, "close": set(), "stop": set()}.get(kind)
                if extra is None or set(item) != {"protocol_version", "type", *IDENTITY_FIELDS} | extra:
                    raise ModelBackendError("PROTOCOL_INVALID")
                if kind in {"close", "stop"}:
                    unload()
                    emit("status", ids, model_status="closed" if kind == "close" else "stopped")
                    return 0
                if kind in {"load", "reload"}:
                    if not isinstance(item["source"], str) or not item["source"] or len(item["source"]) > 4096:
                        raise ModelBackendError("PROTOCOL_INVALID")
                    load(item["source"], ids)
                    continue
                view = CanonicalPublicGameView.from_dict(item["observation"])
                if (view.session_id, view.game_id, view.state_version) != (
                        ids["session_id"], ids["game_id"], ids["state_version"]):
                    raise ModelBackendError("RESPONSE_MISMATCH")
            except Exception as exc:
                ids = getattr(exc, "request_identity", None) or ids
                emit("error", ids, fatal=True, **public_error(exc, "PROTOCOL_INVALID"))
                return 3
            try:
                if advisor is None:
                    raise ModelBackendError("MODEL_NOT_LOADED")
                result = _provider_call(lambda: advisor.recommend(view, ids["request_id"]))
                if result.key != view.key or result.request_id != ids["request_id"]:
                    raise ModelBackendError("RESPONSE_MISMATCH")
                if result.status != "ready":
                    raise ModelBackendError(result.error_code or "MODEL_INFERENCE_FAILED")
                validate_advice_for_view(view, result)
                # Success never carries provider diagnostics in the legacy error fields.
                safe_result = replace(result, error_message="", error_code="").to_dict()
                # Reparse the canonical result before crossing the wire. Legacy adapters
                # are only called by the provider boundary, never by this generic decoder.
                decode_canonical_advice(safe_result)
                emit("result", ids, result=safe_result)
            except Exception as exc:
                emit("error", ids, fatal=False, **public_error(exc))
    finally:
        unload()


if __name__ == "__main__":
    raise SystemExit(run())
