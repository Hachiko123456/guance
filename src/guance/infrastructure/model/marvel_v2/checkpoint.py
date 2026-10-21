"""Restricted, data-driven reader for MARVEL V2 evaluation checkpoints.

The deployment descriptor supplies the expected artifact identity and the
checkpoint expectations (schema/algorithm/architecture/protocol/iteration/
config/source/backend/classification). This module validates the payload
against those expectations with exact enumerated payload variants, verifies
artifact bytes and SHA-256 before any ``weights_only=True`` load, and never
exposes payload contents. No global checkpoint hash, iteration or config
constant is used for loading.
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
import re
from types import MappingProxyType
from typing import Any

from ..errors import ModelBackendError
from ..manifest import ModelBundle
from .identity import (
    ALLOWED_ALGORITHMS, ALLOWED_ARCHITECTURES, ALLOWED_PAYLOAD_VARIANTS,
    CHECKPOINT_SCHEMA, PROTOCOL, CheckpointIdentity, validate_contract_fields,
)
from .state_contract import STATE_SHAPES

VARIANT_DDP = "ddp_merged_frozen_v1"
VARIANT_FLAT = "flat_not_smoothed_v1"

_COMMON_PAYLOAD_KEYS = frozenset({
    "schema", "algorithm", "architecture", "protocol_version", "backend", "backend_identity",
    "resolved_config", "resolved_config_hash", "agent_state_dict", "iteration", "merged_policy",
    "source_fingerprint", "source_hash", "source_parent_commit", "training_data_provenance",
    "resume_classification", "training_resume_allowed",
})
_VARIANT_EXTRA_KEYS = {VARIANT_DDP: frozenset({"distributed_training"}), VARIANT_FLAT: frozenset()}
_MERGED_KEYS = {
    VARIANT_DDP: frozenset({"identity", "beta", "frozen_for_evaluation"}),
    VARIANT_FLAT: frozenset({"identity", "beta", "not_smoothed_when_beta_zero"}),
}
_MERGED_FLAG = {VARIANT_DDP: "frozen_for_evaluation", VARIANT_FLAT: "not_smoothed_when_beta_zero"}
_PROVENANCE_KEYS = {
    VARIANT_DDP: frozenset({"complete", "deal_count", "deal_hashes", "domain", "generator_version",
                            "hash", "initial_env_seeds", "manifest_hash", "rank_provenance_hashes",
                            "schema", "seed_semantics", "world_size"}),
    VARIANT_FLAT: frozenset({"complete", "deal_count", "deal_hashes", "domain", "generator_version",
                             "hash", "initial_env_seed", "manifest_hash", "records_hash",
                             "rollout_batch_size", "schema", "seed_semantics"}),
}
_PROVENANCE_SCHEMA = {
    VARIANT_DDP: "guandan-marvel-ddp-training-data-provenance-v1",
    VARIANT_FLAT: "guandan-marvel-training-data-provenance-v1",
}
_DISTRIBUTED_KEYS = frozenset({"schema", "world_size", "checkpoint_manifest_hash", "global_counters"})
_BACKEND_KEYS = frozenset({"schema", "backend", "module", "game_identifier", "protocol_version",
                           "implementation", "hash"})
_FINGERPRINT_KEYS = frozenset({"schema", "components", "hash"})
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class CheckpointExpectation:
    """Bundle-declared checkpoint identity. Structural validation only; no I/O."""

    filename: str
    bytes: int
    sha256: str
    schema: str
    algorithm: str
    architecture: str
    protocol_version: str
    iteration: int
    classification: str
    resume_classification: str
    training_resume_allowed: bool
    resolved_config_hash: str
    source_hash: str
    source_parent_commit: str
    training_backend_identity_hash: str
    payload_variant: str
    model_version: str = ""
    training_version: str = ""

    def validate(self) -> "CheckpointExpectation":
        def fail(code: str) -> None:
            raise ModelBackendError(code)

        if (not isinstance(self.filename, str) or not self.filename or len(self.filename) > 260
                or any(char in self.filename for char in '<>:"/\\|?*')
                or any(ord(char) < 32 or ord(char) == 127 for char in self.filename)
                or self.filename.endswith((" ", "."))):
            fail("ARTIFACT_PATH_INVALID")
        if type(self.bytes) is not int or not 0 < self.bytes < 8 * 1024 * 1024 * 1024:
            fail("ARTIFACT_SIZE_MISMATCH")
        if not isinstance(self.sha256, str) or not _SHA256.fullmatch(self.sha256):
            fail("ARTIFACT_HASH_MISMATCH")
        if self.schema != CHECKPOINT_SCHEMA:
            fail("MODEL_VERSION_UNSUPPORTED")
        if self.algorithm not in ALLOWED_ALGORITHMS or self.architecture not in ALLOWED_ARCHITECTURES:
            fail("MODEL_VERSION_UNSUPPORTED")
        if self.protocol_version != PROTOCOL:
            fail("MODEL_VERSION_UNSUPPORTED")
        if type(self.iteration) is not int or not 0 <= self.iteration < 2**31:
            fail("MODEL_VERSION_UNSUPPORTED")
        if self.classification != "evaluation_only" or self.resume_classification != "evaluation_only" \
                or self.training_resume_allowed is not False:
            fail("MODEL_VERSION_UNSUPPORTED")
        for name, value in (("resolved_config_hash", self.resolved_config_hash),
                            ("source_hash", self.source_hash),
                            ("training_backend_identity_hash", self.training_backend_identity_hash)):
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                fail("MODEL_LOAD_FAILED")
        if not isinstance(self.source_parent_commit, str) or not _COMMIT.fullmatch(self.source_parent_commit):
            fail("MODEL_LOAD_FAILED")
        if self.payload_variant not in ALLOWED_PAYLOAD_VARIANTS:
            fail("MODEL_VERSION_UNSUPPORTED")
        for value in (self.model_version, self.training_version):
            if not isinstance(value, str) or len(value) > 128:
                fail("MODEL_VERSION_UNSUPPORTED")
        return self


def _plain_json(value: Any, depth: int = 0) -> None:
    if depth > 32:
        raise ModelBackendError("MODEL_LOAD_FAILED")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, dict) and all(type(key) is str for key in value):
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


def verified_artifact_bytes(bundle: ModelBundle, expectation: CheckpointExpectation) -> bytes:
    if not isinstance(bundle, ModelBundle) or bundle.legacy:
        raise ModelBackendError("MODEL_SOURCE_INVALID")
    if type(expectation) is not CheckpointExpectation:
        raise ModelBackendError("MODEL_LOAD_FAILED")
    expectation.validate()
    validate_contract_fields(bundle.manifest)
    if (bundle.manifest.algorithm != expectation.algorithm
            or bundle.manifest.architecture != expectation.architecture):
        raise ModelBackendError("MODEL_VERSION_UNSUPPORTED")
    bundle.verify()
    if bundle.manifest.artifact_sha256 != expectation.sha256:
        raise ModelBackendError("ARTIFACT_HASH_MISMATCH")
    try:
        with bundle.artifact_path.open("rb") as stream:
            raw = stream.read(expectation.bytes + 1)
    except OSError:
        raise ModelBackendError("ARTIFACT_MISSING") from None
    if len(raw) != expectation.bytes:
        raise ModelBackendError("ARTIFACT_SIZE_MISMATCH")
    if hashlib.sha256(raw).hexdigest() != expectation.sha256:
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


def _identity_hash(value: Any, expected: str, keys: frozenset[str]) -> None:
    _require(isinstance(value, dict) and value.keys() == keys)
    _require(type(value["hash"]) is str and value["hash"] == expected)
    _require(canonical_json_hash({k: v for k, v in value.items() if k != "hash"}) == expected)


@dataclass(frozen=True, slots=True)
class _ValidatedCheckpoint:
    identity: CheckpointIdentity
    resolved_config: dict[str, Any] = field(repr=False)
    state_dict: Mapping[str, Any] = field(repr=False)


def _validate_merged_policy(merged: Any, variant: str) -> None:
    _require(isinstance(merged, dict) and frozenset(merged.keys()) == _MERGED_KEYS[variant])
    _require(merged["identity"] == "current_policy_copy")
    _require(type(merged["beta"]) is float and merged["beta"] == 0.0)
    _require(merged[_MERGED_FLAG[variant]] is True)


def _validate_provenance(provenance: Any, variant: str) -> None:
    _require(isinstance(provenance, dict))
    _require(provenance.get("hash") is not None)
    _require(provenance["schema"] == _PROVENANCE_SCHEMA[variant])
    _identity_hash(provenance, str(provenance["hash"]), _PROVENANCE_KEYS[variant])
    _require(provenance["complete"] is True)
    _require(provenance["domain"] == "training")
    _require(isinstance(provenance["manifest_hash"], str)
             and _SHA256.fullmatch(provenance["manifest_hash"]) is not None)


def _validate_distributed(distributed: Any) -> None:
    _require(isinstance(distributed, dict) and frozenset(distributed.keys()) == _DISTRIBUTED_KEYS)
    _require(distributed["schema"] == "guandan-marvel-ddp-checkpoint-v2")
    _require(type(distributed["world_size"]) is int and 1 <= distributed["world_size"] <= 1024)
    _require(isinstance(distributed["checkpoint_manifest_hash"], str)
             and _SHA256.fullmatch(distributed["checkpoint_manifest_hash"]) is not None)
    _plain_json(distributed["global_counters"])


def _validate_source_fingerprint(source: Any, expectation: CheckpointExpectation) -> None:
    _identity_hash(source, expectation.source_hash, _FINGERPRINT_KEYS)
    _require(source["schema"] == "guandan-marvel-source-fingerprint-v1")
    _require(isinstance(source["components"], dict) and source["components"])
    for component in source["components"].values():
        _require(isinstance(component, dict) and component.keys() == {"files", "hash"})
        _require(type(component["files"]) is list)
        _require(canonical_json_hash(component["files"]) == component["hash"])


def _validate_backend_identity(backend: Any, expectation: CheckpointExpectation) -> None:
    _identity_hash(backend, expectation.training_backend_identity_hash, _BACKEND_KEYS)
    _require(backend["schema"] == "guandan-marvel-backend-identity-v1")
    _require(backend["backend"] == "cpp")
    _require(backend["protocol_version"] == expectation.protocol_version)


def _parse_payload(payload: Any, torch: Any, expectation: CheckpointExpectation) -> _ValidatedCheckpoint:
    expectation.validate()
    variant = expectation.payload_variant
    allowed_keys = _COMMON_PAYLOAD_KEYS | _VARIANT_EXTRA_KEYS[variant]
    _require(isinstance(payload, dict) and payload.keys() == allowed_keys)
    expected = {
        "schema": expectation.schema, "algorithm": expectation.algorithm,
        "architecture": expectation.architecture, "protocol_version": expectation.protocol_version,
        "backend": "cpp", "iteration": expectation.iteration,
        "resume_classification": expectation.resume_classification,
        "training_resume_allowed": expectation.training_resume_allowed,
        "resolved_config_hash": expectation.resolved_config_hash,
        "source_hash": expectation.source_hash,
        "source_parent_commit": expectation.source_parent_commit,
    }
    for key, value in expected.items():
        _require(type(payload[key]) is type(value) and payload[key] == value)
    config = payload["resolved_config"]
    _require(isinstance(config, dict) and canonical_json_hash(config) == expectation.resolved_config_hash)
    _require(config.get("algo_name") == expectation.algorithm)
    _require(isinstance(config.get("model"), dict)
             and config["model"].get("architecture") == expectation.architecture)
    _require(isinstance(config.get("game"), dict)
             and config["game"].get("protocol_version") == expectation.protocol_version)

    _validate_source_fingerprint(payload["source_fingerprint"], expectation)
    _validate_backend_identity(payload["backend_identity"], expectation)
    _validate_merged_policy(payload["merged_policy"], variant)
    _validate_provenance(payload["training_data_provenance"], variant)
    if variant == VARIANT_DDP:
        _validate_distributed(payload["distributed_training"])

    # Provenance/distributed data never leave this reader.
    state = payload["agent_state_dict"]
    _require(isinstance(state, dict) and state.keys() == STATE_SHAPES.keys() and len(state) == 78)
    for name, shape in STATE_SHAPES.items():
        tensor = state[name]
        _require(type(tensor) is torch.Tensor)
        _require(tensor.device.type == "cpu" and tensor.layout == torch.strided)
        _require(tensor.dtype == torch.float32 and tuple(tensor.shape) == shape)
        _require(bool(torch.isfinite(tensor).all()))
    identity = CheckpointIdentity(
        status="validated", checkpoint_sha256=expectation.sha256,
        checkpoint_schema=expectation.schema, algorithm=expectation.algorithm,
        architecture=expectation.architecture, iteration=expectation.iteration,
        classification=expectation.classification,
        resume_classification=expectation.resume_classification,
        protocol_version=expectation.protocol_version,
        resolved_config_hash=expectation.resolved_config_hash,
        source_hash=expectation.source_hash, source_fingerprint_hash=expectation.source_hash,
        source_parent_commit=expectation.source_parent_commit,
        training_backend_identity_hash=expectation.training_backend_identity_hash,
        tensor_count=len(state), training_resume_allowed=expectation.training_resume_allowed,
        model_version=expectation.model_version, training_version=expectation.training_version,
    )
    return _ValidatedCheckpoint(identity=identity, resolved_config=deepcopy(config),
                                state_dict=MappingProxyType(dict(state)))


def load_checkpoint(bundle: ModelBundle, expectation: CheckpointExpectation) -> _ValidatedCheckpoint:
    """Only a verified bundle with a declared expectation can reach deserialization."""
    raw = verified_artifact_bytes(bundle, expectation)
    payload = _restricted_load(raw)
    try:
        return _parse_payload(payload, _torch(), expectation)
    except ModelBackendError:
        raise
    except Exception:
        raise ModelBackendError("MODEL_LOAD_FAILED") from None
