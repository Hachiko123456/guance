"""Explicit experimental MARVEL V2 provider; never a fallback provider."""
from __future__ import annotations

from collections.abc import Mapping

from guance.contracts import (
    AdviceCandidate, CanonicalAdviceResult, CanonicalPublicGameView,
    CONFIRMED_DECISION_REQUEST_CONTRACT, ModelCapabilities, ModelMetadata,
)
from ..device_policy import (
    DEVICE_RUNTIME_CONTRACT, LEGACY_CPU_RUNTIME_CONTRACT, RUNTIME_CONTRACTS,
    normalize_requested_device, select_device,
)
from ..errors import ModelBackendError
from ..factory import AbstractProviderFactory
from ..manifest import ModelBundle, ModelManifest
from ..marvel_v2.identity import (
    CONTRACT_FIELDS, DEPLOYMENT_ARTIFACT_FORMAT, validate_contract_capabilities,
    validate_contract_fields, validate_provider_manifest,
)

# The legacy CPU contract keeps its original CPU-only meaning. The device
# contract selects GPU first with an explicit, observable CPU fallback policy.
MARVEL_V2_RUNTIME_CONTRACT = LEGACY_CPU_RUNTIME_CONTRACT
MARVEL_V2_RUNTIME_CONTRACTS = RUNTIME_CONTRACTS
MARVEL_V2_DEVICE_RUNTIME_CONTRACT = DEVICE_RUNTIME_CONTRACT
SCORE_KIND = "marvel_v2_policy_path_score"
# Discovery claim contract: without a verified deployment/evaluation identity the
# MARVEL V2 family is evaluation-only. Any production/strength claim fails closed;
# promoting it would require a new provider contract and code change, never a
# manifest self-report.
MARVEL_V2_ALLOWED_CLASSIFICATION = "evaluation_only"
MARVEL_V2_EXPECTED_READINESS = {"experimental": True, "production_ready": False, "strength_claim": False}


def validate_marvel_v2_claims(manifest: ModelManifest) -> None:
    if manifest.classification is None and manifest.readiness is None:
        return  # schema 1 fixed descriptors carry no claim fields
    if (manifest.classification != MARVEL_V2_ALLOWED_CLASSIFICATION
            or manifest.readiness is None
            or dict(manifest.readiness) != MARVEL_V2_EXPECTED_READINESS):
        raise ModelBackendError("CLAIM_UNSUPPORTED")


# model_version/training_version are display metadata; identity lives in the bundle.
_DISCOVERY_FIELDS = tuple(CONTRACT_FIELDS)


