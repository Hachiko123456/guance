"""Restricted reader for one pinned evaluation artifact; never a training loader.

Hash a bounded immutable byte snapshot before deserializing that same snapshot.
No pickle fallback, safe-global additions, upstream loader, or payload logging.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import importlib
import io
import json
import math
from types import MappingProxyType
from typing import Any

from ..errors import ModelBackendError
from ..manifest import ModelBundle
from .identity import (
    CHECKPOINT_BYTES, CHECKPOINT_SCHEMA, CHECKPOINT_SHA256, CONFIG_SHA256,
    PROTOCOL, SOURCE_COMMIT, SOURCE_SHA256, TRAINING_BACKEND_SHA256,
    CheckpointIdentity, validate_descriptor,
)
from .state_contract import STATE_SHAPES

_PAYLOAD_KEYS = frozenset({
    "schema", "algorithm", "architecture", "protocol_version", "backend", "backend_identity",
    "resolved_config", "resolved_config_hash", "agent_state_dict", "iteration", "merged_policy",
    "source_fingerprint", "source_hash", "source_parent_commit", "training_data_provenance",
    "resume_classification", "training_resume_allowed", "distributed_training",
})


def _plain_json(value: Any, depth: int = 0) -> None:
    if depth > 32:
        raise ModelBackendError("MODEL_LOAD_FAILED")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _plain_json(item, depth + 1)
        return
    if type(value) is list:
        for item in value:
            _plain_json(item, depth + 1)
        return
    raise ModelBackendError("MODEL_LOAD_FAILED")


def canonical_json_hash(value: Any) -> str:
    """No default=str, NaN, object coercion, or non-string mapping keys."""
    try:
        _plain_json(value)
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise ModelBackendError("MODEL_LOAD_FAILED") from None


def verified_artifact_bytes(bundle: ModelBundle) -> bytes:
    if not isinstance(bundle, ModelBundle) or bundle.legacy:
        raise ModelBackendError("MODEL_SOURCE_INVALID")
    validate_descriptor(bundle.manifest)
    bundle.verify()
    try:
        with bundle.artifact_path.open("rb") as stream:
            raw = stream.read(CHECKPOINT_BYTES + 1)
    except OSError:
        raise ModelBackendError("ARTIFACT_MISSING") from None
    if len(raw) != CHECKPOINT_BYTES or hashlib.sha256(raw).hexdigest() != CHECKPOINT_SHA256:
        raise ModelBackendError("ARTIFACT_HASH_MISMATCH")
    return raw


def _torch() -> Any:
    try:
        return importlib.import_module("torch")
    except Exception:
        raise ModelBackendError("MODEL_UNAVAILABLE") from None


def _restricted_load(raw: bytes) -> Any:
    # Private primitive, called only after verified_artifact_bytes in the public entry.
    torch = _torch()
    try:
        return torch.load(io.BytesIO(raw), map_location="cpu", weights_only=True)
    except Exception:
        raise ModelBackendError("MODEL_LOAD_FAILED") from None


def _require(condition: bool) -> None:
    if not condition:
        raise ModelBackendError("MODEL_LOAD_FAILED")


def _identity_hash(value: Any, expected: str, keys: set[str]) -> None:
    _require(type(value) is dict and value.keys() == keys)
    _require(type(value["hash"]) is str and value["hash"] == expected)
    _require(canonical_json_hash({k: v for k, v in value.items() if k != "hash"}) == expected)


@dataclass(frozen=True, slots=True)
class _ValidatedCheckpoint:
    identity: CheckpointIdentity
    resolved_config: dict[str, Any] = field(repr=False)
    state_dict: Mapping[str, Any] = field(repr=False)


def _parse_payload(payload: Any, torch: Any) -> _ValidatedCheckpoint:
    _require(type(payload) is dict and payload.keys() == _PAYLOAD_KEYS)
    expected = {
        "schema": CHECKPOINT_SCHEMA, "algorithm": "ippo", "architecture": "v1.3",
        "protocol_version": PROTOCOL, "backend": "cpp", "iteration": 100,
        "resume_classification": "evaluation_only", "training_resume_allowed": False,
        "resolved_config_hash": CONFIG_SHA256, "source_hash": SOURCE_SHA256,
        "source_parent_commit": SOURCE_COMMIT,
    }
    for key, value in expected.items():
        _require(type(payload[key]) is type(value) and payload[key] == value)
    config = payload["resolved_config"]
    _require(type(config) is dict and canonical_json_hash(config) == CONFIG_SHA256)
    _require(config.get("algo_name") == "ippo")
    _require(type(config.get("model")) is dict and config["model"].get("architecture") == "v1.3")
    _require(type(config.get("game")) is dict and config["game"].get("protocol_version") == PROTOCOL)

    source = payload["source_fingerprint"]
    _identity_hash(source, SOURCE_SHA256, {"schema", "components", "hash"})
    _require(source["schema"] == "guandan-marvel-source-fingerprint-v1")
    _require(type(source["components"]) is dict)
    for component in source["components"].values():
        _require(type(component) is dict and component.keys() == {"files", "hash"})
        _require(type(component["files"]) is list)
        _require(canonical_json_hash(component["files"]) == component["hash"])
    backend = payload["backend_identity"]
    _identity_hash(backend, TRAINING_BACKEND_SHA256, {
        "schema", "backend", "module", "game_identifier", "protocol_version", "implementation", "hash",
    })
    _require(backend["schema"] == "guandan-marvel-backend-identity-v1")
    _require(backend["backend"] == "cpp" and backend["protocol_version"] == PROTOCOL)
    # This is the ORIGINAL training identity, not a claim about this deployment.
    merged = payload["merged_policy"]
    _require(type(merged) is dict and merged.keys() == {"identity", "beta", "frozen_for_evaluation"})
    _require(merged["identity"] == "current_policy_copy" and type(merged["beta"]) is float
             and merged["beta"] == 0.0 and merged["frozen_for_evaluation"] is True)
    provenance = payload["training_data_provenance"]
    _require(type(provenance) is dict and type(provenance.get("hash")) is str)
    _identity_hash(provenance, provenance["hash"], {
        "complete", "deal_count", "deal_hashes", "domain", "generator_version", "hash",
        "initial_env_seeds", "manifest_hash", "rank_provenance_hashes", "schema",
        "seed_semantics", "world_size",
    })
    distributed = payload["distributed_training"]
    _require(type(distributed) is dict and distributed.keys() == {
        "schema", "world_size", "checkpoint_manifest_hash", "global_counters",
    })
    _plain_json(distributed)
    # Neither provenance nor distributed-training data leave this reader.
    state = payload["agent_state_dict"]
    _require(type(state) is dict and state.keys() == STATE_SHAPES.keys() and len(state) == 78)
    for name, shape in STATE_SHAPES.items():
        tensor = state[name]
        _require(type(tensor) is torch.Tensor)
        _require(tensor.device.type == "cpu" and tensor.layout == torch.strided)
        _require(tensor.dtype == torch.float32 and tuple(tensor.shape) == shape)
        _require(bool(torch.isfinite(tensor).all()))
    return _ValidatedCheckpoint(CheckpointIdentity(), deepcopy(config), MappingProxyType(dict(state)))


def load_checkpoint(bundle: ModelBundle) -> _ValidatedCheckpoint:
    """Only an explicit, non-legacy fixed bundle can reach deserialization."""
    raw = verified_artifact_bytes(bundle)
    payload = _restricted_load(raw)
    try:
        return _parse_payload(payload, _torch())
    except ModelBackendError:
        raise
    except Exception:
        raise ModelBackendError("MODEL_LOAD_FAILED") from None
