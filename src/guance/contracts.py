"""Versioned, Qt-free contracts shared by core, model, runtime and UI adapters."""
from __future__ import annotations
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Literal
import math

MANIFEST_SCHEMA_VERSION = 1
WORKER_PROTOCOL_VERSION = 3
CANONICAL_PUBLIC_STATE_SCHEMA_VERSION = 1
CANONICAL_ADVICE_RESULT_SCHEMA_VERSION = 1
# Explicit recommendation request kinds. The request kind is always carried as a
# tag; providers are never guessed from field presence.
PUBLIC_RECOMMENDATION_KIND = "public"
CONFIRMED_DECISION_RECOMMENDATION_KIND = "confirmed_decision"
PUBLIC_RECOMMENDATION_CONTRACT = "CanonicalPublicGameView.v1"
CONFIRMED_DECISION_REQUEST_CONTRACT = "GuandanMARVELV2.ConfirmedDecision.v1"
CONFIRMED_DECISION_REQUEST_FIELDS = frozenset({
    "schema", "capture_id", "frame_sequence", "session_id", "game_id", "state_version",
    "model", "marvel_state", "current_winning_declaration", "session", "history",
    "cache_request", "decision_identity_sha256",
})
# Legacy alias retained for older capture/model imports. It names the public-state schema only.
SCHEMA_VERSION = CANONICAL_PUBLIC_STATE_SCHEMA_VERSION

_METADATA_KEYS = frozenset({"provider", "model_version", "action_version", "strategy", "test_only",
                            "backend", "runtime", "model_sha256", "legacy",
                            "score_kind", "input_contract", "output_contract",
                            "wire_input_contract", "wire_output_contract"})

class ModelLifecycleState(str, Enum):
    UNSELECTED = "unselected"
    UNAVAILABLE = "unavailable"
    UNINITIALIZED = "uninitialized"
    LOADING = "loading"
    READY = "ready"
    ERROR = "error"
    TIMEOUT = "timeout"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class ModelCapabilities(Mapping[str, bool]):
    recommend: bool = False
    top_n: bool = False
    complete_action: bool = False
    next_token_policy: bool = False
    confirmed_decision: bool = False
    beam_search: bool = False
    belief: bool = False
    search: bool = False
    public_plus_self: bool = False
    extras: Mapping[str, bool] = field(default_factory=dict, repr=False, compare=True)

    _KNOWN = ("recommend", "top_n", "complete_action", "next_token_policy",
              "confirmed_decision", "beam_search", "belief", "search", "public_plus_self")

    def __post_init__(self) -> None:
        values = {
            "recommend": self.recommend, "top_n": self.top_n,
            "complete_action": self.complete_action,
            "next_token_policy": self.next_token_policy,
            "confirmed_decision": self.confirmed_decision,
            "beam_search": self.beam_search, "belief": self.belief,
            "search": self.search, "public_plus_self": self.public_plus_self,
        }
        if any(type(value) is not bool for value in values.values()):
            raise ValueError("invalid model capabilities")
        extras = dict(self.extras)
        if any(not isinstance(key, str) or not _identity(key) or type(value) is not bool
               for key, value in extras.items()):
            raise ValueError("invalid model capabilities")
        object.__setattr__(self, "extras", MappingProxyType(extras))

    @classmethod
    def from_mapping(cls, value: Mapping[str, bool] | None) -> "ModelCapabilities":
        if value is None:
            return cls()
        known = {name: bool(value.get(name, False)) for name in cls._KNOWN}
        extras = {key: item for key, item in value.items() if key not in known}
        return cls(**known, extras=extras)

    def __getitem__(self, key: str) -> bool:
        if key in self._KNOWN:
            return bool(getattr(self, key))
        return self.extras[key]

    def __iter__(self) -> Iterator[str]:
        yield from self._KNOWN
        yield from self.extras

    def __len__(self) -> int:
        return len(self._KNOWN) + len(self.extras)

    def to_dict(self) -> dict[str, bool]:
        return {key: self[key] for key in self}


@dataclass(frozen=True, slots=True)
class ModelMetadata(Mapping[str, Any]):
    status: str = "loaded"
    provider: str = ""
    backend: str = ""
    runtime: str = ""
    runtime_version: str = ""
    runtime_contract: str = ""
    model_version: str = ""
    iteration: int = 0
    model_sha256: str = ""
    bundle_fingerprint: str = ""
    deployment_fingerprint: str = ""
    score_kind: str = ""
    classification: str = ""
    formal_evaluation_status: str = ""
    champion_status: str = ""
    input_contract: str = ""
    output_contract: str = ""
    wire_input_contract: str = ""
    wire_output_contract: str = ""
    legacy: bool = False
    test_only: bool = False
    experimental: bool | None = None
    production_ready: bool | None = None
    strength_claim: bool | None = None
    public_self_supported: bool | None = None
    requested_device: str = ""
    actual_device: str = ""
    device_fallback: bool | None = None
    device_reason: str = ""
    torch_cuda_build: str = ""
    device_evidence: str = ""
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)
    extras: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=True)

    def __post_init__(self) -> None:
        if not isinstance(self.capabilities, ModelCapabilities):
            object.__setattr__(self, "capabilities", ModelCapabilities.from_mapping(self.capabilities))
        for name in ("legacy", "test_only"):
            if type(getattr(self, name)) is not bool:
                raise ModelContractError("MODEL_OUTPUT_INVALID")
        for name in ("experimental", "production_ready", "strength_claim", "public_self_supported"):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise ModelContractError("MODEL_OUTPUT_INVALID")
        for name in ("model_sha256", "bundle_fingerprint", "deployment_fingerprint"):
            value = getattr(self, name)
            if value and (len(value) != 64 or any(c not in "0123456789abcdef" for c in value)):
                raise ModelContractError("MODEL_OUTPUT_INVALID")
        if type(self.iteration) is not int or self.iteration < 0:
            raise ModelContractError("MODEL_OUTPUT_INVALID")
        if self.requested_device not in ("", "auto", "cpu", "cuda"):
            raise ModelContractError("MODEL_OUTPUT_INVALID")
        if self.actual_device not in ("", "cpu", "cuda") and not (
                self.actual_device.startswith("cuda:")
                and self.actual_device[5:].isdigit()):
            raise ModelContractError("MODEL_OUTPUT_INVALID")
        if self.device_fallback is not None and type(self.device_fallback) is not bool:
            raise ModelContractError("MODEL_OUTPUT_INVALID")
        for name in ("device_reason", "torch_cuda_build", "device_evidence"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) > 128 or any(
                    not (char.isalnum() or char in "._:+-") for char in value):
                raise ModelContractError("MODEL_OUTPUT_INVALID")
        object.__setattr__(self, "extras", _freeze_public_mapping(self.extras))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | "ModelMetadata") -> "ModelMetadata":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ModelContractError("MODEL_OUTPUT_INVALID")
        known = {field.name for field in cls.__dataclass_fields__.values()}
        data = {key: item for key, item in value.items() if key in known and key not in {"extras", "capabilities"}}
        data["capabilities"] = ModelCapabilities.from_mapping(value.get("capabilities"))
        data["extras"] = {key: item for key, item in value.items() if key not in known}
        return cls(**data)

    @classmethod
    def from_status_mapping(cls, value: Mapping[str, Any] | "ModelMetadata") -> "ModelMetadata":
        """Build the deliberately smaller metadata projection allowed in status DTOs."""
        if isinstance(value, cls):
            value = value.to_dict()
        if not isinstance(value, Mapping):
            raise ModelContractError("MODEL_OUTPUT_INVALID")
        allowed = {
            "status", "provider", "backend", "runtime", "runtime_version",
            "runtime_contract", "model_version", "iteration", "model_sha256",
            "bundle_fingerprint", "deployment_fingerprint",
            "score_kind", "classification", "formal_evaluation_status", "champion_status",
            "input_contract", "output_contract", "wire_input_contract", "wire_output_contract",
            "legacy", "test_only", "experimental", "production_ready", "strength_claim",
            "public_self_supported", "requested_device", "actual_device",
            "device_fallback", "device_reason", "torch_cuda_build", "device_evidence",
            "capabilities",
        }
        filtered = {key: item for key, item in value.items() if key in allowed}
        filtered["extras"] = {}
        return cls.from_mapping(filtered)

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        yield from self.to_dict()

    def __len__(self) -> int:
        return len(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": self.status, "provider": self.provider, "backend": self.backend,
            "runtime": self.runtime, "runtime_version": self.runtime_version,
            "model_version": self.model_version, "model_sha256": self.model_sha256,
            "bundle_fingerprint": self.bundle_fingerprint,
            "deployment_fingerprint": self.deployment_fingerprint,
            "score_kind": self.score_kind, "classification": self.classification,
            "formal_evaluation_status": self.formal_evaluation_status,
            "champion_status": self.champion_status,
            "input_contract": self.input_contract, "output_contract": self.output_contract,
            "wire_input_contract": self.wire_input_contract,
            "wire_output_contract": self.wire_output_contract, "legacy": self.legacy,
            "test_only": self.test_only, "capabilities": self.capabilities.to_dict(),
        }
        if self.runtime_contract:
            result["runtime_contract"] = self.runtime_contract
        if self.iteration:
            result["iteration"] = self.iteration
        for name in ("experimental", "production_ready", "strength_claim", "public_self_supported"):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        for name in ("requested_device", "actual_device", "device_reason",
                     "torch_cuda_build", "device_evidence"):
            value = getattr(self, name)
            if value:
                result[name] = value
        if self.device_fallback is not None:
            result["device_fallback"] = self.device_fallback
        result.update(self.extras)
        # Keep lifecycle booleans explicit so consumers can distinguish a legacy
        # bundle from a modern one and a test-only runtime from production.
        return {key: value for key, value in result.items()
                if value not in ("", {}) or key in {"legacy", "test_only"}}


