"""Device selection policy truth table for GPU-first inference.

Every case here is a complete branch of the policy, not a single counterexample:
requested device x torch CUDA build x device visibility x execution probe outcome
x runtime contract. GPU execution failures must never be reported as "no GPU".
"""
from __future__ import annotations

import pytest

from guance.infrastructure.model.device_policy import (
    DEVICE_RUNTIME_CONTRACT, LEGACY_CPU_RUNTIME_CONTRACT, cuda_execution_probe,
    normalize_requested_device, select_device,
)
from guance.infrastructure.model.errors import ModelBackendError


class _Version:
    def __init__(self, cuda):
        self.cuda = cuda


class _Cuda:
    def __init__(self, *, available=True, count=1, probe_error=None):
        self._available = available
        self._count = count
        self.probe_error = probe_error
        self.probe_called = False

    def is_available(self):
        if isinstance(self._available, BaseException):
            raise self._available
        return self._available

    def device_count(self):
        return self._count


class _FakeTorch:
    def __init__(self, *, cuda_build="12.6", available=True, count=1, probe_error=None):
        self.version = _Version(cuda_build)
        self.cuda = _Cuda(available=available, count=count, probe_error=probe_error)


def _select(requested, torch=None, *, probe=None, contract=DEVICE_RUNTIME_CONTRACT):
    return select_device(requested, torch_module=torch, probe=probe,
                         runtime_contract=contract)


def test_auto_uses_cuda_only_after_successful_execution_probe():
    torch = _FakeTorch()
    calls = []

    def probe(module):
        calls.append(module)
        torch.cuda.probe_called = True

    selection = _select("auto", torch, probe=probe)
    assert calls == [torch]
    assert selection.actual_device == "cuda:0"
    assert selection.fallback is False
    assert selection.reason == "CUDA_PROBE_OK"
    assert selection.torch_cuda_build == "12.6"


def test_auto_cpu_only_torch_is_distinct_from_missing_device():
    torch = _FakeTorch(cuda_build=None, available=False, count=0)
    selection = _select("auto", torch, probe=lambda _module: pytest.fail("probe must not run"))
    assert selection.actual_device == "cpu"
    assert selection.fallback is True
    assert selection.reason == "CPU_ONLY_TORCH"
    assert selection.torch_cuda_build == "cpu"


def test_auto_cuda_build_without_visible_device_falls_back_with_own_reason():
    torch = _FakeTorch(available=False, count=0)
    selection = _select("auto", torch, probe=lambda _module: pytest.fail("probe must not run"))
    assert selection.actual_device == "cpu"
    assert selection.fallback is True
    assert selection.reason == "NO_CUDA_DEVICE"


def test_auto_cuda_build_device_present_but_unavailable_falls_back_with_own_reason():
    torch = _FakeTorch(available=False, count=1)
    selection = _select("auto", torch, probe=lambda _module: pytest.fail("probe must not run"))
    assert selection.actual_device == "cpu"
    assert selection.reason == "CUDA_NOT_AVAILABLE"


def test_cpu_never_probes_or_initializes_cuda_even_when_gpu_is_available():
    torch = _FakeTorch()
    selection = _select("cpu", torch, probe=lambda _module: pytest.fail("forced CPU must not probe"))
    assert selection.actual_device == "cpu"
    assert selection.fallback is False
    assert selection.reason == "FORCED_CPU"
    assert torch.cuda.probe_called is False


def test_cuda_request_with_cpu_only_torch_fails_closed_without_cpu_fallback():
    torch = _FakeTorch(cuda_build=None, available=False, count=0)
    with pytest.raises(ModelBackendError) as exc:
        _select("cuda", torch, probe=lambda _module: pytest.fail("probe must not run"))
    assert exc.value.code == "MODEL_UNAVAILABLE"
    assert exc.value.device_selection.reason == "CPU_ONLY_TORCH"
    assert exc.value.device_selection.actual_device == ""
    assert exc.value.device_selection.fallback is False


def test_cuda_request_probe_initialization_failure_is_explicit_and_never_cpu():
    torch = _FakeTorch()
    def probe(_module):
        raise RuntimeError("CUDA driver version is insufficient for CUDA runtime version")
    with pytest.raises(ModelBackendError) as exc:
        _select("cuda", torch, probe=probe)
    assert exc.value.code == "MODEL_UNAVAILABLE"
    assert exc.value.device_selection.reason == "CUDA_INIT_UNAVAILABLE"


