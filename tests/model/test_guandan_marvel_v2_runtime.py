"""GMV2-P0-003/010/011, P1-001, P2-004: isolated demo, never live advice."""
from dataclasses import asdict, replace
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback
from types import SimpleNamespace
import weakref

import pytest

from guance.contracts import ModelMetadata
from guance.infrastructure.model.errors import ModelBackendError
from guance.infrastructure.model.factory import ModelFactory
from guance.infrastructure.model.marvel_v2 import runtime as runtime_module
from guance.infrastructure.model.marvel_v2.identity import CheckpointIdentity, checkpoint_manifest
from guance.infrastructure.model.marvel_v2.checkpoint import canonical_json_hash
from guance.infrastructure.model.marvel_v2.runtime import MarvelV2DemoRuntime
from guance.infrastructure.model.providers.guandan_marvel_v2 import GuandanMarvelV2Provider
from guance.infrastructure.model.registry import default_registry

# Shared optional fixtures live only in the two explicitly authorized test files.
from test_guandan_marvel_v2_checkpoint import (
    checkpoint_bundle, checkpoint_expectation, checkpoint_payload, pinned_expectation, torch_module,
)

# Bundle-declared expectation for the locally pinned evaluation checkpoint
# (test fixture data; production constants no longer exist).
EXPECTATION = pinned_expectation()


@pytest.fixture
def marvel_modules(monkeypatch, torch_module):
    # Local opt-in test harness only. Never installed into a production sys.path.
    source = Path(os.environ.get(
        "GUANCE_MARVEL_V2_SOURCE",
        r"D:\project\guance\build\acceptance\guance-model-platform-20261016"
        r"\phase2\runtime\marvel-e5c82e7c-d7acbc8d\source"))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    if source.is_dir():
        monkeypatch.syspath_prepend(str(source))
    config = pytest.importorskip("guandan_marvel.config", reason="optional MARVEL source unavailable")
    model = pytest.importorskip("guandan_marvel.model", reason="optional MARVEL source unavailable")
    return config, model


def synthetic_rollout(torch, steps=2):
    # Tensor-shape exercise, NOT an encoded game or missing live state filled in
    # with zeros. No deals, hands, capture, environment, native backend, or KV.
    tokens = torch.zeros((1, steps, 4, 48), dtype=torch.long)
    channels = torch.zeros((1, steps, 4, 512), dtype=torch.uint8)
    for seat in range(4):
        tokens[:, :, seat, :6] = torch.tensor([1, 256, 2, 18, 38 + seat, 308])
        channels[:, :, seat, seat] = 1
    rows = torch.zeros((1, steps, 256, 8), dtype=torch.long)
    for index, token in enumerate((8, 9, 10, 11)):
        rows[:, :, index] = torch.tensor([262, 2, 263, 298, 304, token, 305, 307])
    weights = torch.zeros((1, steps, 256), dtype=torch.float32)
    weights[:, :, 0] = 1
    weights[:, :, 1] = 2
    weights[:, :, 3] = 1  # Row 2 is nonpadding but illegal; row 4+ is padding.
    return {
        "num_steps": torch.tensor([steps]),
        "player_ids": torch.arange(steps).remainder(4).reshape(1, steps),
        "obs_tokens": tokens, "obs_channels": channels,
        "avail_tokens": rows, "avail_weights": weights,
    }


@pytest.fixture
def loaded_runtime(checkpoint_bundle, marvel_modules):
    runtime = MarvelV2DemoRuntime()
    runtime.load(checkpoint_bundle, EXPECTATION)
    yield runtime
    runtime.close()


def assert_cleared(runtime):
    assert runtime.metadata is None
    assert runtime.identity is None
    assert runtime.model_metadata is None
    assert runtime._model is None
    assert runtime._bundle is None
    assert runtime._torch is None
    assert runtime._tensor_dict is None