@dataclass(frozen=True, slots=True, eq=False)
class ModelStatusSnapshot(Mapping[str, Any]):
    state: ModelLifecycleState = ModelLifecycleState.UNAVAILABLE
    error_code: str = ""
    message: str = ""
    metadata: ModelMetadata = field(default_factory=ModelMetadata)
    epoch: int = 0
    generation: int = 0
    event: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.state, ModelLifecycleState):
            object.__setattr__(self, "state", ModelLifecycleState(str(self.state)))
        # Status snapshots expose only the allow-listed metadata projection; provider
        # diagnostics, payloads, model configs and unknown/private extras are dropped.
        object.__setattr__(self, "metadata", ModelMetadata.from_status_mapping(self.metadata))
        if type(self.epoch) is not int or self.epoch < 0 or type(self.generation) is not int or self.generation < 0:
            raise ValueError("invalid model status identity")

    @classmethod
    def from_wire(cls, value: Mapping[str, Any] | "ModelStatusSnapshot") -> "ModelStatusSnapshot":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ModelContractError("MODEL_OUTPUT_INVALID")
        state = value.get("model_status", value.get("state", ModelLifecycleState.UNAVAILABLE.value))
        metadata = value.get("metadata", {})
        return cls(state=ModelLifecycleState(str(state)), error_code=str(value.get("error_code", "")),
                   message=str(value.get("message", "")), metadata=ModelMetadata.from_status_mapping(metadata),
                   epoch=int(value.get("epoch", 0)), generation=int(value.get("generation", 0)),
                   event=str(value.get("event", "")))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"model_status": self.state.value}
        if self.error_code:
            result["error_code"] = self.error_code
        if self.message:
            result["message"] = self.message
        if self.metadata.to_dict():
            result["metadata"] = self.metadata.to_dict()
        if self.epoch:
            result["epoch"] = self.epoch
        if self.generation:
            result["generation"] = self.generation
        if self.event:
            result["event"] = self.event
        return result

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        yield from self.to_dict()

    def __len__(self) -> int:
        return len(self.to_dict())

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ModelStatusSnapshot):
            return (self.state, self.error_code, self.message, self.metadata, self.epoch, self.generation, self.event) == (
                other.state, other.error_code, other.message, other.metadata, other.epoch, other.generation, other.event)
        if isinstance(other, Mapping):
            return self.to_dict() == dict(other)
        return NotImplemented



@dataclass(frozen=True, slots=True)
class RequestIdentity:
    request_id: str
    session_id: str
    game_id: str
    state_version: int
    epoch: int = 0
    generation: int = 0

    @property
    def key(self) -> tuple[str, str, int]:
        return self.session_id, self.game_id, self.state_version

    def to_dict(self) -> dict[str, Any]:
        return {"request_id": self.request_id, "session_id": self.session_id,
                "game_id": self.game_id, "state_version": self.state_version,
                "epoch": self.epoch, "generation": self.generation}


@dataclass(frozen=True, slots=True)
class PublicRecommendationRequest:
    """Explicit public-view recommendation request (FableDan family)."""

    identity: RequestIdentity
    view: "CanonicalPublicGameView"

    def validate(self) -> None:
        if type(self.identity) is not RequestIdentity:
            raise ModelContractError("MODEL_INPUT_INVALID")
        self.view.validate()


@dataclass(frozen=True, slots=True)
class ConfirmedDecisionRecommendationRequest:
    """Explicit confirmed-decision recommendation request (MARVEL family).

    The decision payload is bounded plain data (no tensors, providers or paths).
    Structural checks happen here; the strict schema/identity parse belongs to the
    provider-side adapter at the infrastructure boundary.
    """

    identity: RequestIdentity
    decision: Mapping[str, Any]

    def validate(self) -> None:
        if type(self.identity) is not RequestIdentity:
            raise ModelContractError("MODEL_INPUT_INVALID")
        if not isinstance(self.decision, Mapping) or set(self.decision) != CONFIRMED_DECISION_REQUEST_FIELDS:
            raise ModelContractError("MODEL_INPUT_UNSUPPORTED")
        if self.decision.get("schema") != CONFIRMED_DECISION_REQUEST_CONTRACT:
            raise ModelContractError("MODEL_INPUT_UNSUPPORTED")
        for name in ("session_id", "game_id"):
            if self.decision.get(name) != getattr(self.identity, name):
                raise ModelContractError("MODEL_INPUT_INVALID")
        if self.decision.get("state_version") != self.identity.state_version:
            raise ModelContractError("MODEL_INPUT_INVALID")


DECLARED_FACTS_SCHEMA = "Guandan.DeclaredFacts.v2"
DECLARED_FACTS_SCHEMA_V1 = "Guandan.DeclaredFacts.v1"
_FACT_STATUSES = ("known", "absent", "unknown", "unsupported")
_DANKS_V3_TRUSTED_ORIGINS = ("synthetic_normal_entry", "live_capture")


