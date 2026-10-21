"""Explicit, single-owner CPU tensor demo. NOT a production public+self runtime.

No environment, native backend, history/KV cache, action decoding, or game input
adapter is supplied here. Synthetic tensors must come from a test harness. A
successful forward verifies tensor mechanics only, not encoding/platform parity.
Timeouts discard late results and clear this instance; synchronous native calls
cannot be preempted. Hard wall-clock limits belong to an external test process.
"""
from __future__ import annotations

from dataclasses import replace
import importlib
import math
import re
from time import perf_counter
from typing import Any

from guance.contracts import ModelMetadata
from ..errors import ModelBackendError
from ..manifest import ModelBundle
from .checkpoint import (
    CheckpointExpectation, canonical_json_hash, load_checkpoint, verified_artifact_bytes,
)
from .identity import CheckpointIdentity

_ROLLOUT_KEYS = frozenset({
    "num_steps", "player_ids", "obs_tokens", "obs_channels", "avail_tokens", "avail_weights",
})
_MAX_DEMO_STEPS = 32
_DEVICE_NAME = re.compile(r"(?:cpu|cuda(?::(?:0|[1-9][0-9]{0,2}))?)\Z")


def _deadline(timeout_seconds: float | None) -> float | None:
    if timeout_seconds is None:
        return None
    # Bound Python integers before math.isfinite can coerce them to float.
    if (type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 600
            or not math.isfinite(timeout_seconds)):
        raise ModelBackendError("MODEL_INPUT_INVALID")
    return perf_counter() + timeout_seconds


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and perf_counter() >= deadline:
        raise ModelBackendError("MODEL_TIMEOUT")


