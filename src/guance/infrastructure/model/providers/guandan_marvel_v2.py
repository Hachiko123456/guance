"""Explicit experimental MARVEL V2 provider; never a fallback provider."""
from __future__ import annotations

from guance.contracts import CanonicalAdviceResult, CanonicalPublicGameView, ModelCapabilities, ModelMetadata
from ..errors import ModelBackendError
from ..factory import AbstractProviderFactory
from ..manifest import ModelBundle, ModelManifest
from ..marvel_v2.identity import (
    CHECKPOINT_SHA256, DESCRIPTOR_FIELDS, validate_descriptor, validate_provider_manifest,
)


class UnavailableMarvelV2Advisor:
    """Compatibility descriptor boundary; direct checkpoint descriptors never wire production."""

    def __init__(self, bundle: ModelBundle) -> None:
        self.bundle = bundle
        self._closed = False

    def load(self) -> ModelMetadata:
        if self._closed:
            raise ModelBackendError("MODEL_CLOSED")
        validate_descriptor(self.bundle.manifest)
        self.bundle.verify()
        raise ModelBackendError("MODEL_UNAVAILABLE")

    def recommend(self, view: CanonicalPublicGameView, request_id: str) -> CanonicalAdviceResult:
        raise ModelBackendError("MODEL_CLOSED" if self._closed else "MODEL_UNAVAILABLE")

    def close(self) -> None:
        self._closed = True


class MarvelV2ObservationAdapter:
    def adapt_confirmed(self, value: object) -> object:
        from ..marvel_v2.live_adapter import MarvelV2ConfirmedDecisionAdapter
        try:
            return MarvelV2ConfirmedDecisionAdapter().adapt(value)
        except Exception:
            raise ModelBackendError("GUANDAN_STATE_UNAVAILABLE") from None

    def adapt(self, view: CanonicalPublicGameView) -> object:
        # Import only after explicit provider selection. Current canonical live state
        # deliberately cannot prove exact four-observer history/private prefixes.
        from ..marvel_v2.encoder import adapt_live_state
        try:
            return adapt_live_state(view)
        except Exception:
            raise ModelBackendError("GUANDAN_STATE_UNAVAILABLE") from None


class MarvelV2ExperimentalRuntime:
    """Single-owner real CPU runtime loaded only inside the dedicated worker."""

    def __init__(self) -> None:
        self._runtime = None
        self._resolved = None
        self._closed = False

    def load(self, bundle: ModelBundle) -> ModelMetadata:
        if self._closed:
            raise ModelBackendError("MODEL_CLOSED")
        from ..marvel_v2.deployment_bundle import load_descriptor, resolve_environment
        if self._runtime is not None:
            self.close()
            self._closed = False
        descriptor = load_descriptor(bundle)
        resolved = resolve_environment(descriptor)
        from ..marvel_v2.runtime import MarvelV2DemoRuntime
        runtime = MarvelV2DemoRuntime()
        try:
            runtime.load(resolved.checkpoint_bundle)
            identity = runtime.mark_experimental_deployment_verified()
        except Exception:
            runtime.close()
            resolved.bootstrap.close()
            raise
        if (identity.production_ready or identity.strength_claim or identity.test_only
                or not identity.deployment_identity_verified
                or identity.classification != "evaluation_only"):
            runtime.close()
            resolved.bootstrap.close()
            raise ModelBackendError("MODEL_VERSION_UNSUPPORTED")
        self._runtime, self._resolved = runtime, resolved
        return ModelMetadata(
            status="unavailable", provider="guandan_marvel_v2", backend="guandan",
            runtime="torch-cpu", runtime_version=str(resolved.descriptor.runtime["torch_version"]),
            model_version=DESCRIPTOR_FIELDS["model_version"], model_sha256=CHECKPOINT_SHA256,
            bundle_fingerprint=resolved.descriptor.bundle_fingerprint,
            deployment_fingerprint=resolved.deployment_identity.identity_sha256,
            score_kind="marvel_v2_policy_path_score", classification="evaluation_only",
            formal_evaluation_status="not_run", champion_status="not_established",
            input_contract=DESCRIPTOR_FIELDS["input_contract"],
            output_contract=DESCRIPTOR_FIELDS["output_contract"],
            wire_input_contract="CanonicalPublicGameView.v1", wire_output_contract="",
            legacy=False, test_only=False, experimental=True, production_ready=False,
            strength_claim=False, public_self_supported=False,
            capabilities=ModelCapabilities(next_token_policy=True),
        )

    def close(self) -> None:
        runtime, self._runtime = self._runtime, None
        resolved, self._resolved = self._resolved, None
        if runtime is not None:
            runtime.close()
        if resolved is not None:
            resolved.bootstrap.close()
        self._closed = True


class GuandanMarvelV2Advisor:
    def __init__(self, bundle: ModelBundle, runtime: MarvelV2ExperimentalRuntime,
                 adapter: MarvelV2ObservationAdapter) -> None:
        self.bundle, self.runtime, self.adapter = bundle, runtime, adapter
        self._loaded = False
        self._closed = False

    def load(self) -> ModelMetadata:
        if self._closed:
            raise ModelBackendError("MODEL_CLOSED")
        self._loaded = False
        metadata = self.runtime.load(self.bundle)
        self._loaded = True
        return metadata

    def recommend(self, view: CanonicalPublicGameView, request_id: str) -> CanonicalAdviceResult:
        if self._closed:
            raise ModelBackendError("MODEL_CLOSED")
        if not self._loaded:
            raise ModelBackendError("MODEL_NOT_LOADED")
        self.adapter.adapt(view)
        # No current live state reaches this point. Do not synthesize rows, execute
        # actions, or reinterpret evaluation-only next-token scores as advice.
        raise ModelBackendError("GUANDAN_STATE_UNAVAILABLE")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._loaded = False
        self.runtime.close()


class GuandanMarvelV2Provider(AbstractProviderFactory):
    def validate(self, manifest: ModelManifest) -> None:
        if manifest.artifact_format == "torch_checkpoint":
            validate_descriptor(manifest)
        else:
            validate_provider_manifest(manifest)

    def create_observation_adapter(self) -> MarvelV2ObservationAdapter:
        return MarvelV2ObservationAdapter()

    def create_runtime(self, bundle: ModelBundle) -> MarvelV2ExperimentalRuntime:
        self.validate(bundle.manifest)
        if bundle.manifest.artifact_format == "torch_checkpoint":
            raise ModelBackendError("MODEL_UNAVAILABLE")
        return MarvelV2ExperimentalRuntime()

    def create(self, bundle: ModelBundle) -> GuandanMarvelV2Advisor | UnavailableMarvelV2Advisor:
        self.validate(bundle.manifest)
        if bundle.manifest.artifact_format == "torch_checkpoint":
            return UnavailableMarvelV2Advisor(bundle)
        return GuandanMarvelV2Advisor(bundle, self.create_runtime(bundle),
                                      self.create_observation_adapter())


__all__ = [
    "GuandanMarvelV2Advisor", "GuandanMarvelV2Provider", "MarvelV2ExperimentalRuntime",
    "MarvelV2ObservationAdapter", "UnavailableMarvelV2Advisor",
]
