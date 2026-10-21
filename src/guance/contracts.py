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


# Backward-compatible name for the public request; the union is explicit and tagged.
RecommendationRequest = PublicRecommendationRequest
ModelRecommendationRequest = PublicRecommendationRequest | ConfirmedDecisionRecommendationRequest


def recommendation_request_kind(request: ModelRecommendationRequest) -> str:
    if type(request) is PublicRecommendationRequest:
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


def _public_action(action: PublicAction) -> PublicAction:
    # Never serialize __dict__, arbitrary nested metadata, or a subclass's to_dict.
    if not isinstance(action, PublicAction):
        raise ModelContractError()
    if (not _integer(action.index, 1, 10000)
            or not _integer(action.player, 0, 3)
            or not _integer(action.trick, 0, 10000)
            or action.next_player is not None and not _integer(action.next_player, 0, 3)
            or action.combo_type is not None and not _integer(action.combo_type, 1, 10)
            or action.kind not in ("play", "pass")
            or not isinstance(action.cards, (tuple, list))
            or not all(_card(c) for c in action.cards)
            or len(action.cards) != len(set(action.cards))
            or len(action.cards) > 27
            or (action.kind == "pass") != (len(action.cards) == 0)):
        raise ModelContractError()
    return PublicAction(action.index, action.player, action.kind, tuple(action.cards),
                        action.combo_type, action.next_player, action.trick)


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
                   public_history=tuple(_public_action(a) for a in observation.history),
                   lead_action=_public_action(observation.lead_action)
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


def validate_advice_for_view(view: CanonicalPublicGameView, result: CanonicalAdviceResult) -> None:
    """Validate provider output against the public Guandan action boundary."""
    view.validate()
    result.to_dict()

    def ranks(cards: tuple[int, ...]) -> list[int]:
        return sorted(card & 0x0F for card in cards)

    def consecutive(values: list[int]) -> bool:
        return len(values) == len(set(values)) and all(b == a + 1 for a, b in zip(values, values[1:]))

    kind_aliases = {"0": "pass", "1": "single", "2": "pair", "3": "triple",
                    "4": "straight", "5": "full", "6": "serial_pair",
                    "7": "serial_triple", "8": "bomb", "9": "sflush", "10": "rocket"}
    known = {"single", "pair", "triple", "full", "straight", "serial_pair",
             "serial_triple", "triple+1", "triple+2", "four+2", "bomb", "rocket",
             "sflush", "play"}
    hand = set(view.hand)
    for candidate in result.candidates:
        cards = tuple(candidate.cards)
        kind = kind_aliases.get(candidate.kind, candidate.kind)
        if kind == "pass":
            if view.free_lead or cards:
                raise ModelContractError("MODEL_OUTPUT_INVALID")
            continue
        if (kind not in known or not cards or len(cards) != len(set(cards))
                or not set(cards) <= hand):
            raise ModelContractError("MODEL_OUTPUT_INVALID")
        values = ranks(cards)
        counts = sorted({value: values.count(value) for value in set(values)}.values())
        if kind == "single" and len(cards) != 1:
            raise ModelContractError("MODEL_OUTPUT_INVALID")
        if kind == "pair" and (len(cards) != 2 or counts != [2]):
            raise ModelContractError("MODEL_OUTPUT_INVALID")
        if kind == "triple" and (len(cards) != 3 or counts != [3]):
            raise ModelContractError("MODEL_OUTPUT_INVALID")
        if kind == "full" and (len(cards) != 5 or counts != [2, 3]):
            raise ModelContractError("MODEL_OUTPUT_INVALID")
        if kind == "triple+1" and (len(cards) != 4 or counts != [1, 3]):
            raise ModelContractError("MODEL_OUTPUT_INVALID")
        if kind == "triple+2" and (len(cards) != 5 or counts != [2, 3]):
            raise ModelContractError("MODEL_OUTPUT_INVALID")
        if kind == "four+2" and (len(cards) != 6 or 4 not in counts):
            raise ModelContractError("MODEL_OUTPUT_INVALID")
        if kind == "bomb" and (len(cards) < 4 or counts != [len(cards)]):
            raise ModelContractError("MODEL_OUTPUT_INVALID")
        if kind == "rocket" and (len(cards) not in {2, 4} or not all(value >= 14 for value in values)):
            raise ModelContractError("MODEL_OUTPUT_INVALID")
        if kind == "straight" and (len(cards) < 5 or not consecutive(values)):
            raise ModelContractError("MODEL_OUTPUT_INVALID")
        if kind == "serial_pair":
            unique = sorted(set(values))
            if len(cards) != len(unique) * 2 or any(values.count(value) != 2 for value in unique) or not consecutive(unique):
                raise ModelContractError("MODEL_OUTPUT_INVALID")
        if kind == "serial_triple":
            unique = sorted(set(values))
            if len(cards) != len(unique) * 3 or any(values.count(value) != 3 for value in unique) or not consecutive(unique):
                raise ModelContractError("MODEL_OUTPUT_INVALID")