class PublicDeclarationProtocol:
    """Structural protocol for the declaration type used in declared facts.

    Avoids a circular import: the concrete PublicDeclaration lives in
    public_state_contract and satisfies this shape.
    """
    family: str
    declared_ranks: tuple[int, ...]
    comparison_rank: int
    suit: str | None
    length: int
    canonical_cards: tuple[int, ...]
    wild_assignments: tuple[Any, ...]
    canonical_action_cards: tuple[int, ...]

    def validate_for_level(self, wire_level: object) -> None: ...
    def to_dict(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class CurrentWinningDeclarationFact:
    """Declaration fact for the current winning action (view.lead_action).

    ``known`` carries the complete PublicDeclaration; ``absent`` marks a free
    lead; ``unknown``/``unsupported`` keep an explicit non-fact with a reason
    code.  Nothing here is ever inferred from zero/False.
    """

    status: str
    declaration: PublicDeclarationProtocol | None = None
    reason: str = ""

    def validate(self) -> None:
        if self.status not in _FACT_STATUSES:
            raise ModelContractError("MODEL_INPUT_INVALID")
        if self.status == "known":
            if self.declaration is None or self.reason:
                raise ModelContractError("MODEL_INPUT_INVALID")
            self.declaration.to_dict()
        elif self.status == "absent":
            if self.declaration is not None or self.reason:
                raise ModelContractError("MODEL_INPUT_INVALID")
        else:
            if self.declaration is not None or not self.reason:
                raise ModelContractError("MODEL_INPUT_INVALID")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        result: dict[str, Any] = {"status": self.status}
        if self.status == "known":
            result["declaration"] = self.declaration.to_dict()
        if self.reason:
            result["reason"] = self.reason
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CurrentWinningDeclarationFact":
        if (not isinstance(data, dict) or "status" not in data
                or set(data) - {"status", "declaration", "reason"}):
            raise ModelContractError("MODEL_INPUT_INVALID")
        from guance.public_state_contract import PublicDeclaration
        declaration = None
        if data.get("declaration") is not None:
            declaration = PublicDeclaration.from_dict(data["declaration"])
        return cls(status=data["status"], declaration=declaration,
                   reason=data.get("reason", ""))


@dataclass(frozen=True, slots=True)
class ActionDeclarationFact:
    """Declaration fact for one public history action (index-aligned).

    Pass actions carry status ``absent``; plays carry ``known`` or an explicit
    ``unknown``/``unsupported``.  ``known`` binds the declaration to the
    action's exact entity cards.
    """

    index: int
    status: str
    declaration: PublicDeclarationProtocol | None = None
    reason: str = ""

    def validate(self) -> None:
        if not _integer(self.index, 1, 10000) or self.status not in _FACT_STATUSES:
            raise ModelContractError("MODEL_INPUT_INVALID")
        if self.status == "known":
            if self.declaration is None or self.reason:
                raise ModelContractError("MODEL_INPUT_INVALID")
            self.declaration.to_dict()
        elif self.status == "absent":
            if self.declaration is not None or self.reason:
                raise ModelContractError("MODEL_INPUT_INVALID")
        else:
            if self.declaration is not None or not self.reason:
                raise ModelContractError("MODEL_INPUT_INVALID")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        result: dict[str, Any] = {"index": self.index, "status": self.status}
        if self.status == "known":
            result["declaration"] = self.declaration.to_dict()
        if self.reason:
            result["reason"] = self.reason
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionDeclarationFact":
        if (not isinstance(data, dict) or "index" not in data or "status" not in data
                or set(data) - {"index", "status", "declaration", "reason"}):
            raise ModelContractError("MODEL_INPUT_INVALID")
        from guance.public_state_contract import PublicDeclaration
        declaration = None
        if data.get("declaration") is not None:
            declaration = PublicDeclaration.from_dict(data["declaration"])
        return cls(index=data["index"], status=data["status"],
                   declaration=declaration, reason=data.get("reason", ""))


@dataclass(frozen=True, slots=True)
class DeclaredFacts:
    """Versioned declaration facts attached to a public recommendation request.

    Schema ``Guandan.DeclaredFacts.v1`` per the phase 1 frozen design: the v1
    view bytes stay unchanged and this additive member carries the confirmed
    current-winning declaration plus optional aligned history facts and the
    history provenance summary.  Unknown/unsupported facts stay explicit and
    never become zero/False.
    """

    schema: str
    current_winning_declaration: CurrentWinningDeclarationFact
    history_declarations: tuple[ActionDeclarationFact, ...] = ()
    history_origin: str = ""
    transport_contiguous: str = ""
    # v2 additions (requirement 2026-11-21): the actual fact values, not just
    # the status codes - known(False) means CONFIRMED discontinuous.
    public_history_complete: str = ""
    history_complete_value: bool | None = None
    transport_value: bool | None = None

    def validate(self) -> None:
        if self.schema not in (DECLARED_FACTS_SCHEMA, DECLARED_FACTS_SCHEMA_V1):
            raise ModelContractError("MODEL_INPUT_UNSUPPORTED")
        self.current_winning_declaration.validate()
        if len(self.history_declarations) > 10000:
            raise ModelContractError("MODEL_INPUT_INVALID")
        for fact in self.history_declarations:
            fact.validate()
        for value in (self.history_origin, self.transport_contiguous,
                      self.public_history_complete):
            if value != "" and (not isinstance(value, str) or len(value) > 80
                                or any(not (char.isalnum() or char in "._:-")
                                       for char in value)):
                raise ModelContractError("MODEL_INPUT_INVALID")
        for value in (self.history_complete_value, self.transport_value):
            if value is not None and type(value) is not bool:
                raise ModelContractError("MODEL_INPUT_INVALID")

    def validate_for_view(self, view: "CanonicalPublicGameView") -> None:
        """Bind the facts to the view: winning declaration to lead_action,
        history facts index-aligned to public_history."""
        self.validate()
        view.validate()
        fact = self.current_winning_declaration
        if fact.status == "known":
            if view.free_lead or view.lead_action is None:
                raise ModelContractError("MODEL_INPUT_INVALID")
            if fact.declaration.canonical_cards != tuple(view.lead_action.cards):
                raise ModelContractError("MODEL_INPUT_INVALID")
        elif fact.status == "absent":
            if not view.free_lead:
                raise ModelContractError("MODEL_INPUT_INVALID")
        if self.history_declarations:
            if len(self.history_declarations) != len(view.public_history):
                raise ModelContractError("MODEL_INPUT_INVALID")
            for item, action in zip(self.history_declarations, view.public_history):
                if item.index != action.index:
                    raise ModelContractError("MODEL_INPUT_INVALID")
                if item.status == "known" and \
                        item.declaration.canonical_cards != tuple(action.cards):
                    raise ModelContractError("MODEL_INPUT_INVALID")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        result: dict[str, Any] = {
            "schema": self.schema,
            "current_winning_declaration": self.current_winning_declaration.to_dict(),
        }
        if self.history_declarations:
            result["history_declarations"] = [item.to_dict()
                                              for item in self.history_declarations]
        if self.history_origin:
            result["history_origin"] = self.history_origin
        if self.transport_contiguous:
            result["transport_contiguous"] = self.transport_contiguous
        if self.public_history_complete:
            result["public_history_complete"] = self.public_history_complete
        if self.history_complete_value is not None:
            result["history_complete_value"] = self.history_complete_value
        if self.transport_value is not None:
            result["transport_value"] = self.transport_value
        return result

    @classmethod
    def from_dict(cls, data: Any) -> "DeclaredFacts":
        if (not isinstance(data, dict) or "schema" not in data
                or "current_winning_declaration" not in data
                or set(data) - {"schema", "current_winning_declaration",
                                "history_declarations", "history_origin",
                                "transport_contiguous", "public_history_complete",
                                "history_complete_value", "transport_value"}):
            raise ModelContractError("MODEL_INPUT_INVALID")
        history = tuple(ActionDeclarationFact.from_dict(item)
                        for item in data.get("history_declarations") or ())
        value_fields = {}
        for name in ("history_complete_value", "transport_value"):
            if data.get(name) is not None:
                value_fields[name] = data[name]
        return cls(schema=data["schema"],
                   current_winning_declaration=CurrentWinningDeclarationFact.from_dict(
                       data["current_winning_declaration"]),
                   history_declarations=history,
                   history_origin=data.get("history_origin", ""),
                   transport_contiguous=data.get("transport_contiguous", ""),
                   public_history_complete=data.get("public_history_complete", ""),
                   **value_fields)


@dataclass(frozen=True, slots=True)
class PublicRecommendationRequestV2:
    """Public recommendation request carrying versioned declaration facts.

    The view bytes and identity semantics are identical to
    ``PublicRecommendationRequest``; providers that do not declare
    ``DeclaredFacts.v1`` support never receive the facts member (legacy
    wire behavior is byte-identical).
    """

    identity: RequestIdentity
    view: "CanonicalPublicGameView"
    declared_facts: DeclaredFacts

    def validate(self) -> None:
        if type(self.identity) is not RequestIdentity:
            raise ModelContractError("MODEL_INPUT_INVALID")
        self.declared_facts.validate_for_view(self.view)


# Backward-compatible name for the public request; the union is explicit and tagged.
RecommendationRequest = PublicRecommendationRequest
ModelRecommendationRequest = (PublicRecommendationRequest |
                              PublicRecommendationRequestV2 |
                              ConfirmedDecisionRecommendationRequest)


def recommendation_request_kind(request: ModelRecommendationRequest) -> str:
    if type(request) in (PublicRecommendationRequest, PublicRecommendationRequestV2):
        return PUBLIC_RECOMMENDATION_KIND
    if type(request) is ConfirmedDecisionRecommendationRequest:
        return CONFIRMED_DECISION_RECOMMENDATION_KIND
    raise ModelContractError("MODEL_INPUT_INVALID")


def _freeze_public_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_public_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_public_value(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)[:128]


def _freeze_public_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise ModelContractError("MODEL_OUTPUT_INVALID")
    return MappingProxyType({str(key): _freeze_public_value(item) for key, item in value.items()})


@dataclass(frozen=True, slots=True)
class SourceFrame:
    host: str
    payload: bytes
    received_at: float
    connection_id: str = ""
    sequence: int = 0
    direction: Literal["client", "server", "unknown"] = "unknown"

@dataclass(frozen=True, slots=True)
class PublicAction:
    index: int
    player: int
    kind: Literal["play", "pass"]
    cards: tuple[int, ...] = ()
    combo_type: int | None = None
    next_player: int | None = None
    trick: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "player": self.player, "kind": self.kind,
                "cards": list(self.cards), "combo_type": self.combo_type,
                "next_player": self.next_player, "trick": self.trick}

