"""Pinned descriptor and bounded identity; no Torch, paths, or opaque metadata."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from guance.contracts import ModelCapabilities, ModelMetadata
from ..errors import ModelBackendError
from ..manifest import ModelManifest

PROVIDER = "guandan_marvel_v2"
CHECKPOINT_SHA256 = "44b8a41371965279c7ce8f3e592162689e9a1b32cb4a07147f2a8ea58ff89696"
CHECKPOINT_BYTES = 8_316_529
CHECKPOINT_SCHEMA = "guandan-marvel-evaluation-checkpoint-v1"
PROTOCOL = "GD-ACTION-0.2-MARVEL|GD-ENV-0.2-MARVEL|GD-ENCODING-0.2-MARVEL"
CONFIG_SHA256 = "944ac18bd65a1082fff70822960b98361ba5ab5c35ad67c6b0b92e0b5b382330"
SOURCE_SHA256 = "831e92294daea36297013cd2d079b53b887093972a3e66e7f7145c0dff3b495c"
SOURCE_COMMIT = "18507bdaa8dd86b25817060d64852a7531cbd8e9"
TRAINING_BACKEND_SHA256 = "5b8b7300f8bb32f0892959990ff9414dce67b51bd371e1063a8d9e78894c30de"

SEMANTIC_FIELDS = MappingProxyType({
    "provider": PROVIDER, "backend": "guandan", "runtime": "torch",
    "input_contract": "GuandanMARVELV2.Rollout.v1",
    "output_contract": "GuandanMARVELV2.ActionRow.v2",
    "model_version": "MARVEL-V2-v1.3-iter000100",
    "training_version": "M09-IPPO-pilot-20261011",
    "rules_version": "GD-RULES-0.1", "action_version": "GD-ACTION-0.2-MARVEL",
    "environment_version": "GD-ENV-0.2-MARVEL", "encoding_version": "GD-ENCODING-0.2-MARVEL",
    "algorithm": "ippo", "architecture": "v1.3",
})
DESCRIPTOR_FIELDS = MappingProxyType({
    **SEMANTIC_FIELDS, "artifact_format": "torch_checkpoint",
    "artifact_sha256": CHECKPOINT_SHA256,
})
DEPLOYMENT_ARTIFACT_FORMAT = "guance_marvel_v2_deployment_v1"


def fixed_manifest(artifact: str) -> ModelManifest:
    """An explicit descriptor, not a production readiness or adapter claim.

    Schema 1 requires 'recommend'; this denotes the policy artifact, not an
    available Guance advisor. No complete-action/public+self capability is claimed.
    """
    return ModelManifest.from_dict({
        "schema_version": 1, "protocol_version": 1, "artifact": artifact,
        **DESCRIPTOR_FIELDS, "capabilities": {"recommend": True, "next_token_policy": True},
    })


def validate_descriptor(manifest: ModelManifest) -> None:
    ModelManifest.from_dict(manifest.to_dict())
    for name, expected in DESCRIPTOR_FIELDS.items():
        if getattr(manifest, name) != expected:
            code = {
                "provider": "PROVIDER_UNKNOWN", "backend": "BACKEND_UNSUPPORTED",
                "runtime": "RUNTIME_UNSUPPORTED", "artifact_sha256": "ARTIFACT_HASH_MISMATCH",
                "input_contract": "CONTRACT_UNSUPPORTED", "output_contract": "CONTRACT_UNSUPPORTED",
            }.get(name, "MODEL_VERSION_UNSUPPORTED")
            raise ModelBackendError(code)
    caps = manifest.capabilities
    if (not isinstance(caps, Mapping) or caps.get("next_token_policy") is not True
            or any(value and name not in {"recommend", "next_token_policy"}
                   for name, value in caps.items())):
        raise ModelBackendError("CONTRACT_UNSUPPORTED")


def validate_provider_manifest(manifest: ModelManifest) -> None:
    """Validate the explicit Phase 6 deployment-bundle manifest.

    The outer artifact is a bounded deployment descriptor, not the checkpoint.
    The descriptor independently pins and verifies the checkpoint before loading.
    """
    ModelManifest.from_dict(manifest.to_dict())
    for name, expected in SEMANTIC_FIELDS.items():
        if getattr(manifest, name) != expected:
            code = {
                "provider": "PROVIDER_UNKNOWN", "backend": "BACKEND_UNSUPPORTED",
                "runtime": "RUNTIME_UNSUPPORTED", "input_contract": "CONTRACT_UNSUPPORTED",
                "output_contract": "CONTRACT_UNSUPPORTED",
            }.get(name, "MODEL_VERSION_UNSUPPORTED")
            raise ModelBackendError(code)
    if manifest.artifact_format != DEPLOYMENT_ARTIFACT_FORMAT:
        raise ModelBackendError("MODEL_VERSION_UNSUPPORTED")
    caps = manifest.capabilities
    if (not isinstance(caps, Mapping) or caps.get("next_token_policy") is not True
            or any(value and name not in {"recommend", "next_token_policy"}
                   for name, value in caps.items())):
        raise ModelBackendError("CONTRACT_UNSUPPORTED")


@dataclass(frozen=True, slots=True)
class CheckpointIdentity:
    """Infrastructure-private projection. Never serialize the loaded payload."""

    status: str = "validated"
    provider: str = PROVIDER
    checkpoint_sha256: str = CHECKPOINT_SHA256
    checkpoint_schema: str = CHECKPOINT_SCHEMA
    algorithm: str = "ippo"
    architecture: str = "v1.3"
    iteration: int = 100
    classification: str = "evaluation_only"
    resume_classification: str = "evaluation_only"
    protocol_version: str = PROTOCOL
    resolved_config_hash: str = CONFIG_SHA256
    source_hash: str = SOURCE_SHA256
    source_fingerprint_hash: str = SOURCE_SHA256
    source_parent_commit: str = SOURCE_COMMIT
    training_backend_identity_hash: str = TRAINING_BACKEND_SHA256
    tensor_count: int = 78
    training_resume_allowed: bool = False
    experimental: bool = True
    test_only: bool = True
    production_ready: bool = False
    public_self_supported: bool = False
    deployment_identity_verified: bool = False
    formal_evaluation_status: str = "not_run"
    champion_status: str = "not_established"
    strength_claim: bool = False

    def to_model_metadata(self) -> ModelMetadata:
        """Project only fixed public fields, never private identity/payload blobs.

        The existing contract has no experimental flag, so status and test_only
        label this demo explicitly. Empty wire contracts mean no public+self
        transport adapter; next-token scores are not complete-action advice.
        """
        status = {
            "validated": "experimental_artifact_validated",
            "demo_loaded": "experimental_demo_loaded",
        }.get(self.status, "experimental_unavailable")
        return ModelMetadata(
            status=status, provider=PROVIDER, backend=DESCRIPTOR_FIELDS["backend"],
            runtime=DESCRIPTOR_FIELDS["runtime"], model_version=DESCRIPTOR_FIELDS["model_version"],
            model_sha256=CHECKPOINT_SHA256, legacy=False, test_only=True,
            score_kind="marvel_v2_row_probability",
            input_contract=DESCRIPTOR_FIELDS["input_contract"],
            output_contract=DESCRIPTOR_FIELDS["output_contract"],
            wire_input_contract="", wire_output_contract="",
            capabilities=ModelCapabilities(next_token_policy=True),
        )
