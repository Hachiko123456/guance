"""Stable MARVEL V2 contract fields and the dynamic checkpoint identity record.

No iteration, artifact hash, config hash or source commit constants live here any
more: the deployment descriptor built from each bundle supplies the artifact
identity, checkpoint expectations and runtime pins. Only the stable contract
(provider/backend/contracts/rules/action/env/encoding/algorithm/architecture),
the checkpoint schema, the protocol string and the payload-variant enumeration
are fixed in code.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from guance.contracts import ModelCapabilities, ModelMetadata
from ..errors import ModelBackendError
from ..manifest import ModelManifest

PROVIDER = "guandan_marvel_v2"
CHECKPOINT_SCHEMA = "guandan-marvel-evaluation-checkpoint-v1"
PROTOCOL = "GD-ACTION-0.2-MARVEL|GD-ENV-0.2-MARVEL|GD-ENCODING-0.2-MARVEL"
DEPLOYMENT_ARTIFACT_FORMAT = "guance_marvel_v2_deployment_v1"
DEPLOYMENT_SCHEMA = "guance-guandan-marvel-v2-deployment-v2"
ALLOWED_ALGORITHMS = frozenset({"ippo"})
ALLOWED_ARCHITECTURES = frozenset({"v1.3"})
ALLOWED_PAYLOAD_VARIANTS = frozenset({"ddp_merged_frozen_v1", "flat_not_smoothed_v1"})

# Stable family contract. model_version / training_version are per-bundle display
# metadata and are deliberately not pinned here.
CONTRACT_FIELDS = MappingProxyType({
    "provider": PROVIDER, "backend": "guandan", "runtime": "torch",
    "input_contract": "GuandanMARVELV2.Rollout.v1",
    "output_contract": "GuandanMARVELV2.ActionRow.v2",
    "rules_version": "GD-RULES-0.1", "action_version": "GD-ACTION-0.2-MARVEL",
    "environment_version": "GD-ENV-0.2-MARVEL", "encoding_version": "GD-ENCODING-0.2-MARVEL",
    "algorithm": "ippo", "architecture": "v1.3",
})

_FIELD_CODES = MappingProxyType({
    "provider": "PROVIDER_UNKNOWN", "backend": "BACKEND_UNSUPPORTED",
    "runtime": "RUNTIME_UNSUPPORTED", "input_contract": "CONTRACT_UNSUPPORTED",
    "output_contract": "CONTRACT_UNSUPPORTED",
})


_MARVEL_CAPABILITIES = frozenset({
    "recommend", "top_n", "complete_action", "next_token_policy", "confirmed_decision",
})


def validate_contract_capabilities(manifest: ModelManifest) -> None:
    caps = manifest.capabilities
    if (not isinstance(caps, Mapping) or caps.get("next_token_policy") is not True
            or any(value and name not in _MARVEL_CAPABILITIES
                   for name, value in caps.items())):
        raise ModelBackendError("CONTRACT_UNSUPPORTED")
    if caps.get("confirmed_decision") is True and caps.get("complete_action") is not True:
        raise ModelBackendError("CONTRACT_UNSUPPORTED")


def validate_contract_fields(manifest: ModelManifest) -> None:
    """Stable family contract only; never pins iteration or artifact identity."""
    ModelManifest.from_dict(manifest.to_dict())
    for name, expected in CONTRACT_FIELDS.items():
        if getattr(manifest, name) != expected:
            raise ModelBackendError(_FIELD_CODES.get(name, "MODEL_VERSION_UNSUPPORTED"))


def validate_provider_manifest(manifest: ModelManifest) -> None:
    """Validate the outer deployment-bundle manifest (contract, not identity)."""
    validate_contract_fields(manifest)
    if manifest.artifact_format != DEPLOYMENT_ARTIFACT_FORMAT:
        raise ModelBackendError("MODEL_VERSION_UNSUPPORTED")
    validate_contract_capabilities(manifest)


def checkpoint_manifest(filename: str, sha256: str, *, model_version: str,
                        training_version: str) -> ModelManifest:
    """Schema-1 descriptor for the checkpoint artifact named by a deployment descriptor."""
    return ModelManifest.from_dict({
        "schema_version": 1, "protocol_version": 1, "artifact": filename,
        **CONTRACT_FIELDS, "model_version": model_version, "training_version": training_version,
        "artifact_format": "torch_checkpoint", "artifact_sha256": sha256,
        "capabilities": {"recommend": True, "next_token_policy": True},
    })


@dataclass(frozen=True, slots=True)
class CheckpointIdentity:
    """Infrastructure-private projection. Never serialize the loaded payload."""

    status: str = "validated"
    provider: str = PROVIDER
    checkpoint_sha256: str = ""
    checkpoint_schema: str = CHECKPOINT_SCHEMA
    algorithm: str = ""
    architecture: str = ""
    iteration: int = 0
    classification: str = "evaluation_only"
    resume_classification: str = "evaluation_only"
    protocol_version: str = PROTOCOL
    resolved_config_hash: str = ""
    source_hash: str = ""
    source_fingerprint_hash: str = ""
    source_parent_commit: str = ""
    training_backend_identity_hash: str = ""
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
    model_version: str = ""
    training_version: str = ""
    deployment_fingerprint: str = ""

    def to_model_metadata(self) -> ModelMetadata:
        """Project only fixed public fields, never private identity/payload blobs."""
        status = {
            "validated": "experimental_artifact_validated",
            "demo_loaded": "experimental_demo_loaded",
        }.get(self.status, "experimental_unavailable")
        return ModelMetadata(
            status=status, provider=PROVIDER, backend=CONTRACT_FIELDS["backend"],
            runtime="torch-cpu", model_version=self.model_version or "unknown",
            model_sha256=self.checkpoint_sha256, legacy=False, test_only=True,
            score_kind="marvel_v2_row_probability",
            input_contract=CONTRACT_FIELDS["input_contract"],
            output_contract=CONTRACT_FIELDS["output_contract"],
            wire_input_contract="", wire_output_contract="",
            capabilities=ModelCapabilities(next_token_policy=True),
            deployment_fingerprint=self.deployment_fingerprint)
