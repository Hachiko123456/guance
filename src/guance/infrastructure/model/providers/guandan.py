"""Guandan token/runtime seams, unavailable by default (no invented A04/A05 state).

Only explicitly test-only factories may inject a runtime and complete synthetic state.
The production adapter cannot infer declaration/previous-result/legal-token environment
state from GameObservation. No torch/onnx import or heuristic fallback occurs here.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from time import perf_counter
from typing import Callable, Mapping, Protocol, Any

from guance.contracts import (
    ModelMetadata,
    ADVICE_CONTRACT, ADVICE_CONTRACT_ALIASES, PUBLIC_GAME_CONTRACT, PUBLIC_GAME_CONTRACT_ALIASES,
    AdviceCandidate, CanonicalAdviceResult, CanonicalPublicGameView, validate_advice_for_view,
)
from ..errors import ModelBackendError
from ..factory import AbstractProviderFactory
from ..manifest import ModelBundle, ModelManifest

PASS = "PASS"
COMMIT = "COMMIT"
GUANDAN_VERSIONS = {
    "rules_version": "GD-RULES-0.1", "action_version": "GD-ACTION-0.1",
    "environment_version": "GD-ENV-0.1", "encoding_version": "GD-ENCODING-0.1",
}
GUANDAN_INPUT_CONTRACTS = PUBLIC_GAME_CONTRACT_ALIASES | {"guandan-public-v1"}


@dataclass(frozen=True, slots=True)
class GuandanDecisionState:
    view: CanonicalPublicGameView
    phase: str
    declaration: tuple[str, ...]
    previous_result: str
    legal_tokens: Callable[[tuple[str, ...]], tuple[str, ...]]
    validate_action: Callable[[tuple[int, ...]], bool]


class GuandanRuntime(Protocol):
    def load(self, artifact: Path, manifest: ModelManifest) -> None: ...
    def score_tokens(self, state: GuandanDecisionState, prefix: tuple[str, ...],
                     legal_mask: tuple[str, ...]) -> Mapping[str, float]: ...
    def close(self) -> None: ...


class GuandanPublicObservationAdapter:
    def adapt(self, view: CanonicalPublicGameView) -> GuandanDecisionState:
        view.validate()
        # `phase=play` is not the precise action-generation phase; never manufacture it.
        raise ModelBackendError("GUANDAN_STATE_UNAVAILABLE")


class GuandanTokenActionDecoder:
    def __init__(self, card_tokens: Mapping[str, int]) -> None:
        if PASS in card_tokens or COMMIT in card_tokens or len(set(card_tokens.values())) != len(card_tokens):
            raise ModelBackendError("GUANDAN_TOKEN_INVALID")
        self.card_tokens = dict(card_tokens)

    def decode(self, tokens: tuple[str, ...], state: GuandanDecisionState,
               score: float) -> AdviceCandidate:
        view = state.view
        view.validate()
        if not tokens or len(tokens) > len(view.hand) + 1 or not math.isfinite(score):
            raise ModelBackendError("GUANDAN_TOKEN_INVALID")
        prefix: tuple[str, ...] = ()
        for token in tokens:
            mask = state.legal_tokens(prefix)
            if token not in mask:
                raise ModelBackendError("GUANDAN_TOKEN_INVALID")
            prefix += (token,)
        if tokens == (PASS,):
            if view.free_lead or not state.validate_action(()):
                raise ModelBackendError("GUANDAN_TOKEN_INVALID")
            return AdviceCandidate((), "pass", "不出", score, score_kind="guandan_token_score")
        if tokens[-1] != COMMIT or PASS in tokens or COMMIT in tokens[:-1] or len(tokens) < 2:
            raise ModelBackendError("GUANDAN_TOKEN_INVALID")
        try:
            cards = tuple(self.card_tokens[token] for token in tokens[:-1])
        except KeyError:
            raise ModelBackendError("GUANDAN_TOKEN_INVALID") from None
        if (len(set(cards)) != len(cards) or not set(cards) <= set(view.hand)
                or not state.validate_action(cards)):
            raise ModelBackendError("GUANDAN_TOKEN_INVALID")
        return AdviceCandidate(cards, "play", "出牌", score, score_kind="guandan_token_score")


@dataclass(frozen=True, slots=True)
class TokenPath:
    tokens: tuple[str, ...]
    score: float


class TokenSearchStrategy(Protocol):
    def search(self, runtime: GuandanRuntime, state: GuandanDecisionState,
               top_n: int) -> tuple[TokenPath, ...]: ...


class LegalMaskBeamSearch:
    """Bounded deterministic strategy seam, not a model or a fallback policy."""
    def __init__(self, beam_width: int = 5, max_steps: int = 28) -> None:
        self.beam_width = max(1, min(beam_width, 32))
        self.max_steps = max(1, min(max_steps, 28))

    def search(self, runtime: GuandanRuntime, state: GuandanDecisionState,
               top_n: int) -> tuple[TokenPath, ...]:
        frontier = [TokenPath((), 0.0)]
        completed: list[TokenPath] = []
        for _ in range(self.max_steps):
            expanded: list[TokenPath] = []
            for path in frontier:
                mask = state.legal_tokens(path.tokens)
                if (not isinstance(mask, tuple) or not mask or len(mask) > 110
                        or not all(isinstance(t, str) and t for t in mask) or len(set(mask)) != len(mask)):
                    raise ModelBackendError("GUANDAN_TOKEN_INVALID")
                logits = runtime.score_tokens(state, path.tokens, mask)
                # Scores of masked-out tokens are never used, even if highest.
                for token in mask:
                    score = logits.get(token)
                    if type(score) not in (int, float) or not math.isfinite(score):
                        raise ModelBackendError("MODEL_OUTPUT_INVALID")
                    candidate = TokenPath(path.tokens + (token,), path.score + score)
                    if token in (PASS, COMMIT):
                        completed.append(candidate)
                    else:
                        expanded.append(candidate)
            frontier = sorted(expanded, key=lambda p: (-p.score, p.tokens))[:self.beam_width]
            if not frontier:
                break
        if not completed:
            raise ModelBackendError("GUANDAN_TOKEN_INVALID")
        return tuple(sorted(completed, key=lambda p: (-p.score, p.tokens))[:max(1, min(top_n, 5))])


class GuandanAdvisor:
    def __init__(self, bundle: ModelBundle, runtime: GuandanRuntime | None,
                 observation_adapter: GuandanPublicObservationAdapter,
                 decoder: GuandanTokenActionDecoder, strategy: TokenSearchStrategy,
                 *, test_only: bool = False, top_n: int = 3) -> None:
        self.bundle, self.runtime = bundle, runtime
        self.observation_adapter, self.decoder, self.strategy = observation_adapter, decoder, strategy
        self.test_only = test_only
        self.top_n = max(1, min(top_n, 5))
        self._loaded = False
        self._closed = False

    def load(self) -> ModelMetadata:
        if self._closed:
            raise ModelBackendError("MODEL_CLOSED")
        self.bundle.verify()
        if not self.test_only or self.runtime is None:
            raise ModelBackendError("MODEL_UNAVAILABLE")
        if not self._loaded:
            self.runtime.load(self.bundle.artifact_path, self.bundle.manifest)
            self.bundle.verify()
            self._loaded = True
        m = self.bundle.manifest
        return ModelMetadata(status="loaded", provider=m.provider, backend=m.backend,
                runtime=m.runtime, model_version=m.model_version,
                model_sha256=m.artifact_sha256, bundle_fingerprint=m.artifact_sha256,
                test_only=True,
                score_kind="guandan_token_score",
                input_contract=m.input_contract, output_contract=m.output_contract,
                wire_input_contract=PUBLIC_GAME_CONTRACT, wire_output_contract=ADVICE_CONTRACT,
                capabilities=m.capabilities if isinstance(m.capabilities, Mapping) else {})

    def recommend(self, view: CanonicalPublicGameView, request_id: str) -> CanonicalAdviceResult:
        if not self._loaded or self._closed or self.runtime is None:
            raise ModelBackendError("MODEL_NOT_LOADED")
        started = perf_counter()
        state = self.observation_adapter.adapt(view)
        if (state.view != view or not state.phase or not state.previous_result
                or not isinstance(state.declaration, tuple)):
            raise ModelBackendError("GUANDAN_STATE_UNAVAILABLE")
        paths = self.strategy.search(self.runtime, state, self.top_n)
        candidates = tuple(self.decoder.decode(p.tokens, state, p.score) for p in paths)
        result = CanonicalAdviceResult(
            view.session_id, view.game_id, view.state_version, request_id,
            candidates=candidates, elapsed_ms=(perf_counter() - started) * 1000,
            model_fingerprint=self.bundle.manifest.artifact_sha256,
            score_kind="guandan_token_score", metadata={"test_only": True},
            provider=self.bundle.manifest.provider, model_version=self.bundle.manifest.model_version)
        validate_advice_for_view(view, result)
        return result

    def close(self) -> None:
        self._closed = True
        self._loaded = False
        if self.runtime is not None:
            self.runtime.close()


class GuandanProvider(AbstractProviderFactory):
    def __init__(self, *, runtime_factory: Callable[[ModelBundle], GuandanRuntime] | None = None,
                 observation_adapter: GuandanPublicObservationAdapter | None = None,
                 decoder: GuandanTokenActionDecoder | None = None,
                 strategy: TokenSearchStrategy | None = None, test_only: bool = False) -> None:
        if not test_only and any(x is not None for x in (runtime_factory, observation_adapter, decoder, strategy)):
            raise ModelBackendError("MODEL_UNAVAILABLE")
        self.runtime_factory, self.adapter = runtime_factory, observation_adapter
        self.decoder, self.strategy, self.test_only = decoder, strategy, test_only

    def validate(self, manifest: ModelManifest) -> None:
        if manifest.backend != "guandan":
            raise ModelBackendError("BACKEND_UNSUPPORTED")
        if (manifest.input_contract not in GUANDAN_INPUT_CONTRACTS
                or manifest.output_contract not in ADVICE_CONTRACT_ALIASES):
            raise ModelBackendError("CONTRACT_UNSUPPORTED")
        if any(getattr(manifest, name) != value for name, value in GUANDAN_VERSIONS.items()):
            raise ModelBackendError("MODEL_VERSION_UNSUPPORTED")
        if manifest.runtime not in ("unavailable", "torch", "onnx", "test"):
            raise ModelBackendError("RUNTIME_UNSUPPORTED")
        if manifest.runtime == "test" and not self.test_only:
            raise ModelBackendError("MODEL_UNAVAILABLE")
        if manifest.artifact_format not in ("torch_checkpoint", "pt", "onnx", "test"):
            raise ModelBackendError("MODEL_VERSION_UNSUPPORTED")
        if manifest.artifact_format == "test" and not self.test_only:
            raise ModelBackendError("MODEL_UNAVAILABLE")

    def create_observation_adapter(self) -> GuandanPublicObservationAdapter:
        return self.adapter if self.adapter is not None else GuandanPublicObservationAdapter()

    def create_runtime(self, bundle: ModelBundle) -> GuandanRuntime | None:
        return self.runtime_factory(bundle) if self.test_only and self.runtime_factory is not None else None

    def create(self, bundle: ModelBundle) -> GuandanAdvisor:
        self.validate(bundle.manifest)
        return GuandanAdvisor(bundle, self.create_runtime(bundle), self.create_observation_adapter(),
                              self.decoder or GuandanTokenActionDecoder({}),
                              self.strategy or LegalMaskBeamSearch(), test_only=self.test_only)


# Public interface names requested by the provider integration contract.
GuandanModelRuntime = GuandanRuntime
TokenActionDecoder = GuandanTokenActionDecoder
