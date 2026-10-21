"""Central inference device policy for the MARVEL V2 family.

The policy is the only place that decides between CPU and CUDA.  It separates
five different facts that must never be conflated:

* the machine has a CUDA-capable GPU (external inventory, not this module);
* the imported Torch build supports CUDA (``torch.version.cuda``);
* the current process can see/initialize a CUDA device;
* an actual CUDA execution probe succeeded (allocation + compute + synchronize);
* the device this model instance finally uses.

``auto`` may fall back to CPU only for explicitly classified availability
problems.  GPU execution errors (OOM, device assert, unknown failures) fail
closed and are never reported as "no GPU".  ``cuda`` never falls back.
"""
from __future__ import annotations

from dataclasses import dataclass
import importlib
from typing import Any, Callable

from .errors import ModelBackendError

LEGACY_CPU_RUNTIME_CONTRACT = "marvel_v2.cpu.v1"
DEVICE_RUNTIME_CONTRACT = "marvel_v2.device.v1"
RUNTIME_CONTRACTS = frozenset({LEGACY_CPU_RUNTIME_CONTRACT, DEVICE_RUNTIME_CONTRACT})
REQUESTED_DEVICES = ("auto", "cpu", "cuda")

# Stable, bounded reason codes. They are the only device diagnostics exposed.
CUDA_PROBE_OK = "CUDA_PROBE_OK"
FORCED_CPU = "FORCED_CPU"
LEGACY_CPU_CONTRACT = "LEGACY_CPU_CONTRACT"
CPU_ONLY_TORCH = "CPU_ONLY_TORCH"
NO_CUDA_DEVICE = "NO_CUDA_DEVICE"
CUDA_NOT_AVAILABLE = "CUDA_NOT_AVAILABLE"
CUDA_INIT_UNAVAILABLE = "CUDA_INIT_UNAVAILABLE"
CUDA_PROBE_OOM = "CUDA_PROBE_OOM"
CUDA_PROBE_FAILED = "CUDA_PROBE_FAILED"
TORCH_UNAVAILABLE = "TORCH_UNAVAILABLE"
CUDA_REQUIRED_UNAVAILABLE = "CUDA_REQUIRED_UNAVAILABLE"

REASON_CODES = frozenset({
    CUDA_PROBE_OK, FORCED_CPU, LEGACY_CPU_CONTRACT, CPU_ONLY_TORCH, NO_CUDA_DEVICE,
    CUDA_NOT_AVAILABLE, CUDA_INIT_UNAVAILABLE, CUDA_PROBE_OOM, CUDA_PROBE_FAILED,
    TORCH_UNAVAILABLE, CUDA_REQUIRED_UNAVAILABLE,
})

_DEVICE_PROBE_SIZE = 8
# Only genuine availability/initialization failures may make ``auto`` fall back
# to CPU.  Execution failures (OOM, device assert, illegal memory access,
# "CUDA error: unknown error", unclassified faults) are classified first and
# must always fail closed.  "CUDA error: unknown error" used to be listed here,
# which turned a GPU execution failure into a silent auto->CPU fallback.
_INIT_MARKERS = (
    "no cuda-capable device", "no kernel image is available", "driver version is insufficient",
    "cuda driver version", "cuda initialization", "initialization error", "invalid device ordinal",
    "not compiled with cuda", "cuda is not available", "device is not available",
    "cannot load", "library not found", "driver not found",
)
_OOM_MARKERS = ("out of memory",)
_ASSERT_MARKERS = ("device-side assert", "device side assert")
_EXECUTION_FAILURE_MARKERS = (
    "cuda error: unknown error", "unknown error", "illegal memory access",
    "unspecified launch failure", "launch failed",
)