class MarvelV2DemoRuntime:
    """Real weights via a bundle-declared expectation, isolated from every factory.

    load(same bundle) is idempotent with integrity revalidation; reload always
    drops the old model first. Any operation failure drops model references.
    close is idempotent and terminal; use a new instance after closing.
    """

    def __init__(self, *, device: str = "cpu") -> None:
        if not isinstance(device, str) or not _DEVICE_NAME.fullmatch(device):
            raise ModelBackendError("RUNTIME_UNSUPPORTED")
        self._device_name = "cuda:0" if device == "cuda" else device
        self._closed = False
        self._clear()

    @property
    def device_name(self) -> str:
        """The device this runtime was constructed for; stable for its lifecycle."""
        return self._device_name

    @property
    def device(self) -> Any:
        if self._torch is None:
            raise ModelBackendError("MODEL_NOT_LOADED")
        return self._torch.device(self._device_name)

    def _clear(self) -> None:
        self._model: Any = None
        self._torch: Any = None
        self._tensor_dict: Any = None
        self._bundle: ModelBundle | None = None
        self._metadata: CheckpointIdentity | None = None

    @property
    def metadata(self) -> CheckpointIdentity | None:
        """Backward-compatible private identity; use model_metadata for the DTO."""
        return self._metadata

    @property
    def identity(self) -> CheckpointIdentity | None:
        """Detailed immutable checkpoint identity, separate from public metadata."""
        return self._metadata

    @property
    def model_metadata(self) -> ModelMetadata | None:
        """Bounded existing contract, unavailable until this demo has loaded."""
        return self._metadata.to_model_metadata() if self._metadata is not None else None

    def _ensure_open(self) -> None:
        if self._closed:
            raise ModelBackendError("MODEL_CLOSED")

    def load(self, bundle: ModelBundle, expectation: CheckpointExpectation, *,
             timeout_seconds: float | None = None) -> CheckpointIdentity:
        self._ensure_open()
        if self._model is not None and bundle == self._bundle:
            try:
                deadline = _deadline(timeout_seconds)
                verified_artifact_bytes(bundle, expectation)
                _check_deadline(deadline)
                return self._metadata
            except ModelBackendError:
                self._clear()
                raise
            except Exception:
                self._clear()
                raise ModelBackendError("MODEL_LOAD_FAILED") from None
        return self.reload(bundle, expectation, timeout_seconds=timeout_seconds)

    def reload(self, bundle: ModelBundle, expectation: CheckpointExpectation, *,
               timeout_seconds: float | None = None) -> CheckpointIdentity:
        self._ensure_open()
        self._clear()
        try:
            deadline = _deadline(timeout_seconds)
            checkpoint = load_checkpoint(bundle, expectation)
            _check_deadline(deadline)
            try:
                torch = importlib.import_module("torch")
                config_module = importlib.import_module("guandan_marvel.config")
                model_module = importlib.import_module("guandan_marvel.model")
            except Exception:
                raise ModelBackendError("MODEL_UNAVAILABLE") from None
            cfg = config_module.deserialize_train_config(checkpoint.resolved_config)
            if (cfg is None
                    or canonical_json_hash(cfg.to_dict()) != expectation.resolved_config_hash):
                raise ModelBackendError("MODEL_LOAD_FAILED")
            original_device = cfg.device
            # The only resolved-config override permitted: the selected device.
            cfg.device = self._device_name
            model = model_module.Agent(cfg, enable_value_head=True).to(self._device_name)
            result = model.load_state_dict(checkpoint.state_dict, strict=True)
            if result.missing_keys or result.unexpected_keys:
                raise ModelBackendError("MODEL_LOAD_FAILED")
            model.eval()
            self._assert_model_device(torch, model)
            reconstructed = cfg.to_dict()
            reconstructed["device"] = original_device
            if canonical_json_hash(reconstructed) != expectation.resolved_config_hash:
                raise ModelBackendError("MODEL_LOAD_FAILED")
            _check_deadline(deadline)
            self._model, self._torch, self._tensor_dict = model, torch, model_module.TensorDict
            self._bundle = bundle
            self._metadata = replace(checkpoint.identity, status="demo_loaded")
            return self._metadata
        except ModelBackendError:
            self._clear()
            raise
        except Exception:
            self._clear()
            raise ModelBackendError("MODEL_LOAD_FAILED") from None

    def _assert_model_device(self, torch: Any, model: Any) -> None:
        selected = torch.device(self._device_name)
        for tensor in list(model.parameters()) + list(model.buffers()):
            if tensor.device != selected:
                raise ModelBackendError("MODEL_INFERENCE_FAILED")

    def device_evidence(self) -> dict[str, Any]:
        """Bounded, public-safe evidence that the real model/cache live on the selected device."""
        self._ensure_open()
        if self._model is None or self._torch is None:
            raise ModelBackendError("MODEL_NOT_LOADED")
        torch = self._torch
        selected = torch.device(self._device_name)
        parameters = list(self._model.parameters())
        buffers = list(self._model.buffers())
        if any(tensor.device != selected for tensor in parameters + buffers):
            raise ModelBackendError("MODEL_INFERENCE_FAILED")
        cache = self._new_kv_cache(max_ctx_len=1)
        try:
            if cache.keys.device != selected or cache.values.device != selected:
                raise ModelBackendError("MODEL_INFERENCE_FAILED")
            cache_device = str(cache.keys.device)
        finally:
            del cache
        return {
            "selected_device": self._device_name,
            "parameter_count": len(parameters),
            "parameter_device": str(parameters[0].device) if parameters else "",
            "buffer_count": len(buffers),
            "buffer_device": str(buffers[0].device) if buffers else "",
            "cache_device": cache_device,
        }

    def mark_experimental_deployment_verified(self) -> CheckpointIdentity:
        """Promote the loaded real artifact after the Phase 6 deployment gate.

        Direct Phase 5/demo loads remain test-only.  Only the provider calls this
        after independently validating the exact Windows/Torch/native identity.
        """
        self._ensure_open()
        if self._model is None or self._metadata is None:
            raise ModelBackendError("MODEL_NOT_LOADED")
        self._metadata = replace(
            self._metadata, status="experimental_loaded", test_only=False,
            deployment_identity_verified=True,
        )
        return self._metadata

    def _rollout(self, fields: dict[str, Any]) -> Any:
        torch = self._torch
        selected = torch.device(self._device_name)
        def require(condition: bool) -> None:
            if not condition:
                raise ModelBackendError("MODEL_INPUT_INVALID")
        require(type(fields) is dict and fields.keys() == _ROLLOUT_KEYS)
        require(all(type(tensor) is torch.Tensor and tensor.device == selected
                    and tensor.layout == torch.strided for tensor in fields.values()))
        players = fields["player_ids"]
        require(players.ndim == 2 and players.shape[0] == 1)
        steps = players.shape[1]
        require(1 <= steps <= _MAX_DEMO_STEPS)
        shapes = {
            "num_steps": (1,), "player_ids": (1, steps), "obs_tokens": (1, steps, 4, 48),
            "obs_channels": (1, steps, 4, 512), "avail_tokens": (1, steps, 256, 8),
            "avail_weights": (1, steps, 256),
        }
        for key, shape in shapes.items():
            require(tuple(fields[key].shape) == shape)
        for key in ("num_steps", "player_ids", "obs_tokens", "avail_tokens"):
            require(fields[key].dtype == torch.long)
        require(fields["obs_channels"].dtype in (torch.uint8, torch.int16, torch.int32, torch.int64))
        require(fields["avail_weights"].dtype == torch.float32)
        require(int(fields["num_steps"][0]) == steps)
        require(bool(((players >= 0) & (players < 4)).all()))
        for key in ("obs_tokens", "avail_tokens"):
            require(bool(((fields[key] >= 0) & (fields[key] < 320)).all()))
        channels = fields["obs_channels"]
        require(bool(((channels >= 0) & (channels <= 255)).all()))
        weights = fields["avail_weights"]
        require(bool(torch.isfinite(weights).all() and (weights >= 0).all()))
        require(bool((weights.sum(dim=-1) > 0).all()))
        return self._tensor_dict({k: v.clone() for k, v in fields.items()}, batch_size=[1],
                                 device=self._device_name)

    def forward_synthetic_cached(self, history_fields: dict[str, Any],
                                 next_fields: dict[str, Any], *,
                                 timeout_seconds: float | None = None) -> tuple[Any, Any]:
        """Rebuild a real CPU KV cache from exact synthetic history, then score one step.

        This is an isolated Phase 5 parity seam. It returns probabilities and the
        in-memory cache only to the caller; it never persists a tensor/cache or
        connects a provider, worker, application, UI, capture, or live producer.
        """
        self._ensure_open()
        if self._model is None:
            raise ModelBackendError("MODEL_NOT_LOADED")
        try:
            deadline = _deadline(timeout_seconds)
            torch = self._torch
            cache = self._new_kv_cache()
            history = self._rollout(history_fields)
            next_rollout = self._rollout(next_fields)
            if int(next_rollout["num_steps"][0]) != 1:
                raise ModelBackendError("MODEL_INPUT_INVALID")
            with torch.inference_mode():
                self._model(history, use_cache=True, past_key_values=cache, cache_only=True)
                _check_deadline(deadline)
                output = self._model(next_rollout, use_cache=True,
                                     past_key_values=cache, cache_only=False)
                probabilities = output.avail_probs
                weights = next_rollout["avail_weights"]
                if (tuple(probabilities.shape) != tuple(weights.shape)
                        or not bool(torch.isfinite(probabilities).all())
                        or not bool(((probabilities >= 0) & (probabilities <= 1)).all())
                        or not bool((probabilities[weights == 0] == 0).all())
                        or not torch.allclose(probabilities.sum(dim=-1),
                                              torch.ones_like(weights[..., 0]),
                                              rtol=1e-5, atol=1e-6)):
                    raise ModelBackendError("MODEL_OUTPUT_INVALID")
                _check_deadline(deadline)
                return probabilities[0, -1].clone(), cache
        except ModelBackendError:
            self._clear()
            raise
        except Exception:
            self._clear()
            raise ModelBackendError("MODEL_INFERENCE_FAILED") from None

    def _new_kv_cache(self, *, max_ctx_len: int | None = None) -> Any:
        cache_module = importlib.import_module("guandan_marvel.model.cache")
        config = self._model.decoder.config
        kv_heads = config.num_key_value_heads or config.num_attention_heads
        head_dim = config.hidden_size // config.num_attention_heads
        dtype = next(self._model.parameters()).dtype
        return cache_module.KVCache(
            self._model.decoder_n_layers, 4,
            max_ctx_len=int(self._model.max_game_horizon if max_ctx_len is None else max_ctx_len),
            num_heads=kv_heads, head_dim=head_dim,
            value_heads=kv_heads, value_head_dim=head_dim,
            dtype=dtype, device=self._device_name,
        )

    @staticmethod
    def _valid_probabilities(torch: Any, probabilities: Any, weights: Any) -> bool:
        return (tuple(probabilities.shape) == tuple(weights.shape)
                and bool(torch.isfinite(probabilities).all())
                and bool(((probabilities >= 0) & (probabilities <= 1)).all())
                and bool((probabilities[weights == 0] == 0).all())
                and bool(torch.allclose(probabilities.sum(dim=-1),
                                        torch.ones_like(weights[..., 0]),
                                        rtol=1e-5, atol=1e-6)))

    def forward_confirmed_stepwise(self, fields_sequence: Any, *,
                                   timeout_seconds: float | None = None) -> tuple[Any, Any]:
        """Score an exact confirmed trajectory one step at a time on CPU.

        This evaluation-only seam keeps the real KV cache in memory and is
        suitable for histories longer than the bounded synthetic full-forward
        helper.  It never persists tensors or executes the selected action.
        """
        self._ensure_open()
        if self._model is None:
            raise ModelBackendError("MODEL_NOT_LOADED")
        try:
            sequence = tuple(fields_sequence)
            if not sequence or len(sequence) > int(self._model.max_game_horizon):
                raise ModelBackendError("MODEL_INPUT_INVALID")
            deadline = _deadline(timeout_seconds)
            torch = self._torch
            cache = self._new_kv_cache()
            result = None
            with torch.inference_mode():
                for fields in sequence:
                    rollout = self._rollout(fields)
                    if int(rollout["num_steps"][0]) != 1:
                        raise ModelBackendError("MODEL_INPUT_INVALID")
                    output = self._model(rollout, use_cache=True,
                                         past_key_values=cache, cache_only=False)
                    probabilities = output.avail_probs
                    weights = rollout["avail_weights"]
                    if not self._valid_probabilities(torch, probabilities, weights):
                        raise ModelBackendError("MODEL_OUTPUT_INVALID")
                    result = probabilities[0, -1].clone()
                    _check_deadline(deadline)
            assert result is not None
            return result, cache
        except ModelBackendError:
            self._clear()
            raise
        except Exception:
            self._clear()
            raise ModelBackendError("MODEL_INFERENCE_FAILED") from None

    def clone_confirmed_cache(self, cache: Any) -> Any:
        """Clone an in-memory evaluation cache for speculative prefix scoring."""
        self._ensure_open()
        if self._model is None or cache is None:
            raise ModelBackendError("MODEL_NOT_LOADED")
        try:
            clone = self._new_kv_cache()
            clone.keys.copy_(cache.keys)
            clone.values.copy_(cache.values)
            clone._seq_lens.copy_(cache._seq_lens)
            clone._position_ids.copy_(cache._position_ids)
            clone._step_mask.copy_(cache._step_mask)
            return clone
        except Exception:
            raise ModelBackendError("MODEL_INFERENCE_FAILED") from None

    def forward_confirmed_cached_step(self, cache: Any, fields: dict[str, Any], *,
                                      timeout_seconds: float | None = None) -> Any:
        """Append one confirmed/private-prefix step to an in-memory cache."""
        self._ensure_open()
        if self._model is None:
            raise ModelBackendError("MODEL_NOT_LOADED")
        try:
            deadline = _deadline(timeout_seconds)
            torch = self._torch
            rollout = self._rollout(fields)
            if int(rollout["num_steps"][0]) != 1 or cache is None:
                raise ModelBackendError("MODEL_INPUT_INVALID")
            with torch.inference_mode():
                output = self._model(rollout, use_cache=True,
                                     past_key_values=cache, cache_only=False)
                probabilities = output.avail_probs
                weights = rollout["avail_weights"]
                if not self._valid_probabilities(torch, probabilities, weights):
                    raise ModelBackendError("MODEL_OUTPUT_INVALID")
                result = probabilities[0, -1].clone()
                _check_deadline(deadline)
                return result
        except ModelBackendError:
            self._clear()
            raise
        except Exception:
            self._clear()
            raise ModelBackendError("MODEL_INFERENCE_FAILED") from None

    def forward_synthetic(self, fields: dict[str, Any], *, timeout_seconds: float | None = None) -> Any:
        """Return only the final [256] row probabilities; never execute a row."""
        self._ensure_open()
        if self._model is None:
            raise ModelBackendError("MODEL_NOT_LOADED")
        try:
            deadline = _deadline(timeout_seconds)
            torch = self._torch
            with torch.inference_mode():
                rollout = self._rollout(fields)
                _check_deadline(deadline)
                output = self._model(rollout, use_cache=False, past_key_values=None, cache_only=False)
                probabilities = output.avail_probs
                weights = rollout["avail_weights"]
                if (tuple(probabilities.shape) != tuple(weights.shape)
                        or not bool(torch.isfinite(probabilities).all())
                        or not bool(((probabilities >= 0) & (probabilities <= 1)).all())
                        or not bool((probabilities[weights == 0] == 0).all())
                        or not torch.allclose(probabilities.sum(dim=-1), torch.ones_like(weights[..., 0]),
                                              rtol=1e-5, atol=1e-6)):
                    raise ModelBackendError("MODEL_OUTPUT_INVALID")
                result = probabilities[0, -1].clone()
                _check_deadline(deadline)
                return result
        except ModelBackendError:
            self._clear()
            raise
        except Exception:
            self._clear()
            raise ModelBackendError("MODEL_INFERENCE_FAILED") from None

    def close(self) -> None:
        self._clear()
        self._closed = True