def test_strict_reconstruction_eval_inference_and_masked_cpu_forward(
        checkpoint_bundle, checkpoint_payload, marvel_modules, torch_module, monkeypatch):
    config_module, model_module = marvel_modules
    original_agent = model_module.Agent
    original_load = original_agent.load_state_dict
    calls = []
    def agent(cfg, *, enable_value_head):
        values = cfg.to_dict()
        assert cfg.device == "cpu"
        values["device"] = checkpoint_payload.data["resolved_config"]["device"]
        assert canonical_json_hash(values) == EXPECTATION.resolved_config_hash
        assert enable_value_head is True
        return original_agent(cfg, enable_value_head=enable_value_head)
    def load(model, state, *, strict):
        calls.append((len(state), strict))
        return original_load(model, state, strict=strict)
    monkeypatch.setattr(model_module, "Agent", agent)
    monkeypatch.setattr(original_agent, "load_state_dict", load)
    runtime = MarvelV2DemoRuntime()
    try:
        identity = runtime.load(checkpoint_bundle, EXPECTATION)
        assert calls == [(78, True)]
        assert not runtime._model.training
        assert runtime._model.value_head is not None
        observed = []
        def check_inference(module, args, output):
            observed.append(torch_module.is_inference_mode_enabled())
            assert output.past_key_values is None
            assert bool(torch_module.isfinite(output.player_state_value).all())
        runtime._model.register_forward_hook(check_inference)
        fields = synthetic_rollout(torch_module)
        result = runtime.forward_synthetic(fields, timeout_seconds=30)
        assert observed == [True]
        assert tuple(result.shape) == (256,)
        assert result.device.type == "cpu" and not result.requires_grad
        assert torch_module.is_inference(result)
        assert bool(torch_module.isfinite(result).all())
        assert bool((result[fields["avail_weights"][0, -1] == 0] == 0).all())
        assert result.sum().item() == pytest.approx(1.0, abs=1e-6)
        assert identity.status == "demo_loaded"
        metadata = asdict(identity)
        assert metadata["experimental"] and metadata["test_only"]
        assert not metadata["production_ready"] and not metadata["strength_claim"]
        assert not metadata["deployment_identity_verified"] and not metadata["public_self_supported"]
        assert metadata["formal_evaluation_status"] == "not_run"
        assert metadata["champion_status"] == "not_established"
        assert "nanopokerlib" not in sys.modules
    finally:
        runtime.close()


def test_load_idempotent_reload_drops_old_model_and_close_terminal(loaded_runtime, checkpoint_bundle, torch_module):
    runtime = loaded_runtime
    old = runtime._model
    original_identity = runtime.metadata
    assert runtime.load(checkpoint_bundle, EXPECTATION) is original_identity
    assert runtime._model is old
    old_reference = weakref.ref(old)
    runtime.reload(checkpoint_bundle, EXPECTATION)
    assert runtime._model is not old
    del old
    assert old_reference() is None
    live_reference = weakref.ref(runtime._model)
    runtime.close()
    runtime.close()
    assert_cleared(runtime)
    assert live_reference() is None
    for action in (lambda: runtime.load(checkpoint_bundle, EXPECTATION), lambda: runtime.reload(checkpoint_bundle, EXPECTATION),
                   lambda: runtime.forward_synthetic(synthetic_rollout(torch_module))):
        with pytest.raises(ModelBackendError) as exc:
            action()
        assert exc.value.code == "MODEL_CLOSED"


def test_failed_reload_cannot_keep_old_model(loaded_runtime, checkpoint_bundle, torch_module):
    wrong = replace(checkpoint_bundle, manifest=replace(checkpoint_bundle.manifest, architecture="v1.2"))
    with pytest.raises(ModelBackendError) as exc:
        loaded_runtime.reload(wrong, EXPECTATION)
    assert exc.value.code == "MODEL_VERSION_UNSUPPORTED"
    assert_cleared(loaded_runtime)
    with pytest.raises(ModelBackendError) as exc:
        loaded_runtime.forward_synthetic(synthetic_rollout(torch_module))
    assert exc.value.code == "MODEL_NOT_LOADED"
    assert loaded_runtime.load(checkpoint_bundle, EXPECTATION).status == "demo_loaded"


