"""Strict CPU loader for the DanKS V3 top10 selector checkpoint (D01).

Load contract (fail-closed, no fallback weights):

- bytes identity: SHA-256 must match the expected checkpoint identity unless
  the caller explicitly passes a different ``expected_sha256`` (tests);
- ``torch.load(weights_only=True, map_location="cpu")`` only;
- payload structure: the exact schema key set recorded in phase 1;
- contract fields must equal the vendored schema constants (state 444,
  candidate 121, history 15x61, feature version
  ``top10_selector_v12_calibrated_tactics1``);
- ``build_selector_from_checkpoint`` builds the architecture, then a strict
  ``load_state_dict`` must report zero missing/unexpected keys;
- every state-dict tensor must be finite; a synthetic probe forward under
  ``inference_mode`` must produce finite logits;
- the returned module is in ``eval()`` mode; callers run further forwards
  under ``torch.inference_mode()``.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
from pathlib import Path
from typing import Any

import torch

from .source_identity import CHECKPOINT_SHA256
from .vendor.danks.training import schema
from .vendor.danks.training.model import (
    build_selector_from_checkpoint, selector_model_type,
)

PAYLOAD_KEYS = {
    "action_support", "candidate_capacity", "candidate_dim", "feature_version",
    "history_event_dim", "history_event_semantics", "history_length",
    "history_protocol", "kind", "model_config", "model_state_dict",
    "model_type", "state_dim", "team_belief_protocol",
}


class DanksV3LoaderError(ValueError):
    """Strict-load failure; carries a stable code and never echoes weights."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class LoadedSelector:
    model: Any  # vendored Top10Selector (torch nn.Module)
    checkpoint_sha256: str
    model_type: str
    feature_version: str
    state_dim: int
    candidate_dim: int
    history_length: int
    history_event_dim: int
    action_support: str
    candidate_capacity: int


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_selector(checkpoint_path: str | Path, *,
                  expected_sha256: str = CHECKPOINT_SHA256) -> LoadedSelector:
    path = Path(checkpoint_path)
    if not path.is_file():
        raise DanksV3LoaderError("MODEL_SOURCE_INVALID",
                                 "checkpoint file not found")
    data = path.read_bytes()
    actual_sha256 = _sha256_bytes(data)
    if actual_sha256 != expected_sha256:
        raise DanksV3LoaderError(
            "MODEL_SOURCE_IDENTITY_MISMATCH",
            f"checkpoint sha256 {actual_sha256} != expected {expected_sha256}")
    try:
        payload = torch.load(io.BytesIO(data), weights_only=True,
                             map_location="cpu")
    except Exception as exc:
        raise DanksV3LoaderError("MODEL_SOURCE_INVALID",
                                 "checkpoint is not a weights_only payload") from exc
    if not isinstance(payload, dict) or set(payload) != PAYLOAD_KEYS:
        raise DanksV3LoaderError("MODEL_CHECKPOINT_INVALID",
                                 "payload keys do not match the frozen schema")
    _check_contract_fields(payload)
    try:
        model = build_selector_from_checkpoint(payload, device="cpu")
    except Exception as exc:
        raise DanksV3LoaderError("MODEL_CHECKPOINT_INVALID",
                                 "architecture build failed") from exc
    state_dict = payload["model_state_dict"]
    missing, unexpected = model.load_state_dict(state_dict, strict=True)
    if missing or unexpected:
        raise DanksV3LoaderError(
            "MODEL_WEIGHTS_INVALID",
            f"strict load reported missing={len(missing)} unexpected={len(unexpected)}")
    for name, tensor in state_dict.items():
        if not torch.isfinite(tensor).all().item():
            raise DanksV3LoaderError(
                "MODEL_WEIGHTS_INVALID", f"non-finite weights in {name}")
    if selector_model_type(model) != str(payload["model_type"]):
        raise DanksV3LoaderError("MODEL_CHECKPOINT_INVALID",
                                 "built architecture does not match model_type")
    model.eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    _probe_forward(model)
    return LoadedSelector(
        model=model, checkpoint_sha256=actual_sha256,
        model_type=str(payload["model_type"]),
        feature_version=str(payload["feature_version"]),
        state_dim=int(payload["state_dim"]),
        candidate_dim=int(payload["candidate_dim"]),
        history_length=int(payload["history_length"]),
        history_event_dim=int(payload["history_event_dim"]),
        action_support=str(payload["action_support"]),
        candidate_capacity=int(payload["candidate_capacity"]),
    )


def _check_contract_fields(payload: dict[str, Any]) -> None:
    expected = {
        "state_dim": schema.STATE_DIM,
        "candidate_dim": schema.CANDIDATE_DIM,
        "history_length": schema.HISTORY_LENGTH,
        "history_event_dim": schema.HISTORY_EVENT_DIM,
        "feature_version": schema.FEATURE_VERSION,
        "history_protocol": schema.HISTORY_PROTOCOL,
        "history_event_semantics": schema.HISTORY_EVENT_SEMANTICS,
        "action_support": schema.STRUCTURED_TOPK,
        "candidate_capacity": schema.TOPK,
        "model_type": schema.FEATURE_VERSION,
        "team_belief_protocol": schema.TEAM_BELIEF_PROTOCOL,
        "kind": "top10_online",
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise DanksV3LoaderError(
                "MODEL_VERSION_UNSUPPORTED",
                f"payload {key}={payload.get(key)!r} != expected {value!r}")


def _probe_forward(model: Any) -> None:
    """Synthetic forward under inference_mode; validates wiring finiteness."""
    state = torch.zeros((1, schema.STATE_DIM), dtype=torch.float32)
    candidates = torch.zeros((1, schema.TOPK, schema.CANDIDATE_DIM),
                             dtype=torch.float32)
    mask = torch.ones((1, schema.TOPK), dtype=torch.float32)
    history = torch.zeros((1, schema.HISTORY_LENGTH, schema.HISTORY_EVENT_DIM),
                          dtype=torch.float32)
    with torch.inference_mode():
        logits = model.policy_forward(state, candidates, mask, history)
    if not torch.isfinite(logits).all().item():
        raise DanksV3LoaderError("MODEL_WEIGHTS_INVALID",
                                 "probe forward produced non-finite logits")
