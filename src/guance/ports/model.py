"""Qt/process-free model ports and immutable application DTOs."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, TypeAlias

from guance.contracts import (
    CanonicalAdviceResult,
    CanonicalPublicGameView,
    GameObservation,
    ModelCapabilities,
    ModelMetadata,
    ModelStatusSnapshot,
    RequestIdentity,
)

ModelSource: TypeAlias = Path | str
ResultCallback: TypeAlias = Callable[[CanonicalAdviceResult], None]
StatusCallback: TypeAlias = Callable[[ModelStatusSnapshot], None]


MODEL_MESSAGES: Mapping[str, str] = MappingProxyType({
    "MODEL_PREFERENCES_FAILED": "Model selection could not be saved; recommendations are disabled. Select a model again.",
    "MODEL_CLEANUP_FAILED": "The previous model could not be cleaned up; recommendations are disabled.",
    "MODEL_SELECTION_BUSY": "Another model selection is still finishing; select a model again.",
    "MODEL_SOURCE_INVALID": "Model source is not a bundle or an explicit legacy NPZ.",
    "MANIFEST_INVALID": "Model manifest is missing required fields or has invalid values.",
    "MANIFEST_MISSING": "Model manifest is unavailable.",
    "ARTIFACT_PATH_INVALID": "Model artifact must stay inside its bundle.",
    "ARTIFACT_MISSING": "Required model artifact is unavailable.",
    "ARTIFACT_HASH_MISMATCH": "Model artifact integrity check failed.",
    "SHA256SUMS_INVALID": "Model checksum manifest is invalid.",
    "PROTOCOL_UNSUPPORTED": "Model protocol version is not supported.",
    "CONTRACT_UNSUPPORTED": "Model input or output contract is not supported.",
    "PROVIDER_UNKNOWN": "Model provider is not registered.",
    "PROVIDER_DUPLICATE": "Model provider is already registered.",
    "BACKEND_UNSUPPORTED": "Model backend is not supported by this provider.",
    "RUNTIME_UNSUPPORTED": "Model runtime is not supported by this provider.",
    "MODEL_VERSION_UNSUPPORTED": "Model rules or encoding version is not supported.",
    "MODEL_UNAVAILABLE": "The required model runtime is unavailable; no fallback is used.",
    "MODEL_LOAD_FAILED": "The required model could not be loaded.",
    "MODEL_NOT_LOADED": "No model has been loaded.",
    "MODEL_CLOSED": "The model service is closed.",
    "MODEL_INPUT_INVALID": "Public model input is invalid.",
    "MODEL_INPUT_UNSUPPORTED": "Public model input is unsupported or incomplete.",
    "MODEL_INPUT_AMBIGUOUS": "Public action semantics cannot be resolved uniquely.",
    "MODEL_INPUT_OR_INFERENCE": "The required model could not produce a safe recommendation.",
    "MODEL_OUTPUT_INVALID": "Model returned an invalid recommendation.",
    "MODEL_INFERENCE_FAILED": "The required model inference failed.",
    "MODEL_TIMEOUT": "Model inference timed out.",
    "PROTOCOL_INVALID": "Invalid model protocol message.",
    "PROTOCOL_TOO_LARGE": "Model protocol message exceeds the size limit.",
    "RESPONSE_MISMATCH": "Model response identity does not match the request.",
    "GUANDAN_STATE_UNAVAILABLE": "Public observation cannot supply the required Guandan environment state.",
    "GUANDAN_TOKEN_INVALID": "Guandan action tokens or legal mask are invalid.",
})


class ModelErrorCode(str, Enum):
    MODEL_PREFERENCES_FAILED = "MODEL_PREFERENCES_FAILED"
    MODEL_CLEANUP_FAILED = "MODEL_CLEANUP_FAILED"
    MODEL_SELECTION_BUSY = "MODEL_SELECTION_BUSY"
    MODEL_SOURCE_INVALID = "MODEL_SOURCE_INVALID"
    MANIFEST_INVALID = "MANIFEST_INVALID"
    MANIFEST_MISSING = "MANIFEST_MISSING"
    ARTIFACT_PATH_INVALID = "ARTIFACT_PATH_INVALID"
    ARTIFACT_MISSING = "ARTIFACT_MISSING"
    ARTIFACT_HASH_MISMATCH = "ARTIFACT_HASH_MISMATCH"
    SHA256SUMS_INVALID = "SHA256SUMS_INVALID"
    PROTOCOL_UNSUPPORTED = "PROTOCOL_UNSUPPORTED"
    CONTRACT_UNSUPPORTED = "CONTRACT_UNSUPPORTED"
    PROVIDER_UNKNOWN = "PROVIDER_UNKNOWN"
    PROVIDER_DUPLICATE = "PROVIDER_DUPLICATE"
    BACKEND_UNSUPPORTED = "BACKEND_UNSUPPORTED"
    RUNTIME_UNSUPPORTED = "RUNTIME_UNSUPPORTED"
    MODEL_VERSION_UNSUPPORTED = "MODEL_VERSION_UNSUPPORTED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MODEL_LOAD_FAILED = "MODEL_LOAD_FAILED"
    MODEL_NOT_LOADED = "MODEL_NOT_LOADED"
    MODEL_CLOSED = "MODEL_CLOSED"
    MODEL_INPUT_INVALID = "MODEL_INPUT_INVALID"
    MODEL_INPUT_UNSUPPORTED = "MODEL_INPUT_UNSUPPORTED"
    MODEL_INPUT_AMBIGUOUS = "MODEL_INPUT_AMBIGUOUS"
    MODEL_INPUT_OR_INFERENCE = "MODEL_INPUT_OR_INFERENCE"
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    MODEL_INFERENCE_FAILED = "MODEL_INFERENCE_FAILED"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    PROTOCOL_INVALID = "PROTOCOL_INVALID"
    PROTOCOL_TOO_LARGE = "PROTOCOL_TOO_LARGE"
    RESPONSE_MISMATCH = "RESPONSE_MISMATCH"
    GUANDAN_STATE_UNAVAILABLE = "GUANDAN_STATE_UNAVAILABLE"
    GUANDAN_TOKEN_INVALID = "GUANDAN_TOKEN_INVALID"


class ModelBackendError(ValueError):
    """Stable, bounded model failure shared by application and adapters."""

    request_identity: dict[str, Any] | None

    def __init__(self, code: str | ModelErrorCode) -> None:
        value = code.value if isinstance(code, ModelErrorCode) else code
        self.code = value if value in MODEL_MESSAGES else ModelErrorCode.MODEL_INFERENCE_FAILED.value
        self.request_identity = None
        super().__init__(MODEL_MESSAGES[self.code])


def public_error(error: BaseException, default: str = ModelErrorCode.MODEL_INFERENCE_FAILED.value) -> dict[str, str]:
    code = getattr(error, "code", default)
    if not isinstance(code, str) or code not in MODEL_MESSAGES:
        code = default if default in MODEL_MESSAGES else ModelErrorCode.MODEL_INFERENCE_FAILED.value
    return {"error_code": code, "message": MODEL_MESSAGES[code]}


class ModelAdvisor(Protocol):
    def load(self) -> ModelMetadata: ...
    def recommend(self, view: CanonicalPublicGameView, request_id: str) -> CanonicalAdviceResult: ...
    def close(self) -> None: ...


class ModelClient(Protocol):
    def start(self) -> None: ...
    def submit(self, observation: GameObservation | CanonicalPublicGameView) -> str | None: ...
    def invalidate(self) -> None: ...
    def close(self) -> None: ...


class ModelClientFactory(Protocol):
    def __call__(self, source: ModelSource, *, on_result: ResultCallback,
                 on_status: StatusCallback, timeout_seconds: float, threads: int) -> ModelClient: ...


class UnavailableModelClient:
    """Safe application-layer placeholder until a composition root injects a client."""

    def __init__(self, *, on_result: ResultCallback, on_status: StatusCallback, **_: Any) -> None:
        self._on_status = on_status
        self._closed = False

    def start(self) -> None:
        if not self._closed:
            self._on_status(ModelStatusSnapshot.from_wire({"model_status": "unavailable", "error_code": "MODEL_UNAVAILABLE"}))

    def submit(self, observation: GameObservation | CanonicalPublicGameView) -> str | None:
        if self._closed:
            raise ModelBackendError(ModelErrorCode.MODEL_CLOSED)
        self._on_status(ModelStatusSnapshot.from_wire({"model_status": "unavailable", "error_code": "MODEL_UNAVAILABLE"}))
        return None

    def invalidate(self) -> None:
        return None

    def close(self) -> None:
        self._closed = True


def unavailable_client_factory(source: ModelSource, *, on_result: ResultCallback,
                               on_status: StatusCallback, timeout_seconds: float,
                               threads: int) -> ModelClient:
    del source, timeout_seconds, threads
    return UnavailableModelClient(on_result=on_result, on_status=on_status)


class ModelServicePort(Protocol):
    @property
    def status(self) -> ModelStatusSnapshot: ...
    def submit(self, observation: GameObservation | CanonicalPublicGameView) -> None: ...
    def invalidate(self) -> None: ...
    def import_model(self, source: ModelSource) -> None: ...
    def close(self) -> None: ...


# Compatibility names are intentionally aliases, not a mutable/callable dict contract.
ModelStatus = ModelStatusSnapshot
