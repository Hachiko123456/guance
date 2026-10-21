"""FableDan anti-corruption layer; legacy advisor and ranking algorithm are unchanged."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from guance.contracts import (
    ModelMetadata,
    ADVICE_CONTRACT, ADVICE_CONTRACT_ALIASES, PUBLIC_GAME_CONTRACT, PUBLIC_GAME_CONTRACT_ALIASES,
    CanonicalAdviceResult, CanonicalPublicGameView, GameObservation, PUBLIC_RULESET,
    validate_advice_for_view,
)
from ..errors import ModelBackendError
from ..factory import AbstractProviderFactory
from ..manifest import ModelBundle, ModelManifest

FABLEDAN_RUNTIME_CONTRACT = "fabledan.numpy.v1"
# Discovery claim contract: a FableDan-family bundle may not self-report
# production/strength claims without an evaluated identity; only the legacy
# vendor model and explicitly evaluation-only packages are acceptable today.
FABLEDAN_ALLOWED_CLASSIFICATIONS = frozenset({"legacy", "experimental", "evaluation_only"})


def validate_fabledan_claims(manifest: ModelManifest) -> None:
    classification = manifest.classification
    readiness = manifest.readiness
    if classification is None and readiness is None:
        return  # schema 1 direct selection carries no claim fields
    if classification not in FABLEDAN_ALLOWED_CLASSIFICATIONS or readiness is None:
        raise ModelBackendError("CLAIM_UNSUPPORTED")
    expected_experimental = classification != "legacy"
    if (readiness.get("production_ready") is not False
            or readiness.get("strength_claim") is not False
            or readiness.get("experimental") is not expected_experimental):
        raise ModelBackendError("CLAIM_UNSUPPORTED")


class FableDanObservationAdapter:
    def adapt(self, view: CanonicalPublicGameView) -> GameObservation:
        observation = view.to_observation()
        # Combination/wildcard resolution belongs to the provider, not the canonical DTO.
        from ..advisor import ModelError, build_model_observation
        try:
            build_model_observation(observation)
        except ModelError:
            raise ModelBackendError("MODEL_INPUT_AMBIGUOUS") from None
        return observation


class FableDanModelAdapter:
    def __init__(self, bundle: ModelBundle, runtime: Any,
                 observation_adapter: FableDanObservationAdapter) -> None:
        self.bundle = bundle
        self.runtime = runtime
        self.observation_adapter = observation_adapter
        self._loaded = False
        self._closed = False

    def load(self) -> ModelMetadata:
        if self._closed:
            raise ModelBackendError("MODEL_CLOSED")
        if not self._loaded:
            self.bundle.verify()
            try:
                self.runtime.load()
                # The advisor computes its own digest; reject replacement between validation/load.
                if self.runtime.fingerprint != self.bundle.manifest.artifact_sha256:
                    raise ModelBackendError("ARTIFACT_HASH_MISMATCH")
                self.bundle.verify()
            except ModelBackendError:
                raise
            except Exception:
                raise ModelBackendError("MODEL_LOAD_FAILED") from None
            self._loaded = True
        manifest = self.bundle.manifest
        return ModelMetadata(status="loaded", provider=manifest.provider, backend=manifest.backend,
                runtime=manifest.runtime, runtime_version="",
                runtime_contract=(manifest.runtime_contract or FABLEDAN_RUNTIME_CONTRACT),
                model_version=manifest.model_version, iteration=0,
                model_sha256=manifest.artifact_sha256,
                bundle_fingerprint=manifest.artifact_sha256, legacy=self.bundle.legacy,
                score_kind="fabledan_q", test_only=False,
                input_contract=manifest.input_contract, output_contract=manifest.output_contract,
                wire_input_contract=PUBLIC_GAME_CONTRACT, wire_output_contract=ADVICE_CONTRACT,
                capabilities=manifest.capabilities if isinstance(manifest.capabilities, Mapping) else {})

    def recommend(self, view: CanonicalPublicGameView, request_id: str) -> CanonicalAdviceResult:
        if not self._loaded or self._closed:
            raise ModelBackendError("MODEL_NOT_LOADED")
        observation = self.observation_adapter.adapt(view)
        result = self.runtime.recommend(observation, request_id)
        if result.status != "ready":
            raise ModelBackendError("MODEL_INPUT_OR_INFERENCE")
        canonical = CanonicalAdviceResult.from_legacy(
            result, provider="fabledan", model_version=self.bundle.manifest.model_version,
            score_kind="fabledan_q")
        validate_advice_for_view(view, canonical)
        return canonical

    def recommend_confirmed(self, decision, request_id: str) -> CanonicalAdviceResult:
        # The FableDan public-view family never consumes confirmed-decision DTOs.
        raise ModelBackendError("REQUEST_KIND_UNSUPPORTED")

    def close(self) -> None:
        self._closed = True
        self._loaded = False
        self.runtime.model = None


class FableDanProvider(AbstractProviderFactory):
    def validate(self, manifest: ModelManifest) -> None:
        if manifest.backend != "numpy":
            raise ModelBackendError("BACKEND_UNSUPPORTED")
        if manifest.runtime != "numpy":
            raise ModelBackendError("RUNTIME_UNSUPPORTED")
        if (manifest.input_contract not in PUBLIC_GAME_CONTRACT_ALIASES
                or manifest.output_contract not in ADVICE_CONTRACT_ALIASES):
            raise ModelBackendError("CONTRACT_UNSUPPORTED")
        if (manifest.runtime_contract is not None
                and manifest.runtime_contract != FABLEDAN_RUNTIME_CONTRACT):
            raise ModelBackendError("RUNTIME_UNSUPPORTED")
        validate_fabledan_claims(manifest)
        expected = {"artifact_format": "npz", "rules_version": PUBLIC_RULESET,
                    "action_version": "public-action.v1", "environment_version": "public-play.v1",
                    "encoding_version": "fabledan.v1", "algorithm": "fabledan-q",
                    "architecture": "FableDanNet"}
        if any(getattr(manifest, key) != value for key, value in expected.items()):
            raise ModelBackendError("MODEL_VERSION_UNSUPPORTED")

    def create_observation_adapter(self) -> FableDanObservationAdapter:
        return FableDanObservationAdapter()

    def create_runtime(self, bundle: ModelBundle, *,
                       requested_device: str = "auto") -> Any:
        from ..advisor import FableDanAdvisor
        return FableDanAdvisor(bundle.artifact_path)

    def create(self, bundle: ModelBundle, *,
               requested_device: str = "auto") -> FableDanModelAdapter:
        self.validate(bundle.manifest)
        return FableDanModelAdapter(bundle, self.create_runtime(bundle), self.create_observation_adapter())