def test_auto_probe_initialization_failure_may_fall_back_to_cpu_with_reason():
    torch = _FakeTorch()
    def probe(_module):
        raise RuntimeError("no kernel image is available for execution on the device")
    selection = _select("auto", torch, probe=probe)
    assert selection.actual_device == "cpu"
    assert selection.fallback is True
    assert selection.reason == "CUDA_INIT_UNAVAILABLE"


def test_auto_probe_oom_is_a_gpu_execution_error_not_no_gpu():
    torch = _FakeTorch()
    def probe(_module):
        raise RuntimeError("CUDA out of memory. Tried to allocate 64.00 MiB")
    with pytest.raises(ModelBackendError) as exc:
        _select("auto", torch, probe=probe)
    assert exc.value.code == "MODEL_INFERENCE_FAILED"
    assert exc.value.device_selection.reason == "CUDA_PROBE_OOM"
    assert exc.value.device_selection.actual_device == ""


def test_auto_probe_unknown_error_fails_closed_never_classified_as_no_gpu():
    torch = _FakeTorch()
    def probe(_module):
        raise RuntimeError("CUDA error: device-side assert triggered")
    with pytest.raises(ModelBackendError) as exc:
        _select("auto", torch, probe=probe)
    assert exc.value.code == "MODEL_INFERENCE_FAILED"
    assert exc.value.device_selection.reason == "CUDA_PROBE_FAILED"


def test_availability_query_initialization_error_is_classified_not_swallowed():
    torch = _FakeTorch(available=RuntimeError("CUDA initialization: driver not found"))
    selection = _select("auto", torch, probe=lambda _module: pytest.fail("probe must not run"))
    assert selection.actual_device == "cpu"
    assert selection.reason == "CUDA_INIT_UNAVAILABLE"


def test_availability_query_unknown_error_fails_closed():
    torch = _FakeTorch(available=RuntimeError("unexpected backend fault"))
    with pytest.raises(ModelBackendError) as exc:
        _select("auto", torch, probe=lambda _module: pytest.fail("probe must not run"))
    assert exc.value.code == "MODEL_INFERENCE_FAILED"
    assert exc.value.device_selection.reason == "CUDA_PROBE_FAILED"


def test_legacy_cpu_contract_keeps_cpu_semantics_and_rejects_forced_cuda():
    torch = _FakeTorch()
    for requested in ("auto", "cpu"):
        selection = _select(requested, torch, contract=LEGACY_CPU_RUNTIME_CONTRACT,
                            probe=lambda _module: pytest.fail("legacy contract must not probe"))
        assert selection.actual_device == "cpu"
        assert selection.reason == "LEGACY_CPU_CONTRACT"
        assert selection.fallback is (requested == "auto")
    with pytest.raises(ModelBackendError) as exc:
        _select("cuda", torch, contract=LEGACY_CPU_RUNTIME_CONTRACT)
    assert exc.value.code == "MODEL_UNAVAILABLE"
    assert exc.value.device_selection.reason == "LEGACY_CPU_CONTRACT"


@pytest.mark.parametrize("value", ["", "gpu", "cuda:0", "CPU ", None, 1])
def test_requested_device_values_are_strict(value):
    with pytest.raises(ModelBackendError) as exc:
        normalize_requested_device(value)
    assert exc.value.code == "MODEL_INPUT_INVALID"


def test_auto_exact_unknown_error_literal_fails_closed_never_cpu():
    """Repair-1: the exact real Torch literal must not be an init marker."""
    torch = _FakeTorch()

    def probe(_module):
        raise RuntimeError("CUDA error: unknown error")

    with pytest.raises(ModelBackendError) as exc:
        _select("auto", torch, probe=probe)
    assert exc.value.code == "MODEL_INFERENCE_FAILED"
    assert exc.value.device_selection.reason == "CUDA_PROBE_FAILED"
    assert exc.value.device_selection.actual_device == ""
    assert exc.value.device_selection.fallback is False


def test_availability_query_exact_unknown_error_literal_fails_closed():
    torch = _FakeTorch(available=RuntimeError("CUDA error: unknown error"))
    with pytest.raises(ModelBackendError) as exc:
        _select("auto", torch, probe=lambda _module: pytest.fail("probe must not run"))
    assert exc.value.code == "MODEL_INFERENCE_FAILED"
    assert exc.value.device_selection.reason == "CUDA_PROBE_FAILED"