@pytest.mark.parametrize("dependency", ["torch", "guandan_marvel.config", "guandan_marvel.model"])
def test_optional_dependency_failure_unavailable_no_fallback(checkpoint_bundle, monkeypatch, dependency):
    original = importlib.import_module
    def unavailable(name, *args, **kwargs):
        if name == dependency:
            raise ImportError("private dependency detail")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(importlib, "import_module", unavailable)
    runtime = MarvelV2DemoRuntime()
    with pytest.raises(ModelBackendError) as exc:
        runtime.load(checkpoint_bundle, EXPECTATION)
    assert exc.value.code == "MODEL_UNAVAILABLE"
    assert "private" not in str(exc.value)
    assert_cleared(runtime)


@pytest.mark.parametrize("stage", ["config_none", "config_changed", "construct", "strict", "eval"])
def test_failed_reconstruction_cleans_up(checkpoint_bundle, marvel_modules, monkeypatch, stage):
    config_module, model_module = marvel_modules
    def fail(*args, **kwargs):
        raise RuntimeError("private model details")
    if stage == "config_none":
        monkeypatch.setattr(config_module, "deserialize_train_config", lambda _: None)
    elif stage == "config_changed":
        original = config_module.deserialize_train_config
        def changed(payload):
            cfg = original(payload)
            cfg.seed += 1
            return cfg
        monkeypatch.setattr(config_module, "deserialize_train_config", changed)
    elif stage == "construct":
        monkeypatch.setattr(model_module, "Agent", fail)
    else:
        monkeypatch.setattr(model_module.Agent, "load_state_dict" if stage == "strict" else "eval", fail)
    runtime = MarvelV2DemoRuntime()
    with pytest.raises(ModelBackendError) as exc:
        runtime.load(checkpoint_bundle, EXPECTATION)
    assert exc.value.code == "MODEL_LOAD_FAILED"
    assert "private" not in str(exc.value)
    assert_cleared(runtime)


@pytest.mark.parametrize("kind", ["unknown", "wrong_shape", "wrong_dtype", "token_range", "negative_weight",
                                   "nan_weight", "no_legal_rows", "no_actor", "padded_final_step"])
def test_invalid_synthetic_input_fails_closed(loaded_runtime, torch_module, kind):
    fields = synthetic_rollout(torch_module)
    if kind == "unknown":
        fields["unknown"] = torch_module.zeros(1)
    elif kind == "wrong_shape":
        fields["obs_tokens"] = fields["obs_tokens"][..., :47]
    elif kind == "wrong_dtype":
        fields["player_ids"] = fields["player_ids"].float()
    elif kind == "token_range":
        fields["avail_tokens"][0, 0, 0, 0] = 320
    elif kind in ("negative_weight", "nan_weight"):
        fields["avail_weights"][0, 0, 0] = -1 if kind == "negative_weight" else float("nan")
    elif kind == "no_legal_rows":
        fields["avail_weights"].zero_()
    elif kind == "no_actor":
        fields["player_ids"].fill_(-1)
    else:
        fields["num_steps"].zero_()
    with pytest.raises(ModelBackendError) as exc:
        loaded_runtime.forward_synthetic(fields)
    assert exc.value.code == "MODEL_INPUT_INVALID"
    assert_cleared(loaded_runtime)


@pytest.mark.parametrize("kind", ["error", "nan", "illegal_probability", "sum", "shape"])
def test_forward_errors_and_invalid_output_clear_model(loaded_runtime, torch_module, monkeypatch, kind):
    def forward(rollout, **kwargs):
        assert kwargs == {"use_cache": False, "past_key_values": None, "cache_only": False}
        if kind == "error":
            raise RuntimeError("private forward detail")
        probabilities = torch_module.zeros((1, 2, 256))
        probabilities[..., 0] = 1
        if kind == "nan":
            probabilities[..., 0] = float("nan")
        elif kind == "illegal_probability":
            probabilities[..., 0] = 0
            probabilities[..., 2] = 1
        elif kind == "sum":
            probabilities[..., 0] = 0.5
        else:
            probabilities = probabilities[..., :255]
        return SimpleNamespace(avail_probs=probabilities)
    monkeypatch.setattr(loaded_runtime._model, "forward", forward)
    with pytest.raises(ModelBackendError) as exc:
        loaded_runtime.forward_synthetic(synthetic_rollout(torch_module))
    assert exc.value.code == ("MODEL_INFERENCE_FAILED" if kind == "error" else "MODEL_OUTPUT_INVALID")
    assert "private" not in str(exc.value)
    assert_cleared(loaded_runtime)