class UnavailableMarvelV2Advisor:
    """Compatibility descriptor boundary; direct checkpoint descriptors never wire production."""

    def __init__(self, bundle: ModelBundle) -> None:
        self.bundle = bundle
        self._closed = False

    def load(self) -> ModelMetadata:
        if self._closed:
            raise ModelBackendError("MODEL_CLOSED")
        validate_contract_fields(self.bundle.manifest)
        self.bundle.verify()
        raise ModelBackendError("MODEL_UNAVAILABLE")

    def recommend(self, view: CanonicalPublicGameView, request_id: str) -> CanonicalAdviceResult:
        raise ModelBackendError("MODEL_CLOSED" if self._closed else "MODEL_UNAVAILABLE")

    def recommend_confirmed(self, decision: Mapping, request_id: str) -> CanonicalAdviceResult:
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
    """Single-owner real runtime loaded only inside the dedicated worker."""

    def __init__(self, *, requested_device: str = "auto") -> None:
        self._requested_device = normalize_requested_device(requested_device)
        self._runtime = None
        self._resolved = None
        self._closed = False
        self._confirmed_capable = False
        self._model_version = ""
        self._selection = None
        self._device_evidence: dict[str, object] = {}
        self._contract = MARVEL_V2_RUNTIME_CONTRACT

    @property
    def device_selection(self):
        return self._selection

    @property
    def device_evidence(self) -> dict[str, object]:
        return dict(self._device_evidence)

    def load(self, bundle: ModelBundle) -> ModelMetadata:
        if self._closed:
            raise ModelBackendError("MODEL_CLOSED")
        from ..marvel_v2.deployment_bundle import load_descriptor, resolve_environment
        if self._runtime is not None:
            self.close()
            self._closed = False
        self._selection = None
        self._device_evidence = {}
        descriptor = load_descriptor(bundle)
        resolved = resolve_environment(descriptor)
        contract = bundle.manifest.runtime_contract or MARVEL_V2_RUNTIME_CONTRACT
        selection = None
        runtime = None
        try:
            import importlib
            torch = importlib.import_module("torch")
            selection = select_device(self._requested_device, torch_module=torch,
                                      runtime_contract=contract)
            from ..marvel_v2.runtime import MarvelV2DemoRuntime
            runtime = MarvelV2DemoRuntime(device=selection.actual_device)
            runtime.load(resolved.checkpoint_bundle, resolved.descriptor.checkpoint_expectation)
            identity = runtime.mark_experimental_deployment_verified()
            evidence = runtime.device_evidence()
            if (identity.production_ready or identity.strength_claim or identity.test_only
                    or not identity.deployment_identity_verified
                    or identity.classification != "evaluation_only"):
                raise ModelBackendError("MODEL_VERSION_UNSUPPORTED")
        except BaseException as exc:
            if runtime is not None:
                runtime.close()
            resolved.bootstrap.close()
            if selection is not None and not hasattr(exc, "device_selection"):
                try:
                    exc.device_selection = selection
                except Exception:
                    pass
            raise
        self._runtime, self._resolved = runtime, resolved
        self._selection, self._device_evidence = selection, evidence
        self._contract = contract
        manifest = bundle.manifest
        raw_capabilities = (manifest.capabilities
                            if isinstance(manifest.capabilities, Mapping) else {})
        confirmed_capable = (raw_capabilities.get("confirmed_decision") is True
                             and raw_capabilities.get("complete_action") is True)
        self._confirmed_capable = confirmed_capable
        self._model_version = manifest.model_version
        iteration = resolved.descriptor.checkpoint_expectation.iteration
        evidence_text = (
            f"params.{evidence['parameter_device'] or 'none'}.{evidence['parameter_count']}"
            f".buffers.{evidence['buffer_device'] or 'none'}.{evidence['buffer_count']}"
            f".cache.{evidence['cache_device'] or 'none'}")
        return ModelMetadata(
            status="ready" if confirmed_capable else "unavailable",
            provider="guandan_marvel_v2", backend="guandan",
            runtime="torch-cuda" if selection.actual_cuda else "torch-cpu",
            runtime_version=str(resolved.descriptor.runtime["torch_version"]),
            runtime_contract=(manifest.runtime_contract or MARVEL_V2_RUNTIME_CONTRACT),
            model_version=manifest.model_version,
            iteration=iteration if type(iteration) is int and iteration > 0 else 0,
            model_sha256=identity.checkpoint_sha256,
            bundle_fingerprint=resolved.descriptor.bundle_fingerprint,
            deployment_fingerprint=resolved.deployment_identity.identity_sha256,
            score_kind=SCORE_KIND, classification=identity.classification,
            formal_evaluation_status="not_run", champion_status="not_established",
            input_contract=CONTRACT_FIELDS["input_contract"],
            output_contract=CONTRACT_FIELDS["output_contract"],
            wire_input_contract=(CONFIRMED_DECISION_REQUEST_CONTRACT if confirmed_capable
                                 else "CanonicalPublicGameView.v1"),
            wire_output_contract="CanonicalAdviceResult.v1",
            legacy=False, test_only=False, experimental=True, production_ready=False,
            strength_claim=False, public_self_supported=False,
            requested_device=selection.requested_device,
            actual_device=selection.actual_device,
            device_fallback=selection.fallback,
            device_reason=selection.reason,
            torch_cuda_build=selection.torch_cuda_build,
            device_evidence=evidence_text,
            capabilities=ModelCapabilities.from_mapping(
                raw_capabilities if isinstance(raw_capabilities, Mapping) else None),
        )

    def recommend_confirmed(self, payload: Mapping, request_id: str) -> CanonicalAdviceResult:
        from ..marvel_v2.decision_request import parse_confirmed_decision
        from ..marvel_v2.recommendation import run_confirmed_recommendation
        if self._closed:
            raise ModelBackendError("MODEL_CLOSED")
        if self._runtime is None or self._resolved is None:
            raise ModelBackendError("MODEL_NOT_LOADED")
        if not self._confirmed_capable:
            raise ModelBackendError("REQUEST_KIND_UNSUPPORTED")
        parsed = parse_confirmed_decision(payload)
        if (parsed.artifact_id != self._model_version
                or parsed.artifact_sha256 != self._runtime.identity.checkpoint_sha256):
            raise ModelBackendError("CONFIRMED_DECISION_UNAVAILABLE")
        outcome = run_confirmed_recommendation(
            self._runtime, parsed, rules_root=self._resolved.source_root,
            timeout_seconds=None)
        candidate = AdviceCandidate(
            cards=outcome.cards, kind=outcome.kind, label=outcome.label,
            score=outcome.path_score, claim=outcome.declared_ranks,
            score_kind=SCORE_KIND, metadata={}, rank=1,
            claim_kind="declaration" if outcome.family else "")
        return CanonicalAdviceResult(
            session_id=parsed.session_id, game_id=parsed.game_id,
            state_version=parsed.state_version, request_id=request_id,
            candidates=(candidate,), elapsed_ms=outcome.elapsed_ms, status="ready",
            model_fingerprint=parsed.artifact_sha256, score_kind=SCORE_KIND,
            provider="guandan_marvel_v2", model_version=self._model_version)

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

    def recommend_confirmed(self, decision: Mapping, request_id: str) -> CanonicalAdviceResult:
        if self._closed:
            raise ModelBackendError("MODEL_CLOSED")
        if not self._loaded:
            raise ModelBackendError("MODEL_NOT_LOADED")
        return self.runtime.recommend_confirmed(decision, request_id)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._loaded = False
        self.runtime.close()