def test_device_count_query_unknown_error_fails_closed():
    torch = _FakeTorch()
    torch.cuda.device_count = lambda: (_ for _ in ()).throw(
        RuntimeError("CUDA error: unknown error"))
    with pytest.raises(ModelBackendError) as exc:
        _select("auto", torch, probe=lambda _module: pytest.fail("probe must not run"))
    assert exc.value.code == "MODEL_INFERENCE_FAILED"
    assert exc.value.device_selection.reason == "CUDA_PROBE_FAILED"


@pytest.mark.parametrize("message", [
    "CUDA error: unknown error",
    "cuda error: unknown error",
    "CUDA error: an illegal memory access was encountered",
    "CUDA error: unspecified launch failure",
    "unexpected backend fault",
])
def test_auto_unknown_or_unclassified_execution_errors_fail_closed(message):
    torch = _FakeTorch()

    def probe(_module):
        raise RuntimeError(message)

    with pytest.raises(ModelBackendError) as exc:
        _select("auto", torch, probe=probe)
    assert exc.value.code == "MODEL_INFERENCE_FAILED"
    assert exc.value.device_selection.reason == "CUDA_PROBE_FAILED"


def test_unknown_error_text_takes_priority_over_initialization_text():
    """Unknown execution errors must win even when the text also mentions init."""
    torch = _FakeTorch()

    def probe(_module):
        raise RuntimeError("CUDA initialization failed: CUDA error: unknown error")

    with pytest.raises(ModelBackendError) as exc:
        _select("auto", torch, probe=probe)
    assert exc.value.code == "MODEL_INFERENCE_FAILED"
    assert exc.value.device_selection.reason == "CUDA_PROBE_FAILED"


def test_classified_initialization_unavailable_still_falls_back_for_auto():
    torch = _FakeTorch()

    def probe(_module):
        raise RuntimeError("CUDA driver version is insufficient for CUDA runtime version")

    selection = _select("auto", torch, probe=probe)
    assert selection.actual_device == "cpu"
    assert selection.reason == "CUDA_INIT_UNAVAILABLE"
    assert selection.fallback is True


def test_cuda_request_unknown_error_fails_closed_without_cpu_fallback():
    torch = _FakeTorch()

    def probe(_module):
        raise RuntimeError("CUDA error: unknown error")

    with pytest.raises(ModelBackendError) as exc:
        _select("cuda", torch, probe=probe)
    assert exc.value.code == "MODEL_INFERENCE_FAILED"
    assert exc.value.device_selection.reason == "CUDA_PROBE_FAILED"
    assert exc.value.device_selection.actual_device == ""
    assert exc.value.device_selection.fallback is False


def test_device_error_never_reports_a_cpu_selection():
    torch = _FakeTorch()
    for message in ("CUDA error: unknown error", "CUDA out of memory",
                    "CUDA error: device-side assert triggered",
                    "CUDA error: an illegal memory access was encountered"):
        with pytest.raises(ModelBackendError) as exc:
            _select("auto", torch, probe=lambda _module, m=message: (_ for _ in ()).throw(
                RuntimeError(m)))
        assert exc.value.device_selection.actual_device == ""
        assert exc.value.device_selection.fallback is False


def test_cuda_execution_probe_synchronizes_and_detects_invalid_result():
    class _Tensor:
        def __init__(self, value):
            self.value = value
        def __matmul__(self, other):
            return _Tensor(8.0 if self.value == 1.0 and other.value == 1.0 else 0.0)
        def __getitem__(self, item):
            return self
        def item(self):
            return self.value

    class _ProbeCuda(_Cuda):
        def __init__(self):
            super().__init__()
            self.synchronized = 0
        def synchronize(self, device=None):
            self.synchronized += 1

    class _ProbeTorch:
        def __init__(self, value=1.0):
            self.version = _Version("12.6")
            self.cuda = _ProbeCuda()
            self._value = value
            self.float32 = "float32"
        def device(self, name):
            return name
        def ones(self, shape, dtype=None, device=None):
            return _Tensor(self._value)

    good = _ProbeTorch(1.0)
    cuda_execution_probe(good)
    assert good.cuda.synchronized == 1
    with pytest.raises(ModelBackendError) as exc:
        cuda_execution_probe(_ProbeTorch(2.0))
    assert exc.value.device_selection.reason == "CUDA_PROBE_FAILED"