def test_late_forward_and_load_results_discarded(loaded_runtime, checkpoint_bundle, torch_module, monkeypatch):
    ticks = iter([0, 0, 2])
    monkeypatch.setattr(runtime_module, "perf_counter", lambda: next(ticks))
    with pytest.raises(ModelBackendError) as exc:
        loaded_runtime.forward_synthetic(synthetic_rollout(torch_module), timeout_seconds=1)
    assert exc.value.code == "MODEL_TIMEOUT"
    assert_cleared(loaded_runtime)
    ticks = iter([0, 2])
    with pytest.raises(ModelBackendError) as exc:
        loaded_runtime.load(checkpoint_bundle, EXPECTATION, timeout_seconds=1)
    assert exc.value.code == "MODEL_TIMEOUT"
    assert_cleared(loaded_runtime)


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), 601])
def test_invalid_timeout_rejected_without_loading(checkpoint_bundle, timeout):
    runtime = MarvelV2DemoRuntime()
    with pytest.raises(ModelBackendError) as exc:
        runtime.load(checkpoint_bundle, EXPECTATION, timeout_seconds=timeout)
    assert exc.value.code == "MODEL_INPUT_INVALID"
    assert_cleared(runtime)


def test_device_selection_accepts_cpu_and_cuda_and_rejects_invalid_names():
    assert MarvelV2DemoRuntime().device_name == "cpu"
    assert MarvelV2DemoRuntime(device="cpu").device_name == "cpu"
    assert MarvelV2DemoRuntime(device="cuda").device_name == "cuda:0"
    assert MarvelV2DemoRuntime(device="cuda:0").device_name == "cuda:0"
    for value in ("gpu", "cuda:-1", "cuda:", "", None, 0):
        with pytest.raises(ModelBackendError) as exc:
            MarvelV2DemoRuntime(device=value)
        assert exc.value.code == "RUNTIME_UNSUPPORTED"
    with pytest.raises(TypeError):
        MarvelV2DemoRuntime(seed=1)


def test_default_provider_never_instantiates_demo_or_adapter(checkpoint_bundle, monkeypatch, view):
    def forbidden(*args, **kwargs):
        pytest.fail("production provider attempted to construct the demo")
    monkeypatch.setattr(MarvelV2DemoRuntime, "__init__", forbidden)
    registry = default_registry()
    provider = registry.resolve(checkpoint_bundle.manifest)
    assert isinstance(provider, GuandanMarvelV2Provider)
    assert registry._providers["guandan"] is registry._providers["builtin.guandan"]
    assert provider is not registry._providers["guandan"]
    advisor = ModelFactory().create(checkpoint_bundle)
    for action in (advisor.load, advisor.load, lambda: advisor.recommend(view, "synthetic"),
                   lambda: provider.create_runtime(checkpoint_bundle)):
        with pytest.raises(ModelBackendError) as exc:
            action()
        assert exc.value.code == "MODEL_UNAVAILABLE"
    adapter = provider.create_observation_adapter()
    with pytest.raises(ModelBackendError) as exc:
        adapter.adapt(view)
    assert exc.value.code == "GUANDAN_STATE_UNAVAILABLE"
    advisor.close()
    advisor.close()
    with pytest.raises(ModelBackendError) as exc:
        advisor.load()
    assert exc.value.code == "MODEL_CLOSED"