class GuandanMarvelV2Provider(AbstractProviderFactory):
    def validate(self, manifest: ModelManifest) -> None:
        if manifest.artifact_format == "torch_checkpoint":
            validate_contract_fields(manifest)
            validate_contract_capabilities(manifest)
        else:
            validate_provider_manifest(manifest)
        if (manifest.runtime_contract is not None
                and manifest.runtime_contract not in MARVEL_V2_RUNTIME_CONTRACTS):
            raise ModelBackendError("RUNTIME_UNSUPPORTED")
        validate_marvel_v2_claims(manifest)

    def validate_discovery(self, manifest: ModelManifest) -> None:
        """Stable family contract only; iteration/artifact identity belongs to the bundle."""
        ModelManifest.from_dict(manifest.to_dict())
        validate_marvel_v2_claims(manifest)
        for name in _DISCOVERY_FIELDS:
            if getattr(manifest, name) != CONTRACT_FIELDS[name]:
                code = {
                    "provider": "PROVIDER_UNKNOWN", "backend": "BACKEND_UNSUPPORTED",
                    "runtime": "RUNTIME_UNSUPPORTED", "input_contract": "CONTRACT_UNSUPPORTED",
                    "output_contract": "CONTRACT_UNSUPPORTED",
                }.get(name, "MODEL_VERSION_UNSUPPORTED")
                raise ModelBackendError(code)
        if manifest.artifact_format not in ("torch_checkpoint", DEPLOYMENT_ARTIFACT_FORMAT):
            raise ModelBackendError("MODEL_VERSION_UNSUPPORTED")
        caps = manifest.capabilities
        if (not isinstance(caps, Mapping) or caps.get("next_token_policy") is not True
                or any(value and name not in {"recommend", "next_token_policy",
                                              "complete_action", "confirmed_decision"}
                       for name, value in caps.items())):
            raise ModelBackendError("CONTRACT_UNSUPPORTED")
        if caps.get("confirmed_decision") is True and caps.get("complete_action") is not True:
            raise ModelBackendError("CONTRACT_UNSUPPORTED")
        if manifest.runtime_contract not in MARVEL_V2_RUNTIME_CONTRACTS:
            raise ModelBackendError("RUNTIME_UNSUPPORTED")

    def create_observation_adapter(self) -> MarvelV2ObservationAdapter:
        return MarvelV2ObservationAdapter()

    def create_runtime(self, bundle: ModelBundle, *,
                       requested_device: str = "auto") -> MarvelV2ExperimentalRuntime:
        self.validate(bundle.manifest)
        if bundle.manifest.artifact_format == "torch_checkpoint":
            raise ModelBackendError("MODEL_UNAVAILABLE")
        return MarvelV2ExperimentalRuntime(requested_device=requested_device)

    def create(self, bundle: ModelBundle, *, requested_device: str = "auto",
               ) -> GuandanMarvelV2Advisor | UnavailableMarvelV2Advisor:
        self.validate(bundle.manifest)
        if bundle.manifest.artifact_format == "torch_checkpoint":
            return UnavailableMarvelV2Advisor(bundle)
        return GuandanMarvelV2Advisor(
            bundle, self.create_runtime(bundle, requested_device=requested_device),
            self.create_observation_adapter())


__all__ = [
    "GuandanMarvelV2Advisor", "GuandanMarvelV2Provider", "MARVEL_V2_DEVICE_RUNTIME_CONTRACT",
    "MARVEL_V2_RUNTIME_CONTRACT", "MARVEL_V2_RUNTIME_CONTRACTS", "MarvelV2ExperimentalRuntime",
    "MarvelV2ObservationAdapter", "UnavailableMarvelV2Advisor",
]