@dataclass(frozen=True, slots=True)
class GameObservation:
    session_id: str
    game_id: str
    state_version: int
    captured_at: float
    level: int
    my_seat: int
    first_seat: int
    current_seat: int
    hand: tuple[int, ...]
    remaining: tuple[int, int, int, int]
    history: tuple[PublicAction, ...] = ()
    lead_action: PublicAction | None = None
    free_lead: bool = False
    trick: int = 0

    @property
    def partner_seat(self) -> int:
        return (self.my_seat + 2) % 4

    @property
    def key(self) -> tuple[str, str, int]:
        return self.session_id, self.game_id, self.state_version

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "session_id": self.session_id,
                "game_id": self.game_id, "state_version": self.state_version,
                "captured_at": self.captured_at, "level": self.level,
                "my_seat": self.my_seat, "partner_seat": self.partner_seat,
                "first_seat": self.first_seat, "current_seat": self.current_seat,
                "hand": list(self.hand), "remaining": list(self.remaining),
                "history": [x.to_dict() for x in self.history],
                "lead_action": self.lead_action.to_dict() if self.lead_action else None,
                "free_lead": self.free_lead, "trick": self.trick}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GameObservation:
        def action(x: dict[str, Any]) -> PublicAction:
            return PublicAction(index=int(x["index"]), player=int(x["player"]),
                kind=x["kind"], cards=tuple(x.get("cards", ())),
                combo_type=x.get("combo_type"), next_player=x.get("next_player"),
                trick=int(x.get("trick", 0)))
        return cls(session_id=str(data["session_id"]), game_id=str(data["game_id"]),
            state_version=int(data["state_version"]), captured_at=float(data["captured_at"]),
            level=int(data["level"]), my_seat=int(data["my_seat"]),
            first_seat=int(data["first_seat"]), current_seat=int(data["current_seat"]),
            hand=tuple(data["hand"]), remaining=tuple(data["remaining"]),
            history=tuple(action(x) for x in data.get("history", ())),
            lead_action=action(data["lead_action"]) if data.get("lead_action") else None,
            free_lead=bool(data.get("free_lead")), trick=int(data.get("trick", 0)))

@dataclass(frozen=True, slots=True)
class AdviceCandidate:
    """Provider-neutral recommendation with separate rank, score and claim semantics."""

    cards: tuple[int, ...]
    kind: str
    label: str
    score: float | None = None
    claim: tuple[int, ...] = ()
    score_kind: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    rank: int = 0
    claim_kind: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "cards", tuple(self.cards or ()))
        object.__setattr__(self, "claim", tuple(self.claim or ()))
        object.__setattr__(self, "metadata", _freeze_public_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {"cards": list(self.cards), "kind": self.kind, "label": self.label,
                "score": self.score, "claim": list(self.claim),
                "score_kind": self.score_kind, "metadata": public_model_metadata(self.metadata),
                "rank": self.rank, "claim_kind": self.claim_kind}

@dataclass(frozen=True, slots=True)
class AdviceResult:
    session_id: str
    game_id: str
    state_version: int
    request_id: str
    candidates: tuple[AdviceCandidate, ...] = ()
    elapsed_ms: float = 0.0
    status: str = "ready"
    error_code: str = ""
    error_message: str = ""
    model_fingerprint: str = ""
    score_kind: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates or ()))
        object.__setattr__(self, "metadata", _freeze_public_mapping(self.metadata))

    @property
    def key(self) -> tuple[str, str, int]:
        return self.session_id, self.game_id, self.state_version

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "session_id": self.session_id,
            "game_id": self.game_id, "state_version": self.state_version,
            "request_id": self.request_id, "candidates": [c.to_dict() for c in self.candidates],
            "elapsed_ms": self.elapsed_ms, "status": self.status,
            "error_code": self.error_code, "error_message": self.error_message,
            "model_fingerprint": self.model_fingerprint, "score_kind": self.score_kind,
            "metadata": public_model_metadata(self.metadata)}

@dataclass(frozen=True, slots=True)
class EngineUpdate:
    kind: str
    message: str = ""
    observation: GameObservation | None = None
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""


PUBLIC_GAME_CONTRACT = "CanonicalPublicGameView.v1"
ADVICE_CONTRACT = "CanonicalAdviceResult.v1"
# Explicit transport-schema aliases; provider-specific contracts are checked by their ACL.
PUBLIC_GAME_CONTRACT_ALIASES = frozenset({PUBLIC_GAME_CONTRACT, "canonical-public-v1"})
ADVICE_CONTRACT_ALIASES = frozenset({ADVICE_CONTRACT, "canonical-advice-v1"})
PUBLIC_RULESET = "tencent_daguandan.v1"


class ModelContractError(ValueError):
    """Public contract violation; deliberately contains no input or provider data."""

    def __init__(self, code: str = "MODEL_INPUT_INVALID") -> None:
        self.code = code
        super().__init__(code)


def _integer(value: Any, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _identity(value: Any) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 128
            and all(c.isalnum() or c in "._:-" for c in value))


def _card(value: Any) -> bool:
    if not _integer(value, 0, 0x14D):
        return False
    suit, rank = (value >> 4) & 15, value & 15
    return (suit in (1, 2, 3, 4) and 1 <= rank <= 13
            or suit == 0 and rank in (14, 15))


def _public_action(action: PublicAction, *, observed_level: int | None = None) -> PublicAction:
    # Never serialize __dict__, arbitrary nested metadata, or a subclass's to_dict.
    if not isinstance(action, PublicAction):
        raise ModelContractError()
    if (not _integer(action.index, 1, 10000)
            or not _integer(action.player, 0, 3)
            or not _integer(action.trick, 0, 10000)
            or action.next_player is not None and not _integer(action.next_player, 0, 3)
            or action.kind not in ("play", "pass")
            or not isinstance(action.cards, (tuple, list))
            or not all(_card(c) for c in action.cards)
            or len(action.cards) != len(set(action.cards))
            or len(action.cards) > 27
            or (action.kind == "pass") != (len(action.cards) == 0)):
        raise ModelContractError()
    cards = tuple(action.cards)
    combo_type = action.combo_type
    if (observed_level is not None and action.kind == "play"
            and _integer(combo_type, 10, 11)):
        four_kings = set(cards) == {0x0E, 0x0F, 0x10E, 0x10F}
        # Tencent's 11 is the model's Four Kings (10). Tencent's large
        # same-rank bomb also uses 10, and becomes a rank bomb in the model.
        if four_kings:
            combo_type = 10
        elif combo_type == 10 and 6 <= len(cards) <= 10:
            natural_ranks = {card & 15 for card in cards
                             if not _is_level_wild(card, observed_level)}
            if len(natural_ranks) != 1 or next(iter(natural_ranks)) > 13:
                raise ModelContractError()
            combo_type = 8
        else:
            raise ModelContractError()
    if combo_type is not None and not _integer(combo_type, 1, 10):
        raise ModelContractError()
    return PublicAction(action.index, action.player, action.kind, cards,
                        combo_type, action.next_player, action.trick)