@pytest.mark.parametrize("capability", ["public_plus_self", "search", "beam_search", "belief"])
def test_descriptor_cannot_advertise_unimplemented_capabilities(capability):
    manifest = checkpoint_manifest("model.pt", "a" * 64,
                                   model_version="MARVEL-V2-v1.3-iter000100",
                                   training_version="M09-IPPO-pilot-20261011")
    manifest = replace(manifest, capabilities={**manifest.capabilities, capability: True})
    with pytest.raises(ModelBackendError) as exc:
        default_registry().resolve(manifest)
    assert exc.value.code == "CONTRACT_UNSUPPORTED"


def test_confirmed_decision_capability_requires_complete_action():
    manifest = checkpoint_manifest("model.pt", "a" * 64,
                                   model_version="MARVEL-V2-v1.3-iter002000",
                                   training_version="M09-comparison-delivery-20261016")
    with_confirm = replace(manifest, capabilities={
        **manifest.capabilities, "complete_action": True, "confirmed_decision": True})
    default_registry().resolve(with_confirm)
    without_complete = replace(manifest, capabilities={
        **manifest.capabilities, "confirmed_decision": True})
    with pytest.raises(ModelBackendError) as exc:
        default_registry().resolve(without_complete)
    assert exc.value.code == "CONTRACT_UNSUPPORTED"


def test_fresh_fabledan_registry_and_inference_never_import_optional_dependencies(npz_path, view):
    script = """
import importlib.abc, json, sys
blocked = ('torch', 'guandan_marvel', 'nanopokerlib')
attempts = []
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in blocked:
            attempts.append(fullname)
            raise ModuleNotFoundError('optional dependency intentionally absent')
sys.meta_path.insert(0, Block())
from guance.infrastructure.model.registry import default_registry
from guance.infrastructure.model.factory import ModelFactory
from guance.contracts import CanonicalPublicGameView
default_registry()
assert 'guance.infrastructure.model.marvel_v2.runtime' not in sys.modules
from guance.infrastructure.model.marvel_v2.runtime import MarvelV2DemoRuntime
advisor = ModelFactory().create(sys.argv[1])
assert advisor.load().provider == 'fabledan'
view = CanonicalPublicGameView.from_dict(json.loads(sys.argv[2]))
assert advisor.recommend(view, 'synthetic-no-torch').status == 'ready'
advisor.close()
assert not attempts
assert not any(name.split('.')[0] in blocked for name in sys.modules)
"""
    result = subprocess.run([sys.executable, "-B", "-c", script, str(npz_path), json.dumps(view.to_dict())],
                            capture_output=True, text=True, timeout=30,
                            env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src"),
                                 "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stderr


def test_repair_repeated_load_permission_error_is_bounded_and_cleans_up(
        loaded_runtime, checkpoint_bundle, monkeypatch):
    sentinel = "GMV2_REPEATED_LOAD_PERMISSION_ERROR_SENTINEL"
    def denied(bundle, expectation):
        raise PermissionError(sentinel)
    with monkeypatch.context() as patch:
        patch.setattr(runtime_module, "verified_artifact_bytes", denied)
        with pytest.raises(ModelBackendError) as exc:
            loaded_runtime.load(checkpoint_bundle, EXPECTATION)
        assert exc.value.code == "MODEL_LOAD_FAILED"
        assert sentinel not in str(exc.value)
        assert sentinel not in "".join(traceback.format_exception(exc.value))
        assert exc.value.__suppress_context__ is True
        assert_cleared(loaded_runtime)
    assert loaded_runtime.load(checkpoint_bundle, EXPECTATION).status == "demo_loaded"


@pytest.mark.parametrize("code", ["ARTIFACT_HASH_MISMATCH", "ARTIFACT_MISSING"])
def test_repair_repeated_load_preserves_stable_errors(loaded_runtime, checkpoint_bundle, monkeypatch, code):
    expected = ModelBackendError(code)
    def rejected(bundle, expectation):
        raise expected
    monkeypatch.setattr(runtime_module, "verified_artifact_bytes", rejected)
    with pytest.raises(ModelBackendError) as exc:
        loaded_runtime.load(checkpoint_bundle, EXPECTATION)
    assert exc.value is expected
    assert_cleared(loaded_runtime)


@pytest.mark.parametrize("preloaded", [False, True], ids=["initial-load", "repeated-load"])
@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), 601, 10**400, -(10**400)],
                         ids=["zero", "negative", "bool", "nan", "inf", "over-limit",
                              "huge-positive-int", "huge-negative-int"])
