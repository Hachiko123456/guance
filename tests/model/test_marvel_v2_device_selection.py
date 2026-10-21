"""Real provider device-selection and runtime-contract compatibility.

Legacy ``marvel_v2.cpu.v1`` keeps CPU-only semantics. The new
``marvel_v2.device.v1`` contract applies the centralized policy. Checkpoint
identity is identical across both contracts and across devices.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from guance.infrastructure.model.errors import ModelBackendError
from guance.infrastructure.model.factory import ModelFactory
from guance.infrastructure.model.manifest import load_bundle
from guance.infrastructure.model.providers.guandan_marvel_v2 import (
    GuandanMarvelV2Provider, MarvelV2ExperimentalRuntime,
)

ROOT = Path(__file__).resolve().parents[2]
PHASE2 = ROOT / "build" / "acceptance" / "guance-model-platform-20261016" / "phase2"
RUNTIME_SOURCE = PHASE2 / "runtime" / "marvel-e5c82e7c-d7acbc8d" / "source"
LEGACY_BUNDLE = PHASE2 / "bundles" / "marvel-v2-iter002000" / "manifest.json"
CHECKPOINT = Path(r"D:\project\guandan\runs\comparison_delivery\dataset\m09_candidate_iter002000.pt")
DEVICE_ROOT = ROOT / "build" / "acceptance" / "guance-gpu-first-inference-20261020"
DEVICE_BUNDLE_CPU_ENV = DEVICE_ROOT / "bundles-cpu-env" / "marvel-v2-iter002000-device" / "manifest.json"
DEVICE_BUNDLE_GPU_ENV = DEVICE_ROOT / "bundles" / "marvel-v2-iter002000-device" / "manifest.json"
CHECKPOINT_SHA256 = "af0df1eaeb67c88e467ee78bf6b0494c49d000f5733d655ee81b40351d4ca9c8"


def _require(*paths: Path) -> None:
    for path in paths:
        if not path.exists():
            pytest.skip(f"fixed material unavailable: {path.name}")


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("GUANCE_MARVEL_V2_SOURCE", str(RUNTIME_SOURCE))
    monkeypatch.setenv("GUANCE_MARVEL_V2_CHECKPOINT", str(CHECKPOINT))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    return monkeypatch


def _cuda_build() -> bool:
    try:
        import torch
    except Exception:
        return False
    return bool(torch.version.cuda)


CPU_PINNED = pytest.mark.skipif(
    _cuda_build(), reason="legacy CPU contract pins the accepted CPU-only Torch build")


@CPU_PINNED
def test_legacy_cpu_contract_keeps_cpu_semantics_and_same_checkpoint(env):
    _require(LEGACY_BUNDLE, RUNTIME_SOURCE)
    advisor = ModelFactory(requested_device="cpu").create(LEGACY_BUNDLE)
    try:
        metadata = advisor.load().to_dict()
        assert metadata["runtime_contract"] == "marvel_v2.cpu.v1"
        assert metadata["requested_device"] == "cpu"
        assert metadata["actual_device"] == "cpu"
        assert metadata["device_reason"] == "LEGACY_CPU_CONTRACT"
        assert metadata["device_fallback"] is False
        assert metadata["model_sha256"] == CHECKPOINT_SHA256
        assert metadata["runtime"] == "torch-cpu"
    finally:
        advisor.close()


@CPU_PINNED
def test_legacy_cpu_contract_rejects_forced_cuda(env):
    _require(LEGACY_BUNDLE, RUNTIME_SOURCE)
    advisor = ModelFactory(requested_device="cuda").create(LEGACY_BUNDLE)
    try:
        with pytest.raises(ModelBackendError) as exc:
            advisor.load()
        assert exc.value.code == "MODEL_UNAVAILABLE"
        assert exc.value.device_selection.reason == "LEGACY_CPU_CONTRACT"
    finally:
        advisor.close()


def test_device_contract_auto_falls_back_on_cpu_only_torch_with_reason(env):
    _require(DEVICE_BUNDLE_CPU_ENV, RUNTIME_SOURCE)
    try:
        import torch
        if torch.version.cuda:
            pytest.skip("this test requires the CPU-only Torch environment")
    except Exception:
        pytest.skip("torch unavailable")
    advisor = ModelFactory(requested_device="auto").create(DEVICE_BUNDLE_CPU_ENV)
    try:
        metadata = advisor.load().to_dict()
        assert metadata["runtime_contract"] == "marvel_v2.device.v1"
        assert metadata["actual_device"] == "cpu"
        assert metadata["device_fallback"] is True
        assert metadata["device_reason"] == "CPU_ONLY_TORCH"
        assert metadata["torch_cuda_build"] == "cpu"
        assert metadata["model_sha256"] == CHECKPOINT_SHA256
    finally:
        advisor.close()


def test_device_contract_forced_cuda_never_falls_back_on_cpu_only_torch(env):
    _require(DEVICE_BUNDLE_CPU_ENV, RUNTIME_SOURCE)
    try:
        import torch
        if torch.version.cuda:
            pytest.skip("this test requires the CPU-only Torch environment")
    except Exception:
        pytest.skip("torch unavailable")
    advisor = ModelFactory(requested_device="cuda").create(DEVICE_BUNDLE_CPU_ENV)
    try:
        with pytest.raises(ModelBackendError) as exc:
            advisor.load()
        assert exc.value.code == "MODEL_UNAVAILABLE"
        assert exc.value.device_selection.reason == "CPU_ONLY_TORCH"
        assert exc.value.device_selection.actual_device == ""
    finally:
        advisor.close()


def test_unknown_runtime_contract_is_rejected():
    _require(LEGACY_BUNDLE)
    manifest = load_bundle(LEGACY_BUNDLE).manifest
    with pytest.raises(ModelBackendError) as exc:
        GuandanMarvelV2Provider().validate(replace(manifest, runtime_contract="marvel_v2.other.v1"))
    assert exc.value.code == "RUNTIME_UNSUPPORTED"


@pytest.mark.parametrize("value", ["gpu", "cuda:0", "", None])
def test_requested_device_is_strict(value):
    with pytest.raises(ModelBackendError) as exc:
        MarvelV2ExperimentalRuntime(requested_device=value)
    assert exc.value.code == "MODEL_INPUT_INVALID"