def _action_from_wire(value: Any) -> PublicAction:
    fields = {"index", "player", "kind", "cards", "combo_type", "next_player", "trick"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ModelContractError()
    return _public_action(PublicAction(**value))


@dataclass(frozen=True, slots=True)
class CanonicalPublicGameView:
    """Whitelisted play-phase view, NOT a complete Guandan A04/A05 environment.

    GameObservation is emitted only for active play by the state engine. `phase`
    records that limited fact; it does not invent declaration/sub-action phases.
    `public_play` completeness says nothing about private training/environment state.
    Provider ACLs must still resolve public combination semantics uniquely.
    """

    session_id: str
    game_id: str
    state_version: int
    my_seat: int
    current_seat: int
    first_seat: int
    partner_seat: int
    level: int
    trick: int
    hand: tuple[int, ...]
    remaining: tuple[int, int, int, int]
    public_history: tuple[PublicAction, ...] = ()
    lead_action: PublicAction | None = None
    free_lead: bool = False
    schema_version: int = SCHEMA_VERSION
    ruleset_id: str = PUBLIC_RULESET
    phase: str = "play"
    visibility: str = "public_plus_self"
    completeness: str = "public_play"
    ambiguities: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[str, str, int]:
        return self.session_id, self.game_id, self.state_version

    def validate(self) -> None:
        if (type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION
                or self.ruleset_id != PUBLIC_RULESET or self.phase != "play"
                or self.visibility != "public_plus_self" or self.completeness != "public_play"
                or self.ambiguities):
            raise ModelContractError("MODEL_INPUT_UNSUPPORTED")
        if (not _identity(self.session_id) or not _identity(self.game_id)
                or not _integer(self.state_version, 0, 2**63 - 1)
                or not all(_integer(s, 0, 3) for s in
                           (self.my_seat, self.current_seat, self.first_seat, self.partner_seat))
                or self.partner_seat != (self.my_seat + 2) % 4
                or self.current_seat != self.my_seat
                or not _integer(self.level, 1, 13) or not _integer(self.trick, 0, 10000)
                or type(self.free_lead) is not bool):
            raise ModelContractError()
        if (not isinstance(self.hand, (tuple, list)) or not 1 <= len(self.hand) <= 27
                or not all(_card(c) for c in self.hand) or len(set(self.hand)) != len(self.hand)
                or not isinstance(self.remaining, (tuple, list)) or len(self.remaining) != 4
                or not all(_integer(c, 0, 27) for c in self.remaining)
                or self.remaining[self.my_seat] != len(self.hand)):
            raise ModelContractError()
        if not isinstance(self.public_history, (tuple, list)) or len(self.public_history) > 10000:
            raise ModelContractError()
        seen_cards = set(self.hand)
        last_index = 0
        last_trick = 0
        by_index: dict[int, PublicAction] = {}
        for item in self.public_history:
            action = _public_action(item)
            if (action.index <= last_index or not last_trick <= action.trick <= self.trick
                    or seen_cards.intersection(action.cards)):
                raise ModelContractError()
            seen_cards.update(action.cards)
            by_index[action.index] = action
            last_index, last_trick = action.index, action.trick
        if self.lead_action is not None:
            lead = _public_action(self.lead_action)
            if (lead.kind != "play" or lead.trick != self.trick
                    or set(lead.cards).intersection(self.hand)
                    or lead.index in by_index and by_index[lead.index] != lead):
                raise ModelContractError()
        if self.free_lead != (self.lead_action is None):
            raise ModelContractError("MODEL_INPUT_AMBIGUOUS")

    @classmethod
    def from_observation(cls, observation: GameObservation) -> CanonicalPublicGameView:
        if not isinstance(observation, GameObservation):
            raise ModelContractError()
        view = cls(session_id=observation.session_id, game_id=observation.game_id,
                   state_version=observation.state_version, my_seat=observation.my_seat,
                   current_seat=observation.current_seat, first_seat=observation.first_seat,
                   partner_seat=(observation.my_seat + 2) % 4, level=observation.level,
                   trick=observation.trick, hand=tuple(observation.hand),
                   remaining=tuple(observation.remaining),
                   public_history=tuple(_public_action(a, observed_level=observation.level)
                                        for a in observation.history),
                   lead_action=_public_action(observation.lead_action, observed_level=observation.level)
                   if observation.lead_action is not None else None,
                   free_lead=observation.free_lead)
        view.validate()
        return view

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {"schema_version": self.schema_version, "ruleset_id": self.ruleset_id,
                "session_id": self.session_id, "game_id": self.game_id,
                "state_version": self.state_version, "my_seat": self.my_seat,
                "current_seat": self.current_seat, "first_seat": self.first_seat,
                "partner_seat": self.partner_seat, "level": self.level, "phase": self.phase,
                "trick": self.trick, "hand": list(self.hand), "remaining": list(self.remaining),
                "public_history": [_public_action(a).to_dict() for a in self.public_history],
                "lead_action": _public_action(self.lead_action).to_dict()
                if self.lead_action is not None else None,
                "free_lead": self.free_lead, "visibility": self.visibility,
                "completeness": self.completeness, "ambiguities": list(self.ambiguities)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CanonicalPublicGameView:
        fields = set(cls.__dataclass_fields__)
        if not isinstance(data, dict) or set(data) != fields:
            raise ModelContractError()
        try:
            copied = dict(data)
            for name in ("hand", "remaining", "ambiguities"):
                if not isinstance(copied[name], list):
                    raise ModelContractError()
                copied[name] = tuple(copied[name])
            if not isinstance(data["public_history"], list):
                raise ModelContractError()
            copied["public_history"] = tuple(_action_from_wire(a) for a in data["public_history"])
            copied["lead_action"] = (_action_from_wire(data["lead_action"])
                                     if data["lead_action"] is not None else None)
            view = cls(**copied)
            view.validate()
            return view
        except (TypeError, KeyError, ValueError) as exc:
            if isinstance(exc, ModelContractError):
                raise
            raise ModelContractError() from None

    def to_observation(self) -> GameObservation:
        """Explicit legacy ACL conversion; capture timestamp is not model input."""
        self.validate()
        return GameObservation(self.session_id, self.game_id, self.state_version, 0.0,
                               self.level, self.my_seat, self.first_seat, self.current_seat,
                               tuple(self.hand), tuple(self.remaining), tuple(self.public_history),
                               self.lead_action, self.free_lead, self.trick)


# Only these bounded, provider-labelled scalar annotations cross the model boundary.
_MODEL_METADATA = _METADATA_KEYS


def public_model_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(metadata, Mapping):
        raise ModelContractError("MODEL_OUTPUT_INVALID")
    return {key: value for key, value in metadata.items()
            if key in _MODEL_METADATA and (type(value) is bool or _identity(value))}


@dataclass(frozen=True, slots=True)
class CanonicalAdviceResult(AdviceResult):
    provider: str = ""
    model_version: str = ""

    def __post_init__(self) -> None:
        # Explicit base call is required for frozen slotted dataclass inheritance.
        AdviceResult.__post_init__(self)
        normalized: list[AdviceCandidate] = []
        for index, candidate in enumerate(self.candidates, 1):
            if not isinstance(candidate, AdviceCandidate):
                normalized.append(candidate)
            elif candidate.rank <= 0:
                normalized.append(AdviceCandidate(
                    candidate.cards, candidate.kind, candidate.label, candidate.score,
                    candidate.claim, candidate.score_kind, candidate.metadata, index, candidate.claim_kind))
            else:
                normalized.append(candidate)
        object.__setattr__(self, "candidates", tuple(normalized))

    @classmethod
    def from_legacy(cls, result: AdviceResult, *, provider: str,
                    model_version: str, score_kind: str) -> CanonicalAdviceResult:
        candidates = tuple(AdviceCandidate(tuple(c.cards), c.kind, c.label, c.score,
                                           tuple(c.claim or ()), c.score_kind or score_kind,
                                           public_model_metadata(c.metadata), index,
                                           getattr(c, "claim_kind", ""))
                           for index, c in enumerate(result.candidates, 1))
        return cls(session_id=result.session_id, game_id=result.game_id,
                   state_version=result.state_version, request_id=result.request_id,
                   candidates=candidates, elapsed_ms=result.elapsed_ms, status=result.status,
                   error_code=result.error_code, error_message=result.error_message,
                   model_fingerprint=result.model_fingerprint, score_kind=score_kind,
                   metadata=public_model_metadata(result.metadata),
                   provider=provider, model_version=model_version)

    def to_dict(self) -> dict[str, Any]:
        if (not _identity(self.session_id) or not _identity(self.game_id)
                or not _identity(self.request_id) or not _identity(self.provider)
                or not _identity(self.model_version) or not _identity(self.score_kind)
                or not _integer(self.state_version, 0, 2**63 - 1)
                or self.status not in ("ready", "error", "unavailable")
                or type(self.elapsed_ms) not in (int, float)
                or not math.isfinite(self.elapsed_ms) or self.elapsed_ms < 0
                or not isinstance(self.model_fingerprint, str)
                or self.model_fingerprint and (len(self.model_fingerprint) != 64
                    or any(c not in "0123456789abcdef" for c in self.model_fingerprint))
                or not isinstance(self.error_code, str) or len(self.error_code) > 80
                or not isinstance(self.error_message, str) or len(self.error_message) > 300
                or len(self.candidates) > 5
                or self.status == "ready" and not self.candidates
                or self.status != "ready" and self.candidates):
            raise ModelContractError("MODEL_OUTPUT_INVALID")
        for c in self.candidates:
            if (not isinstance(c, AdviceCandidate) or not isinstance(c.cards, (tuple, list))
                    or len(c.cards) > 27
                    or not all(_card(card) for card in c.cards) or len(set(c.cards)) != len(c.cards)
                    or not _identity(c.kind)
                     or c.kind == "pass" and len(c.cards) != 0
                     or c.kind in ("play", "") and len(c.cards) == 0
                     or not isinstance(c.label, str) or len(c.label) > 80
                    or not _integer(c.rank, 1, len(self.candidates))
                    or c.rank != len([item for item in self.candidates[:c.rank] if isinstance(item, AdviceCandidate)])
                    or not _identity(c.score_kind)
                    or c.claim_kind and not _identity(c.claim_kind)
                    or c.score is not None and (type(c.score) not in (int, float)
                                                or not math.isfinite(c.score))
                    or not all(_integer(rank, 0, 15) for rank in c.claim or ())
                    or not isinstance(c.metadata, Mapping)):
                raise ModelContractError("MODEL_OUTPUT_INVALID")
        return {"schema_version": CANONICAL_ADVICE_RESULT_SCHEMA_VERSION, "session_id": self.session_id,
                "game_id": self.game_id, "state_version": self.state_version,
                "request_id": self.request_id, "elapsed_ms": self.elapsed_ms,
                "status": self.status, "error_code": self.error_code,
                "error_message": self.error_message, "model_fingerprint": self.model_fingerprint,
                "score_kind": self.score_kind, "metadata": public_model_metadata(self.metadata),
                "provider": self.provider, "model_version": self.model_version,
                "candidates": [{"cards": list(c.cards), "kind": c.kind, "label": c.label,
                                "score": c.score, "claim": list(c.claim or ()),
                                "score_kind": c.score_kind,
                                "metadata": public_model_metadata(c.metadata),
                                "rank": c.rank, "claim_kind": c.claim_kind}
                               for c in self.candidates]}


    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CanonicalAdviceResult:
        """Decode only the current canonical schema; legacy is an explicit adapter."""
        return decode_canonical_advice(data)


def _decode_advice(data: Mapping[str, Any], *, legacy: bool) -> CanonicalAdviceResult:
    fields = set(CanonicalAdviceResult.__dataclass_fields__) | {"schema_version"}
    required = {"session_id", "game_id", "state_version", "request_id", "candidates",
                "elapsed_ms", "status", "error_code", "error_message",
                "model_fingerprint", "score_kind", "metadata", "provider",
                "model_version"}
    if not isinstance(data, Mapping) or set(data) - fields or set(data) | {"provider", "model_version", "score_kind", "metadata", "rank", "claim_kind"} < required:
        raise ModelContractError("MODEL_OUTPUT_INVALID")
    if type(data.get("schema_version")) is not int or data["schema_version"] != CANONICAL_ADVICE_RESULT_SCHEMA_VERSION:
        raise ModelContractError("MODEL_OUTPUT_INVALID")
    if not legacy and ("provider" not in data or "model_version" not in data):
        raise ModelContractError("MODEL_OUTPUT_INVALID")
    try:
        candidates_wire = data["candidates"]
        if not isinstance(candidates_wire, list):
            raise ModelContractError("MODEL_OUTPUT_INVALID")
        candidates: list[AdviceCandidate] = []
        for value in candidates_wire:
            if not isinstance(value, Mapping):
                raise ModelContractError("MODEL_OUTPUT_INVALID")
            candidate_fields = {"cards", "kind", "label", "score", "claim",
                                "score_kind", "metadata", "rank", "claim_kind"}
            if set(value) - candidate_fields or not {"cards", "kind", "label", "score", "claim"} <= set(value):
                raise ModelContractError("MODEL_OUTPUT_INVALID")
            if not isinstance(value["cards"], list) or value["claim"] is not None and not isinstance(value["claim"], list):
                raise ModelContractError("MODEL_OUTPUT_INVALID")
            candidates.append(AdviceCandidate(
                cards=tuple(value["cards"]), kind=value["kind"], label=value["label"],
                score=value["score"], claim=tuple(value["claim"] or ()),
                score_kind=value.get("score_kind", data.get("score_kind", "unknown")),
                metadata=value.get("metadata", {}), rank=int(value.get("rank", 0)),
                claim_kind=value.get("claim_kind", "")))
        result = CanonicalAdviceResult(
            session_id=data["session_id"], game_id=data["game_id"],
            state_version=data["state_version"], request_id=data["request_id"],
            candidates=tuple(candidates), elapsed_ms=data["elapsed_ms"], status=data["status"],
            error_code=data["error_code"], error_message=data["error_message"],
            model_fingerprint=data["model_fingerprint"], score_kind=data.get("score_kind", "unknown"),
            metadata=data.get("metadata", {}),
            provider=data.get("provider", "fabledan" if legacy else ""),
            model_version=data.get("model_version", "legacy" if legacy else ""))
        result.to_dict()
        return result
    except ModelContractError:
        raise
    except (TypeError, KeyError, ValueError, OverflowError):
        raise ModelContractError("MODEL_OUTPUT_INVALID") from None


def decode_canonical_advice(data: Mapping[str, Any]) -> CanonicalAdviceResult:
    """Strict decoder for the current worker/application contract."""
    return _decode_advice(data, legacy=False)


def decode_legacy_advice_result(data: Mapping[str, Any]) -> CanonicalAdviceResult:
    """Explicit FableDan legacy wire adapter; never used for generic worker input."""
    return _decode_advice(data, legacy=True)


class LegacyAdviceResultAdapter:
    decode = staticmethod(decode_legacy_advice_result)


# --- Follow semantics: a following candidate must beat the current target ---
# Point semantics mirror the frozen reference engine (python_rules._rank_value):
# the level rank is 15, A=14, jokers 16/17, and the deuce is 2 (the trained
# reference engine's ordering, kept verbatim for deployment comparison).
_FAMILY_BY_TYPE = {1: "single", 2: "pair", 3: "triple", 4: "straight",
                   5: "full", 6: "serial_pair", 7: "serial_triple",
                   8: "bomb", 9: "sflush", 10: "rocket"}
_FAMILY_CODE = {"single": 1, "pair": 2, "triple": 3, "straight": 4,
                "full": 5, "serial_pair": 6, "serial_triple": 7}
_DECLARATION_FAMILY_CODE = {"SINGLE": 1, "PAIR": 2, "TRIPLE": 3, "STRAIGHT": 4,
                            "FULL_HOUSE": 5, "PAIR_SEQUENCE": 6,
                            "TRIPLE_SEQUENCE": 7, "RANK_BOMB": 8,
                            "STRAIGHT_FLUSH": 9, "FOUR_KINGS": 10}
# Serial windows in the frozen engine point space (python_rules sequence
# order A,2,3,...,K,A: the wheel A2345 tops at 5, TJQKA tops at 14; the
# deuce is point 2).  Windows are tuples of ENGINE POINTS with the window's
# comparison = its last element.
_ENGINE_SEQUENCE = (14, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14)
_SERIAL_WINDOWS = {
    1: tuple(_ENGINE_SEQUENCE[index:index + 5] for index in range(10)),
    2: tuple(_ENGINE_SEQUENCE[index:index + 3] for index in range(12)),
    3: tuple(_ENGINE_SEQUENCE[index:index + 2] for index in range(13)),
}


def _point_of_rank_label(label: str, level: int | None) -> int:
    if label == "BJ":
        return 16
    if label == "RJ":
        return 17
    if level is not None and label == _rank_label(level):
        return 15
    return {"A": 14, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8,
            "9": 9, "T": 10, "J": 11, "Q": 12, "K": 13}.get(label, 0)


def _rank_label(wire_rank: int) -> str:
    # Wire ranks 11/12/13 are J/Q/K (the phase 1 frozen engine-label mapping;
    # str(11..13) would make the point lookup return 0 - same family as the
    # phase 1 returned defect).
    return {1: "A", 2: "2", 10: "T", 11: "J", 12: "Q", 13: "K"}.get(
        wire_rank, str(wire_rank) if 3 <= wire_rank <= 9 else "")


def _card_rank_label(card: int) -> str:
    if (card >> 4) & 15 == 0:
        return {14: "BJ", 15: "RJ"}[card & 15]
    return _rank_label(card & 15)


def _is_level_wild(card: int, level: int) -> bool:
    return ((card >> 4) & 15) == 2 and (card & 15) == level


def _natural_combo_points(cards: tuple[int, ...], kind: str, level: int) -> list[int]:
    # A level card retains its natural point inside a sequence. Only the
    # heart of the level may fill a gap. One-rank plays use the level bonus.
    comparison_level = None if kind in ("straight", "serial_pair", "serial_triple", "sflush") else level
    return [_point_of_rank_label(_card_rank_label(card), comparison_level)
            for card in cards if not _is_level_wild(card, level)]


def _bomb_identity(size: int, comparison_point: int) -> tuple[int, int, int]:
    if size >= 6:
        return (80 + min(size, 8), size, comparison_point)
    if size == 5:
        return (60, size, comparison_point)
    return (50, size, comparison_point)


def _same_rank_comparison(natural_points: list[int]) -> int:
    """Comparison point of a one-rank combo; wilds must take the natural
    rank (the only fixed-size interpretation), all-wild means the level."""
    if not natural_points:
        return 15
    return max(natural_points)


def _best_serial_top(natural_points: list[int], wilds: int, group: int) -> int:
    """Highest serial window top the cards can realize (wilds fill gaps).

    Windows and tops are engine points; natural cards must fall inside the
    window (no duplicates within one serial) and wilds cover the gaps."""
    distinct = sorted(set(natural_points))
    best = 0
    for window in _SERIAL_WINDOWS[group]:
        if any(point not in window for point in distinct):
            continue
        missing = len(window) - len(distinct)
        if missing < 0 or missing > wilds:
            continue
        best = max(best, window[-1])
    return best


def _combo_identity(cards: tuple[int, ...], kind: str, level: int) -> tuple:
    """Comparable identity of one combo under max-strength wild interpretation.

    Bombs: (1, tier, size, rank_point).  Non-bombs: (0, size, family_code,
    comparison_point).  Different non-bomb families never compare equal.
    """
    natural_points = _natural_combo_points(cards, kind, level)
    wilds = sum(1 for card in cards if _is_level_wild(card, level))
    if kind == "rocket":
        return (1, 100, 0, 0)
    if kind == "sflush":
        return (1, 70, 0, _best_serial_top(natural_points, wilds, 1))
    if kind == "bomb":
        return (1,) + _bomb_identity(len(cards), _same_rank_comparison(natural_points))
    size = len(cards)
    if kind in ("single", "pair", "triple"):
        return (0, size, _FAMILY_CODE[kind], _same_rank_comparison(natural_points))
    if kind == "full":
        counts: dict[int, int] = {}
        for point in natural_points:
            counts[point] = counts.get(point, 0) + 1
        best = 0
        for triple_point in sorted(counts, reverse=True):
            need = 3 - counts[triple_point]
            if need < 0 or need > wilds:
                continue
            left = wilds - need
            for pair_point, pair_count in counts.items():
                if pair_point != triple_point and pair_count + left >= 2:
                    best = max(best, triple_point)
                    break
            else:
                if left >= 2:
                    best = max(best, triple_point)
        return (0, size, _FAMILY_CODE["full"], best)
    if kind == "straight":
        return (0, size, _FAMILY_CODE["straight"],
                _best_serial_top(natural_points, wilds, 1))
    if kind == "serial_pair":
        return (0, size, _FAMILY_CODE["serial_pair"],
                _best_serial_top(natural_points, wilds, 2))
    if kind == "serial_triple":
        return (0, size, _FAMILY_CODE["serial_triple"],
                _best_serial_top(natural_points, wilds, 3))
    # triple+1 / triple+2 / four+2 / play and other descriptive kinds never
    # appear as follows in the validated provider output; shape checks above
    # have already constrained them.
    return (0, size, 0, 0)


def _declared_target_identity(declaration, level: int) -> tuple:
    """Comparable identity of a confirmed PublicDeclaration target."""
    family = declaration.family
    code = _DECLARATION_FAMILY_CODE[family]
    size = declaration.length
    if family in _DECLARATION_SERIAL_GROUPS:
        # Sequence families compare in the natural engine window space (the
        # canonical cards fix the realized window; its top is the comparison
        # point).  The level-card 15 rule is a one-rank-family rule and never
        # applies here: at level 5 the wheel A2345 tops at 5, not 15.
        top = _declared_serial_top(declaration)
        if family == "STRAIGHT_FLUSH":
            return (1, 70, 0, top)
        return (0, size, code, top)
    gd_comparison = declaration.comparison_rank
    gd_level = {1: 14, 2: 15}.get(level, level)
    if gd_comparison == gd_level:
        point = 15
    else:
        point = {14: 14, 15: 2}.get(gd_comparison, gd_comparison)
    if family == "FOUR_KINGS":
        return (1, 100, 0, 0)
    if family == "STRAIGHT_FLUSH":
        return (1, 70, 0, point)
    if family == "RANK_BOMB":
        return (1,) + _bomb_identity(size, point)
    return (0, size, code, point)


_DECLARATION_SERIAL_GROUPS = {"STRAIGHT": 1, "STRAIGHT_FLUSH": 1,
                              "PAIR_SEQUENCE": 2, "TRIPLE_SEQUENCE": 3}


def _declared_serial_top(declaration) -> int:
    """Natural engine-space comparison top of a confirmed sequence target.

    The confirmed declaration's canonical cards (wild assignments already
    resolved) fix the realized natural window; the comparison point is that
    window's last engine point (A=14, deuce=2, wheel A2345 tops 5).  The
    declared GD comparison rank must agree with the window top (engine point
    2 <-> GD 15 for the A-2 serial wheel); any disagreement fails closed.
    """
    faces = declaration._effective_faces(declaration.canonical_cards,
                                         declaration.wild_assignments)
    points = sorted({{14: 14, 15: 2}.get(rank, rank) for rank, _suit, _card in faces})
    group = _DECLARATION_SERIAL_GROUPS[declaration.family]
    matches = [window for window in _SERIAL_WINDOWS[group] if set(window) == set(points)]
    if len(matches) != 1:
        raise ModelContractError("MODEL_INPUT_INVALID")
    top = matches[0][-1]
    if {2: 15}.get(top, top) != declaration.comparison_rank:
        raise ModelContractError("MODEL_INPUT_INVALID")
    return top


def _engine_natural_counts(values: list[int]) -> dict[int, int]:
    """Natural wire rank values -> engine point counts (A=14, deuce=2)."""
    counts: dict[int, int] = {}
    for value in values:
        point = {1: 14, 2: 2}.get(value, value)
        counts[point] = counts.get(point, 0) + 1
    return counts


def _serial_feasible(counts: dict[int, int], wilds: int, group: int) -> bool:
    """Serial feasibility in the engine point space: some consecutive window
    of `group`-sized ranks covers every natural rank, no rank exceeds the
    group multiplicity, and the wilds fill exactly the gaps."""
    for window in _SERIAL_WINDOWS[group]:
        if any(point not in window for point in counts):
            continue
        if any(count > group for count in counts.values()):
            continue
        missing = sum(group - counts.get(point, 0) for point in window)
        if missing == wilds:
            return True
    return False


def _shape_ok(kind: str, cards: tuple[int, ...], values: list[int],
              natural_values: list[int], wild_count: int, level: int) -> bool:
    """Shape feasibility; the heart-level card may complete any group.

    Natural-only shapes pass unchanged; a wild-containing combo is feasible
    when some assignment of the wilds realizes the kind's shape.
    """
    if kind == "rocket":
        return not wild_count and values == [14, 14, 15, 15]
    if kind == "sflush":
        suits = {(card >> 4) & 15 for card in cards if not _is_level_wild(card, level)}
        return (len(cards) == 5 and len(suits) == 1 and 0 not in suits
                and _serial_feasible(_engine_natural_counts(natural_values), wild_count, 1))

    if wild_count == 0:
        counts = sorted({value: values.count(value) for value in set(values)}.values())
        if kind == "single":
            return len(cards) == 1
        if kind == "pair":
            return len(cards) == 2 and counts == [2]
        if kind == "triple":
            return len(cards) == 3 and counts == [3]
        if kind == "full":
            return len(cards) == 5 and counts == [2, 3]
        if kind == "triple+1":
            return len(cards) == 4 and counts == [1, 3]
        if kind == "triple+2":
            return len(cards) == 5 and counts == [2, 3]
        if kind == "four+2":
            return len(cards) == 6 and 4 in counts
        if kind == "bomb":
            return len(cards) >= 4 and counts == [len(cards)]
        if kind == "straight":
            # Engine point space: the wire-value consecutiveness check broke
            # A-high windows (T,J,Q,K,A -> [10,11,12,13,1]).
            return (len(cards) == 5
                    and _serial_feasible(_engine_natural_counts(values), 0, 1))
        if kind == "serial_pair":
            return (len(cards) == 6
                    and _serial_feasible(_engine_natural_counts(values), 0, 2))
        if kind == "serial_triple":
            return (len(cards) == 6
                    and _serial_feasible(_engine_natural_counts(values), 0, 3))
        return True  # descriptive kinds (play) and pass handled by callers

    # Wild-aware feasibility: distribute the wilds over the natural ranks.
    distinct_naturals = sorted(set(natural_values))
    if kind == "single":
        return len(cards) == 1
    if kind == "pair":
        return len(cards) == 2 and len(distinct_naturals) <= 1
    if kind == "triple":
        return len(cards) == 3 and len(distinct_naturals) <= 1
    if kind == "bomb":
        return len(cards) >= 4 and len(distinct_naturals) <= 1
    if kind == "full":
        counts: dict[int, int] = {}
        for value in natural_values:
            counts[value] = counts.get(value, 0) + 1
        for triple in sorted(counts, reverse=True):
            need = 3 - counts[triple]
            if need < 0 or need > wild_count:
                continue
            left = wild_count - need
            for pair, pair_count in counts.items():
                if pair != triple and pair_count + left >= 2:
                    return True
            if left >= 2:
                return True
        return False
    if kind == "straight":
        return (len(cards) == 5
                and _serial_feasible(_engine_natural_counts(natural_values),
                                     wild_count, 1))
    if kind == "serial_pair":
        return (len(cards) == 6
                and _serial_feasible(_engine_natural_counts(natural_values),
                                     wild_count, 2))
    if kind == "serial_triple":
        return (len(cards) == 6
                and _serial_feasible(_engine_natural_counts(natural_values),
                                     wild_count, 3))
    if kind == "triple+1":
        counts = {}
        for value in natural_values:
            counts[value] = counts.get(value, 0) + 1
        for triple in sorted(counts, reverse=True):
            need = 3 - counts[triple]
            if 0 <= need <= wild_count and len(cards) == 4:
                return True
        return False
    if kind == "triple+2":
        return len(cards) == 5 and _shape_ok("full", cards, values,
                                             natural_values, wild_count, level)
    if kind == "four+2":
        counts = {}
        for value in natural_values:
            counts[value] = counts.get(value, 0) + 1
        return len(cards) == 6 and any(count + wild_count >= 4
                                       for count in counts.values())
    return True


def validate_advice_for_view(view: CanonicalPublicGameView, result: CanonicalAdviceResult,
                             *, declared_facts: "DeclaredFacts | None" = None) -> None:
    """Validate provider output against the public Guandan action boundary.

    When the view is a follow (not a free lead), every non-pass candidate
    must beat the current winning action.  The target's comparison identity
    comes from the confirmed declaration when ``declared_facts`` is provided;
    without declared facts it is derived from the lead action's public cards,
    and a wild-containing lead whose interpretation would change the verdict
    fails closed (MODEL_OUTPUT_AMBIGUOUS) instead of guessing.
    """
    view.validate()
    if declared_facts is not None:
        declared_facts.validate_for_view(view)
    result.to_dict()

    kind_aliases = {"0": "pass", "1": "single", "2": "pair", "3": "triple",
                    "4": "straight", "5": "full", "6": "serial_pair",
                    "7": "serial_triple", "8": "bomb", "9": "sflush", "10": "rocket"}
    known = {"single", "pair", "triple", "full", "straight", "serial_pair",
             "serial_triple", "triple+1", "triple+2", "four+2", "bomb", "rocket",
             "sflush", "play"}
    hand = set(view.hand)
    identities = []
    for candidate in result.candidates:
        cards = tuple(candidate.cards)
        kind = kind_aliases.get(candidate.kind, candidate.kind)
        if kind == "pass":
            if view.free_lead or cards:
                raise ModelContractError("MODEL_OUTPUT_INVALID")
            identities.append(None)
            continue
        if (kind not in known or not cards or len(cards) != len(set(cards))
                or not set(cards) <= hand):
            raise ModelContractError("MODEL_OUTPUT_INVALID")
        raw_values = [card & 0x0F for card in cards]
        values = sorted(raw_values)
        wild_count = sum(1 for card in cards if _is_level_wild(card, view.level))
        natural_values = [value for card, value in zip(cards, raw_values)
                          if not _is_level_wild(card, view.level)]
        if not _shape_ok(kind, cards, values, natural_values, wild_count,
                         view.level):
            raise ModelContractError("MODEL_OUTPUT_INVALID")
        identities.append(_combo_identity(cards, kind, view.level))

    if view.free_lead:
        return
    # Follow state: verify every non-pass candidate beats the target.
    fact = declared_facts.current_winning_declaration if declared_facts is not None else None
    ambiguous = fact is not None and fact.status in ("unknown", "unsupported")
    target = None
    max_target = None
    if fact is not None and fact.status == "known":
        # The confirmed declaration is authoritative even when the raw cards
        # admit other interpretations.
        target = _declared_target_identity(fact.declaration, view.level)
    elif not ambiguous:
        lead = view.lead_action
        lead_kind = _FAMILY_BY_TYPE.get(lead.combo_type)
        if lead_kind is None:
            raise ModelContractError("MODEL_OUTPUT_INVALID")
        max_target = _combo_identity(tuple(lead.cards), lead_kind, view.level)
        min_target = _min_combo_identity(tuple(lead.cards), lead_kind, view.level)
        if min_target != max_target:
            # A wild-containing lead whose interpretation changes the verdict
            # cannot be verified from public cards alone; fail closed.
            ambiguous = True
    for identity in identities:
        if identity is None:
            continue  # pass is always a legal follow
        if ambiguous:
            raise ModelContractError("MODEL_OUTPUT_AMBIGUOUS")
        expected = target if target is not None else max_target
        if not _beats(identity, expected):
            raise ModelContractError("MODEL_OUTPUT_NOT_BEATING")


def _beats(candidate: tuple, target: tuple) -> bool:
    if candidate[0] != target[0]:
        return candidate[0] == 1  # bomb-like beats non-bomb
    if candidate[0] == 1:
        return candidate[1:4] > target[1:4]
    if candidate[2] != target[2] or candidate[1] != target[1]:
        return False  # different family or size never beats
    return candidate[3] > target[3]


def _min_combo_identity(cards: tuple[int, ...], kind: str, level: int) -> tuple:
    """Weakest legal interpretation of a combo (wilds assigned to minimize).

    Used only to detect whether a wild-containing lead is interpretation-
    dependent; same structure as _combo_identity.
    """
    natural_points = _natural_combo_points(cards, kind, level)
    wilds = sum(1 for card in cards if _is_level_wild(card, level))
    if kind == "rocket":
        return (1, 100, 0, 0)
    if kind == "sflush":
        return (1, 70, 0, _min_serial_top(natural_points, wilds, 1))
    if kind == "bomb":
        return (1,) + _bomb_identity(len(cards), _min_same_rank(natural_points))
    size = len(cards)
    if kind in ("single", "pair", "triple"):
        return (0, size, _FAMILY_CODE[kind], _min_same_rank(natural_points))
    if kind == "full":
        counts: dict[int, int] = {}
        for point in natural_points:
            counts[point] = counts.get(point, 0) + 1
        best = None
        for triple_point in sorted(counts):
            need = 3 - counts[triple_point]
            if need < 0 or need > wilds:
                continue
            left = wilds - need
            for pair_point, pair_count in counts.items():
                if pair_point != triple_point and pair_count + left >= 2:
                    best = triple_point if best is None else min(best, triple_point)
                    break
            else:
                if left >= 2:
                    best = triple_point if best is None else min(best, triple_point)
        return (0, size, _FAMILY_CODE["full"], best if best is not None else 0)
    if kind == "straight":
        return (0, size, _FAMILY_CODE["straight"],
                _min_serial_top(natural_points, wilds, 1))
    if kind == "serial_pair":
        return (0, size, _FAMILY_CODE["serial_pair"],
                _min_serial_top(natural_points, wilds, 2))
    if kind == "serial_triple":
        return (0, size, _FAMILY_CODE["serial_triple"],
                _min_serial_top(natural_points, wilds, 3))
    return (0, size, 0, 0)


def _min_same_rank(natural_points: list[int]) -> int:
    if not natural_points:
        return 15
    return min(natural_points)


def _min_serial_top(natural_points: list[int], wilds: int, group: int) -> int:
    distinct = sorted(set(natural_points))
    best = None
    for window in _SERIAL_WINDOWS[group]:
        if any(point not in window for point in distinct):
            continue
        missing = len(window) - len(distinct)
        if missing < 0 or missing > wilds:
            continue
        best = window[-1] if best is None else min(best, window[-1])
    return best if best is not None else 0