def test_repair_invalid_timeout_has_stable_code_on_both_load_paths(
        checkpoint_bundle, marvel_modules, monkeypatch, preloaded, timeout):
    runtime = MarvelV2DemoRuntime()
    try:
        if preloaded:
            runtime.load(checkpoint_bundle, EXPECTATION)
        def forbidden(*args, **kwargs):
            pytest.fail("invalid timeout reached artifact loading")
        monkeypatch.setattr(runtime_module, "load_checkpoint", forbidden)
        monkeypatch.setattr(runtime_module, "verified_artifact_bytes", forbidden)
        with pytest.raises(ModelBackendError) as exc:
            runtime.load(checkpoint_bundle, EXPECTATION, timeout_seconds=timeout)
        assert exc.value.code == "MODEL_INPUT_INVALID"
        assert_cleared(runtime)
    finally:
        runtime.close()


@pytest.mark.parametrize("identity_status,projected_status", [
    ("validated", "experimental_artifact_validated"),
    ("demo_loaded", "experimental_demo_loaded"),
])
def test_repair_checkpoint_identity_projects_to_existing_model_metadata(identity_status, projected_status):
    identity = replace(CheckpointIdentity(checkpoint_sha256=EXPECTATION.sha256,
                                          model_version=EXPECTATION.model_version,
                                          resolved_config_hash=EXPECTATION.resolved_config_hash,
                                          source_hash=EXPECTATION.source_hash),
                       status=identity_status)
    metadata = identity.to_model_metadata()
    assert isinstance(metadata, ModelMetadata)
    assert metadata.status == projected_status
    assert metadata.provider == "guandan_marvel_v2"
    assert metadata.backend == "guandan" and metadata.runtime == "torch-cpu"
    assert metadata.model_version == EXPECTATION.model_version
    assert metadata.model_sha256 == EXPECTATION.sha256
    assert metadata.test_only is True and metadata.legacy is False
    assert metadata.score_kind == "marvel_v2_row_probability"
    assert metadata.input_contract == "GuandanMARVELV2.Rollout.v1"
    assert metadata.output_contract == "GuandanMARVELV2.ActionRow.v2"
    # No canonical/public wire adapter is implemented; empty is deliberate.
    assert metadata.wire_input_contract == metadata.wire_output_contract == ""
    assert metadata.capabilities.to_dict() == {
        "recommend": False, "top_n": False, "complete_action": False,
        "next_token_policy": True, "confirmed_decision": False,
        "beam_search": False, "belief": False,
        "search": False, "public_plus_self": False,
    }
    assert not metadata.extras and not metadata.capabilities.extras
    assert not identity.production_ready and not identity.strength_claim
    assert identity.experimental and not identity.public_self_supported
    serialized = metadata.to_dict()
    assert serialized.keys() == {
        "status", "provider", "backend", "runtime", "model_version", "model_sha256",
        "score_kind", "input_contract", "output_contract", "legacy", "test_only", "capabilities",
    }
    text = json.dumps(serialized)
    assert len(text) < 1500
    assert not any(term in text for term in ("D:", "project/", "project\\", "resolved_config",
                                            "state_dict", "provenance", "deal_hashes", "source_hash"))
    assert ModelMetadata.from_mapping(serialized) == metadata
    with pytest.raises((AttributeError, TypeError)):
        metadata.status = "ready"