@dataclass(frozen=True, slots=True)
class DeviceSelection:
    requested_device: str
    actual_device: str
    fallback: bool
    reason: str
    torch_cuda_build: str = "cpu"
    cuda_device_count: int = 0

    @property
    def actual_cuda(self) -> bool:
        return self.actual_device.startswith("cuda:")

    def metadata(self) -> dict[str, Any]:
        return {
            "requested_device": self.requested_device,
            "actual_device": self.actual_device,
            "device_fallback": self.fallback,
            "device_reason": self.reason,
            "torch_cuda_build": self.torch_cuda_build,
        }


def normalize_requested_device(value: Any) -> str:
    if not isinstance(value, str) or value not in REQUESTED_DEVICES:
        raise ModelBackendError("MODEL_INPUT_INVALID")
    return value


def _selection(requested: str, actual: str, *, fallback: bool, reason: str,
               build: str = "cpu", count: int = 0) -> DeviceSelection:
    return DeviceSelection(requested_device=requested, actual_device=actual,
                           fallback=bool(fallback), reason=reason,
                           torch_cuda_build=build or "cpu", cuda_device_count=count)


def _failure(code: str, selection: DeviceSelection) -> ModelBackendError:
    error = ModelBackendError(code)
    error.device_selection = selection
    return error


def _torch_cuda_build(torch_module: Any) -> str:
    value = getattr(getattr(torch_module, "version", None), "cuda", None)
    return str(value) if value else "cpu"


def _classify_probe_error(torch_module: Any, error: BaseException) -> str:
    text = str(error).lower()
    oom_type = getattr(getattr(torch_module, "cuda", None), "OutOfMemoryError", None)
    if isinstance(oom_type, type) and isinstance(error, oom_type):
        return CUDA_PROBE_OOM
    if any(marker in text for marker in _OOM_MARKERS):
        return CUDA_PROBE_OOM
    if any(marker in text for marker in _ASSERT_MARKERS):
        return CUDA_PROBE_FAILED
    # Known execution failures win over initialization wording: an unknown or
    # illegal GPU execution error is never "no GPU", even if the message also
    # mentions driver/initialization text.
    if any(marker in text for marker in _EXECUTION_FAILURE_MARKERS):
        return CUDA_PROBE_FAILED
    if any(marker in text for marker in _INIT_MARKERS):
        return CUDA_INIT_UNAVAILABLE
    return CUDA_PROBE_FAILED


def cuda_execution_probe(torch_module: Any) -> None:
    """Run a real CUDA allocation + matmul + synchronize, not just a boolean query."""
    try:
        device = torch_module.device("cuda:0")
        left = torch_module.ones((_DEVICE_PROBE_SIZE, _DEVICE_PROBE_SIZE),
                                 dtype=torch_module.float32, device=device)
        right = torch_module.ones((_DEVICE_PROBE_SIZE, _DEVICE_PROBE_SIZE),
                                   dtype=torch_module.float32, device=device)
        product = left @ right
        torch_module.cuda.synchronize(device)
        value = float(product[0, 0].item())
    except ModelBackendError:
        raise
    except Exception as exc:
        reason = _classify_probe_error(torch_module, exc)
        code = "MODEL_UNAVAILABLE" if reason == CUDA_INIT_UNAVAILABLE else "MODEL_INFERENCE_FAILED"
        raise _failure(code, _selection("", "", fallback=False, reason=reason,
                                        build=_torch_cuda_build(torch_module))) from None
    expected = float(_DEVICE_PROBE_SIZE)
    if value != expected:
        raise _failure("MODEL_INFERENCE_FAILED", _selection(
            "", "", fallback=False, reason=CUDA_PROBE_FAILED,
            build=_torch_cuda_build(torch_module)))


def _probe_failure(requested: str, reason: str, build: str, count: int) -> DeviceSelection:
    if reason == CUDA_INIT_UNAVAILABLE:
        if requested == "auto":
            return _selection(requested, "cpu", fallback=True, reason=reason,
                              build=build, count=count)
        raise _failure("MODEL_UNAVAILABLE", _selection(
            requested, "", fallback=False, reason=reason,
            build=build, count=count))
    # OOM/assert/unknown failures are execution errors, not "no GPU".
    raise _failure("MODEL_INFERENCE_FAILED", _selection(
        requested, "", fallback=False, reason=reason, build=build, count=count))


