"""Phase 5 parity, re-based on the frozen materialized runtime (Phase 2 evidence).

The worktree is never used as a runtime root: the default source is the
materialized frozen runtime under the Phase 2 evidence directory, overridable
with GUANCE_MARVEL_V2_SOURCE. All numeric parity assertions are unchanged.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from guance.infrastructure.model.marvel_v2.deployment import (
    collect_deployment_identity, require_platform_distinct_from_training,
    source_manifest_identity,
)
from guance.infrastructure.model.marvel_v2.parity import (
    CACHE_ATOL, CACHE_RTOL, LOGIT_ATOL, LOGIT_RTOL,
    compare_probabilities, compare_numeric,
)
from guance.infrastructure.model.marvel_v2.runtime import MarvelV2DemoRuntime
from guance.infrastructure.model.marvel_v2 import decode_canonical_tokens
from guance.infrastructure.model.marvel_v2.identity import checkpoint_manifest
from guance.infrastructure.model.manifest import ModelBundle

from test_guandan_marvel_v2_runtime import EXPECTATION, synthetic_rollout

DEFAULT_RUNTIME = (r"D:\project\guance\build\acceptance\guance-model-platform-20261016"
                   r"\phase2\runtime\marvel-e5c82e7c-d7acbc8d")
RUNTIME_ROOT = Path(os.environ.get("GUANCE_MARVEL_V2_SOURCE", str(Path(DEFAULT_RUNTIME) / "source")))
CHECKPOINT = Path(os.environ.get("GUANCE_MARVEL_V2_CHECKPOINT", (
    r"D:\project\guandan\runs\M09_full_training\remote\ippo_pilot_failed_20261011"
    r"\extracted-output\evaluation_checkpoints\iter000100.pt")))
NATIVE = RUNTIME_ROOT / "nanopokerlib" / "_guandan_cpp.cp312-win_amd64.pyd"
SOURCE_FILES = (
    "nanopokerlib/cpp/bindings.cpp",
    "nanopokerlib/cpp/core/types.hpp",
    "nanopokerlib/cpp/guandan/guandan.cpp",
    "nanopokerlib/cpp/guandan/guandan.hpp",
)


@pytest.fixture(scope="module")
def checkpoint_bundle():
    if not CHECKPOINT.is_file():
        pytest.skip("fixed checkpoint unavailable")
    return ModelBundle(checkpoint_manifest(
        CHECKPOINT.name, EXPECTATION.sha256,
        model_version=EXPECTATION.model_version,
        training_version=EXPECTATION.training_version), CHECKPOINT.parent)


@pytest.fixture(scope="module")
def torch_module():
    return pytest.importorskip("torch")


def _require_frozen_runtime():
    if not NATIVE.is_file():
        pytest.skip("materialized frozen runtime is unavailable; run scripts/build_marvel_materials.py")


def test_windows_deployment_identity_is_real_distinct_and_source_manifest_matches():
    _require_frozen_runtime()
    normalized_hash, entries = source_manifest_identity(RUNTIME_ROOT, SOURCE_FILES, normalize_newlines=True)
    raw_hash, raw_entries = source_manifest_identity(RUNTIME_ROOT, SOURCE_FILES, normalize_newlines=False)
    # Normalized content equals the frozen delivery family; the raw bytes equal
    # the extension embedded_source_sha256 (line-ending-exact overlay).
    assert normalized_hash == "2c5a26c8ca8b645f22caa40f2ec25c7bf94555d83e06d4d6fb4bc843cb275b60"
    assert raw_hash == "40058234af8227e9f0a0aa8c555959717e277f4ac16ed5094a918a32d46f6898"
    assert len(entries) == len(SOURCE_FILES) == len(raw_entries)
    import torch
    identity = collect_deployment_identity(
        NATIVE, torch_version=torch.__version__, torch_cuda=torch.version.cuda,
        native_source_sha256=raw_hash, compiler="11.2.0",
    )
    require_platform_distinct_from_training(identity)
    assert identity.pointer_bits == 64
    assert identity.python_cache_tag == "cpython-312"
    assert identity.native_sha256 == "d7acbc8d76c9e24caea1b851b733190c068adb8530486a23d42547986344808d"
    assert identity.native_bytes == 3_685_033


def test_real_fixed_checkpoint_cached_rebuild_matches_uninterrupted_synthetic_forward(
        checkpoint_bundle, torch_module, monkeypatch):
    _require_frozen_runtime()
    monkeypatch.syspath_prepend(str(RUNTIME_ROOT))
    runtime = MarvelV2DemoRuntime()
    try:
        runtime.load(checkpoint_bundle, EXPECTATION)
        history = synthetic_rollout(torch_module, 2)
        combined = synthetic_rollout(torch_module, 3)
        next_fields = {key: value[:, 2:3].clone() if key in {
            "player_ids", "obs_tokens", "obs_channels", "avail_tokens", "avail_weights"
        } else value.clone() for key, value in combined.items()}
        next_fields["num_steps"] = torch_module.tensor([1])
        captured = []
        handle = runtime._model.logit_head.register_forward_hook(
            lambda _module, _inputs, output: captured.append(output.detach().clone())
        )
        try:
            uninterrupted = runtime.forward_synthetic(combined)
            uncached_raw = captured.pop()
            rebuilt, cache_a = runtime.forward_synthetic_cached(history, next_fields)
            rebuilt_raw = captured.pop()
            rebuilt_again, cache_b = runtime.forward_synthetic_cached(history, next_fields)
            captured.pop()
        finally:
            handle.remove()
        illegal = [index for index, value in enumerate(next_fields["avail_weights"][0, 0].tolist())
                   if value == 0]
        numeric = compare_probabilities(uninterrupted.tolist(), rebuilt.tolist(),
                                        illegal_indices=illegal)
        legal = next_fields["avail_weights"][0, 0] > 0
        weights = next_fields["avail_weights"][0, 0, legal]
        uncached_logits = uncached_raw[0, -1, legal, 0] + weights.log()
        rebuilt_logits = rebuilt_raw[0, -1, legal, 0] + weights.log()
        logit_numeric = compare_numeric(
            uncached_logits.tolist(), rebuilt_logits.tolist(),
            rtol=LOGIT_RTOL, atol=LOGIT_ATOL,
        )
        cache_max_abs = max(
            float((cache_a.keys - cache_b.keys).abs().max()),
            float((cache_a.values - cache_b.values).abs().max()),
        )
        assert numeric.within_tolerance
        assert numeric.argmax_equal
        assert logit_numeric.within_tolerance and logit_numeric.argmax_equal
        assert rebuilt_again.argmax().item() == rebuilt.argmax().item()
        assert cache_max_abs <= CACHE_ATOL
        assert cache_a.seq_lens.tolist() == [3, 3, 3, 3]
        assert cache_b.seq_lens.tolist() == [3, 3, 3, 3]
    finally:
        runtime.close()


def test_argmax_row_decodes_to_the_same_complete_pass_action(checkpoint_bundle, torch_module, monkeypatch):
    _require_frozen_runtime()
    monkeypatch.syspath_prepend(str(RUNTIME_ROOT))
    runtime = MarvelV2DemoRuntime()
    try:
        runtime.load(checkpoint_bundle, EXPECTATION)
        combined = synthetic_rollout(torch_module, 3)
        combined["avail_tokens"].zero_()
        combined["avail_weights"].zero_()
        combined["avail_tokens"][:, :, 0] = torch_module.tensor(
            [262, 3, 263, 296, 304, 6, 306, 307]
        )
        combined["avail_weights"][:, :, 0] = 1.0
        history = {key: value[:, :2].clone() if key in {
            "player_ids", "obs_tokens", "obs_channels", "avail_tokens", "avail_weights"
        } else value.clone() for key, value in combined.items()}
        history["num_steps"] = torch_module.tensor([2])
        next_fields = {key: value[:, 2:3].clone() if key in {
            "player_ids", "obs_tokens", "obs_channels", "avail_tokens", "avail_weights"
        } else value.clone() for key, value in combined.items()}
        next_fields["num_steps"] = torch_module.tensor([1])
        uncached = runtime.forward_synthetic(combined)
        rebuilt, _cache = runtime.forward_synthetic_cached(history, next_fields)
        assert uncached.argmax().item() == rebuilt.argmax().item() == 0
        assert next_fields["avail_tokens"][0, 0, 0].tolist() == [262, 3, 263, 296, 304, 6, 306, 307]
        decoded = decode_canonical_tokens((6,), phase="FOLLOW_PLAY", level=1, own_hand=())
        assert decoded.kind == "pass" and decoded.tokens == (6,) and decoded.physical_cards == ()
    finally:
        runtime.close()