def test_repair_runtime_exposes_projection_and_explicit_private_identity(checkpoint_bundle, marvel_modules):
    runtime = MarvelV2DemoRuntime()
    assert runtime.identity is None and runtime.model_metadata is None
    try:
        identity = runtime.load(checkpoint_bundle, EXPECTATION)
        assert runtime.identity is identity
        assert runtime.metadata is identity  # Preserve the existing private-metadata API.
        assert isinstance(runtime.model_metadata, ModelMetadata)
        assert runtime.model_metadata == identity.to_model_metadata()
        assert runtime.load(checkpoint_bundle, EXPECTATION) is identity
        assert not runtime.identity.production_ready and not runtime.identity.strength_claim
    finally:
        runtime.close()
    assert runtime.identity is None and runtime.model_metadata is None


def test_confirmed_stepwise_cache_matches_full_forward(
        checkpoint_bundle, marvel_modules, torch_module):
    runtime = MarvelV2DemoRuntime()
    try:
        runtime.load(checkpoint_bundle, EXPECTATION)
        combined = synthetic_rollout(torch_module, steps=3)
        steps = []
        for index in range(3):
            item = {
                key: (value[:, index:index + 1].clone() if key in {
                    "player_ids", "obs_tokens", "obs_channels", "avail_tokens", "avail_weights"
                } else torch_module.tensor([1]))
                for key, value in combined.items()
            }
            steps.append(item)
        expected = runtime.forward_synthetic(combined)
        actual, cache = runtime.forward_confirmed_stepwise(steps)
        assert torch_module.allclose(actual, expected, rtol=1e-5, atol=1e-6)
        assert cache.seq_lens.tolist() == [3, 3, 3, 3]
    finally:
        runtime.close()


def test_cpu_device_evidence_reports_parameters_and_cache_device(loaded_runtime):
    evidence = loaded_runtime.device_evidence()
    assert evidence["selected_device"] == "cpu"
    assert evidence["parameter_count"] > 0 and evidence["parameter_device"] == "cpu"
    assert evidence["buffer_count"] > 0 and evidence["buffer_device"] == "cpu"
    assert evidence["cache_device"] == "cpu"


def _cuda_available() -> bool:
    try:
        import torch
    except Exception:
        return False
    try:
        return bool(torch.cuda.is_available())
    except Exception:
        return False


@pytest.mark.skipif(not _cuda_available(), reason="requires a real CUDA device")
def test_real_cuda_runtime_moves_parameters_inputs_cache_and_forward(
        checkpoint_bundle, torch_module):
    runtime = MarvelV2DemoRuntime(device="cuda:0")
    try:
        runtime.load(checkpoint_bundle, EXPECTATION)
        evidence = runtime.device_evidence()
        assert evidence["selected_device"] == "cuda:0"
        assert evidence["parameter_device"] == "cuda:0"
        assert evidence["buffer_device"] in ("cuda:0", "")
        assert evidence["cache_device"] == "cuda:0"
        fields = {key: value.to("cuda:0")
                  for key, value in synthetic_rollout(torch_module).items()}
        result = runtime.forward_synthetic(fields, timeout_seconds=120)
        assert result.device.type == "cuda"
        assert not result.requires_grad and torch_module.is_inference(result)
        assert bool(torch_module.isfinite(result).all())
        assert bool((result[fields["avail_weights"][0, -1] == 0] == 0).all())
        assert abs(float(result.sum().item()) - 1.0) <= 1e-5
    finally:
        runtime.close()


def test_gpu_execution_failure_fails_closed_without_cpu_retry(
        loaded_runtime, torch_module, monkeypatch):
    runtime = loaded_runtime
    model = runtime._model

    def fail(*_args, **_kwargs):
        raise RuntimeError("CUDA out of memory. Tried to allocate 64.00 MiB")

    monkeypatch.setattr(type(model), "__call__", fail)
    fields = synthetic_rollout(torch_module)
    with pytest.raises(ModelBackendError) as exc:
        runtime.forward_synthetic(fields)
    assert exc.value.code == "MODEL_INFERENCE_FAILED"
    assert runtime._model is None
    with pytest.raises(ModelBackendError) as exc:
        runtime.forward_synthetic(fields)
    assert exc.value.code == "MODEL_NOT_LOADED"