def select_device(requested_device: str = "auto", *, torch_module: Any | None = None,
                  probe: Callable[[Any], None] | None = None,
                  runtime_contract: str | None = None) -> DeviceSelection:
    """Select the inference device; this is the single policy entry point."""
    requested = normalize_requested_device(requested_device)
    contract = DEVICE_RUNTIME_CONTRACT if runtime_contract is None else runtime_contract
    if contract not in RUNTIME_CONTRACTS:
        raise ModelBackendError("MODEL_VERSION_UNSUPPORTED")
    if torch_module is None:
        try:
            torch_module = importlib.import_module("torch")
        except Exception:
            torch_module = None
    build = _torch_cuda_build(torch_module) if torch_module is not None else ""
    if contract == LEGACY_CPU_RUNTIME_CONTRACT:
        # The legacy CPU contract keeps its original CPU-only meaning.
        if requested == "cuda":
            raise _failure("MODEL_UNAVAILABLE", _selection(
                requested, "", fallback=False, reason=LEGACY_CPU_CONTRACT, build=build))
        return _selection(requested, "cpu", fallback=requested == "auto",
                          reason=LEGACY_CPU_CONTRACT, build=build)
    if requested == "cpu":
        return _selection(requested, "cpu", fallback=False, reason=FORCED_CPU, build=build)

    if torch_module is None:
        raise _failure("MODEL_UNAVAILABLE", _selection(
            requested, "", fallback=False, reason=TORCH_UNAVAILABLE)) from None
    if build == "cpu":
        if requested == "cuda":
            raise _failure("MODEL_UNAVAILABLE", _selection(
                requested, "", fallback=False, reason=CPU_ONLY_TORCH))
        return _selection(requested, "cpu", fallback=True, reason=CPU_ONLY_TORCH)
    try:
        count = int(torch_module.cuda.device_count())
    except Exception as exc:
        reason = _classify_probe_error(torch_module, exc)
        return _probe_failure(requested, reason, build, 0)
    try:
        available = bool(torch_module.cuda.is_available())
    except Exception as exc:
        reason = _classify_probe_error(torch_module, exc)
        return _probe_failure(requested, reason, build, count)
    if not available:
        reason = NO_CUDA_DEVICE if count == 0 else CUDA_NOT_AVAILABLE
        if requested == "cuda":
            raise _failure("MODEL_UNAVAILABLE", _selection(
                requested, "", fallback=False, reason=reason,
                build=build, count=count))
        return _selection(requested, "cpu", fallback=True, reason=reason,
                          build=build, count=count)
    try:
        (probe or cuda_execution_probe)(torch_module)
    except ModelBackendError as exc:
        reason = getattr(getattr(exc, "device_selection", None), "reason", CUDA_PROBE_FAILED)
        return _probe_failure(requested, str(reason), build, count)
    except Exception as exc:
        return _probe_failure(requested, _classify_probe_error(torch_module, exc), build, count)
    return _selection(requested, "cuda:0", fallback=False, reason=CUDA_PROBE_OK,
                      build=build, count=count)


__all__ = [
    "CUDA_NOT_AVAILABLE", "CUDA_INIT_UNAVAILABLE", "CUDA_PROBE_FAILED", "CUDA_PROBE_OOM",
    "CUDA_PROBE_OK", "CUDA_REQUIRED_UNAVAILABLE", "CPU_ONLY_TORCH", "DEVICE_RUNTIME_CONTRACT",
    "DeviceSelection", "FORCED_CPU", "LEGACY_CPU_CONTRACT", "NO_CUDA_DEVICE", "REASON_CODES",
    "REQUESTED_DEVICES", "RUNTIME_CONTRACTS", "TORCH_UNAVAILABLE", "cuda_execution_probe",
    "normalize_requested_device", "select_device",
]
