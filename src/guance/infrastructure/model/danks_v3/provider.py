"""DanKS V3 provider: real-weight ModelAdvisor over the phase 2/3 core.

Contract notes (phase 1 frozen design, adapted to the manifest identifier
charset which has no ``+``):

- input contract stays the canonical v1 view; the declaration-facts
  capability is declared via ``runtime_contract = "danks.declared-facts.v1"``;
- the family is evaluation-only: production/strength claims fail closed;
- the runtime is CPU-only for this deployment; a cuda request fails closed
  (no silent device fallback, per the project device policy);
- scores are the policy logits of the ranked slots, explicitly not win
  rates and never comparable across models.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from time import perf_counter
from typing import Any, Mapping

from guance.contracts import (
    ADVICE_CONTRACT, ADVICE_CONTRACT_ALIASES, AdviceCandidate,
    CanonicalAdviceResult, CanonicalPublicGameView, DeclaredFacts,
    ModelCapabilities, ModelMetadata, PUBLIC_GAME_CONTRACT, PUBLIC_GAME_CONTRACT_ALIASES,
    PUBLIC_RULESET, validate_advice_for_view,
)
from ..errors import ModelBackendError
from ..factory import AbstractProviderFactory
from ..manifest import ModelBundle, ModelManifest, sha256_file
# Heavy imports (torch/numpy via loader/core/adapter) stay method-local:
# default_registry() must never eagerly import an optional runtime
# (architecture guard).  The vendored schema module is pure stdlib.
from .source_identity import CHECKPOINT_SHA256
from .vendor.danks.training.schema import FEATURE_VERSION

DANKS_V3_PROVIDER = "danks_v3"
DANKS_V3_BACKEND = "torch"
DANKS_V3_RUNTIME = "torch"
DANKS_V3_RUNTIME_CONTRACT = "danks.v3.torch.cpu.v1"
DANKS_V3_FACTS_CONTRACT = "danks.declared-facts.v1"
DANKS_V3_INPUT_CONTRACT = PUBLIC_GAME_CONTRACT
DANKS_V3_SCORE_KIND = "danks_v3_selector_logit"
DANKS_V3_MODEL_ID = "danks-v3-top10-selector"
DANKS_V3_RULES_VERSION = "danks.gd-rules.c618f537.v1"
DANKS_V3_ACTION_VERSION = "danks.action.v1"
DANKS_V3_ENVIRONMENT_VERSION = "danks.environment.v1"
DANKS_V3_ALGORITHM = "structured-topk-selector"
DANKS_V3_ARCHITECTURE = "EfficientTeamBeliefTop10Selector"
DANKS_V3_ALLOWED_CLASSIFICATIONS = frozenset({"experimental", "evaluation_only"})
DANKS_V3_EXPECTED_READINESS = {"experimental": True, "production_ready": False,
                               "strength_claim": False}
DANKS_V3_ARTIFACT_FORMAT = "pt"

# DanKS kind -> shared-validator kind and the Chinese display label.
_KIND_TO_VALIDATOR = {"Single": "single", "Pair": "pair", "Triple": "triple",
                      "TriplePlus": "full", "Straight": "straight",
                      "StraightPair": "serial_pair", "StraightTriple": "serial_triple",
                      "Bomb": "bomb", "StraightFlush": "sflush",
                      "FourKings": "rocket", "PASS": "pass"}
_KIND_LABELS = {"Single": "单张", "Pair": "对子", "Triple": "三张",
                "TriplePlus": "三带二", "Straight": "顺子",
                "StraightPair": "三连对", "StraightTriple": "钢板",
                "Bomb": "炸弹", "StraightFlush": "同花顺",
                "FourKings": "四王炸", "PASS": "不出"}


class DanksV3ProviderError(ValueError):
    """Provider-boundary failure; carries a stable wire-visible code."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


def validate_danks_v3_claims(manifest: ModelManifest) -> None:
    if manifest.classification is None and manifest.readiness is None:
        return
    if (manifest.classification not in DANKS_V3_ALLOWED_CLASSIFICATIONS
            or manifest.readiness is None
            or dict(manifest.readiness) != DANKS_V3_EXPECTED_READINESS):
        raise ModelBackendError("CLAIM_UNSUPPORTED")


class DanksV3Advisor:
    """Real-weight advisor; loaded only inside the dedicated worker process."""

    supports_declared_facts = True

    def __init__(self, bundle: ModelBundle, *, requested_device: str = "auto") -> None:
        self.bundle = bundle
        self._requested_device = requested_device
        self._loaded: Any = None
        self._engine: Any = None
        self._closed = False

    def load(self) -> ModelMetadata:
        if self._closed:
            raise ModelBackendError("MODEL_CLOSED")
        manifest = self.bundle.manifest
        self.bundle.verify()
        if self._requested_device not in ("auto", "cpu"):
            raise ModelBackendError("MODEL_UNAVAILABLE")  # CPU-only deployment
        if self._loaded is None:
            from .loader import DanksV3LoaderError, load_selector
            try:
                self._loaded = load_selector(
                    self.bundle.artifact_path,
                    expected_sha256=manifest.artifact_sha256)
            except DanksV3LoaderError as exc:
                raise ModelBackendError(exc.code) from None
            self.bundle.verify()
        # Restricted-A optimization: pre-spawn the partition pool at load
        # time so the first opening request stays inside its budget.
        from .optimizations import prewarm_parallel_partitions
        prewarm_parallel_partitions()
        return ModelMetadata(
            status="loaded", provider=manifest.provider, backend=manifest.backend,
            runtime=manifest.runtime, runtime_version="",
            runtime_contract=manifest.runtime_contract or DANKS_V3_RUNTIME_CONTRACT,
            model_version=manifest.model_version, iteration=0,
            model_sha256=manifest.artifact_sha256,
            bundle_fingerprint=manifest.artifact_sha256,
            score_kind=DANKS_V3_SCORE_KIND, test_only=False, experimental=True,
            public_self_supported=True,
            requested_device=self._requested_device, actual_device="cpu",
            device_fallback=False, device_reason="cpu_only_runtime",
            device_evidence="danks.v3.cpu.loader.no.gpu.runtime",
            input_contract=manifest.input_contract,
            output_contract=manifest.output_contract,
            wire_input_contract=PUBLIC_GAME_CONTRACT,
            wire_output_contract=ADVICE_CONTRACT,
            capabilities=manifest.capabilities if isinstance(manifest.capabilities,
                                                             Mapping) else {})

    def recommend(self, view: CanonicalPublicGameView, request_id: str,
                  declared_facts: DeclaredFacts | None = None) -> CanonicalAdviceResult:
        if self._closed:
            raise ModelBackendError("MODEL_CLOSED")
        if self._loaded is None:
            raise ModelBackendError("MODEL_NOT_LOADED")
        started = perf_counter()
        view.validate()
        # D04 provenance gate (requirement 2026-11-21): verify fact status,
        # the ACTUAL boolean value, the trusted source and the corresponding
        # game position.  known(False) = CONFIRMED discontinuous; unknown/
        # unsupported = unverifiable; both fail closed.
        if declared_facts is None:
            raise ModelBackendError("MODEL_INPUT_UNSUPPORTED")
        if declared_facts.schema != "Guandan.DeclaredFacts.v2":
            raise ModelBackendError("MODEL_INPUT_UNSUPPORTED")
        origin = declared_facts.history_origin
        if origin not in ("synthetic_normal_entry", "live_capture"):
            raise ModelBackendError("MODEL_INPUT_UNSUPPORTED")
        if (declared_facts.public_history_complete != "known"
                or declared_facts.history_complete_value is not True
                or declared_facts.transport_contiguous != "known"
                or declared_facts.transport_value is not True):
            raise ModelBackendError("MODEL_INPUT_UNSUPPORTED")
        # Corresponding-game check 1: any seat with fewer than 27 cards while
        # the history is EMPTY is a mid-join - the claimed continuity cannot
        # be true because cards have been played that we cannot account for.
        if not view.public_history and any(
                count < 27 for count in view.remaining):
            raise ModelBackendError("MODEL_INPUT_UNSUPPORTED")
        # Corresponding-game check 2: contiguous action indices.
        if view.public_history:
            indices = [action.index for action in view.public_history]
            if indices != list(range(1, len(indices) + 1)):
                raise ModelBackendError("MODEL_INPUT_UNSUPPORTED")
        # Corresponding-game check 3: per-seat remaining-count reconciliation.
        # When a complete chain is claimed, each seat's remaining count must
        # equal 27 minus the number of cards that seat played in the history.
        if view.public_history:
            seat_played = [0, 0, 0, 0]
            for action in view.public_history:
                if action.kind == "play":
                    seat_played[action.player] += len(action.cards)
            for seat_i in range(4):
                expected = 27 - seat_played[seat_i]
                if view.remaining[seat_i] != expected:
                    raise ModelBackendError("MODEL_INPUT_UNSUPPORTED")
        # Corresponding-game check 4: actor-chain integrity per trick,
        # mirroring the domain state machine (domain/state.py) exactly:
        # every play OVERWRITES lead_action (it is the CURRENT winning
        # action, so an overtaken lead inside the current trick is normal),
        # a trick closes when every seat still holding cards has passed
        # since that play, the next leader is the winner or - when the
        # winner went out with the closing play - their teammate
        # ((winner+2)%4, the state machine's inherited lead / 接风), and
        # rotation skips seats that are out of cards AT THAT MOMENT, not at
        # the end of the game.  Alive-ness is therefore walked incrementally
        # from the dealt 27 cards per seat (accepted by the 38 frozen
        # samples and the acceptance full-game case, 2026-11-21).  A deleted
        # or renumbered PASS breaks exactly one of these invariants:
        #   - mid-chain deletion: the consecutive pair's expected actor is
        #     the deleted seat;
        #   - last deletion of a closed past trick: the replayed trick never
        #     closes;
        #   - last deletion of the open current trick: the chain no longer
        #     ends at the observer.
        remaining_now = [27, 27, 27, 27]

        def _expected_actor(after_player: int) -> int | None:
            for step in range(1, 4):
                seat = (after_player - step) % 4
                if remaining_now[seat] > 0:
                    return seat
            return None

        tricks: dict[int, list] = {}
        for action in view.public_history:
            tricks.setdefault(action.trick, []).append(action)
        # The current trick is either a free lead (nothing recorded yet) or
        # an active follow whose chain's LAST play is lead_action.
        if view.lead_action is not None:
            current_chain = tricks.get(view.trick)
            if current_chain is None or current_chain[0].kind != "play":
                raise ModelBackendError("MODEL_INPUT_UNSUPPORTED")
            current_plays = [a for a in current_chain if a.kind == "play"]
            if (not current_plays
                    or current_plays[-1].index != view.lead_action.index):
                raise ModelBackendError("MODEL_INPUT_UNSUPPORTED")
        elif view.trick in tricks:
            raise ModelBackendError("MODEL_INPUT_UNSUPPORTED")
        prev_winner: int | None = None
        for t_i, (t_key, actions) in enumerate(sorted(tricks.items())):
            is_current = (t_key == view.trick
                          and view.lead_action is not None)
            # Every trick opens with its leader's play (a leader may never
            # pass); a complete from-entry history begins with first_seat
            # leading trick 0 (domain/state.py: first_seat == current_seat
            # at the start event).
            if actions[0].kind != "play":
                raise ModelBackendError("MODEL_INPUT_UNSUPPORTED")
            if t_i == 0 and (t_key != 0
                             or actions[0].player != view.first_seat):
                raise ModelBackendError("MODEL_INPUT_UNSUPPORTED")
            # The previous trick's winner leads the next trick; when they
            # went out with the closing play, the lead is inherited by their
            # teammate (接风).  A wholly deleted trick breaks this hand-off.
            if prev_winner is not None:
                expected_leader = (prev_winner
                                   if remaining_now[prev_winner] > 0
                                   else (prev_winner + 2) % 4)
                if (remaining_now[expected_leader] <= 0
                        or actions[0].player != expected_leader):
                    raise ModelBackendError("MODEL_INPUT_UNSUPPORTED")
            winner = None
            responded: set[int] = set()
            closed = False
            for j, action in enumerate(actions):
                if j:
                    expected = _expected_actor(actions[j - 1].player)
                    if expected is None or action.player != expected:
                        raise ModelBackendError("MODEL_INPUT_UNSUPPORTED")
                if action.kind == "play":
                    remaining_now[action.player] -= len(action.cards)
                    winner, responded, closed = action.player, set(), False
                else:
                    responded.add(action.player)
                    eligible = {seat for seat in range(4)
                                if remaining_now[seat] > 0 and seat != winner}
                    if winner is not None and eligible <= responded:
                        closed = True
                        if j != len(actions) - 1:
                            raise ModelBackendError("MODEL_INPUT_UNSUPPORTED")
                # Team end (a team's both seats out) terminates the state
                # machine (phase="ended", current_seat=None): such a state
                # never produces an advice request, so no view history may
                # contain it.
                if any(remaining_now[s] == 0
                       and remaining_now[(s + 2) % 4] == 0 for s in (0, 1)):
                    raise ModelBackendError("MODEL_INPUT_UNSUPPORTED")
            if closed:
                if is_current:
                    # A closed current trick is already in the past for the
                    # state machine (it advanced the trick and reset to a
                    # free lead); a view claiming it as the active trick is
                    # forged.
                    raise ModelBackendError("MODEL_INPUT_UNSUPPORTED")
                prev_winner = winner
            elif is_current:
                # The open current trick must end exactly at the observer:
                # the advice request happens at their turn.
                if _expected_actor(actions[-1].player) != view.my_seat:
                    raise ModelBackendError("MODEL_INPUT_UNSUPPORTED")
            else:
                raise ModelBackendError("MODEL_INPUT_UNSUPPORTED")
        declared_facts.validate_for_view(view)
        from .adapter import (DanksV3AdapterError, declaration_interpretations, legal_follow_candidates,
                              legal_lead_candidates, observation_context,
                              wire_cards_to_labels)
        target_declaration = self._resolve_target(view, declared_facts,
                                                  declaration_interpretations)
        hand_labels = wire_cards_to_labels(view.hand)
        try:
            context = observation_context(view, target_declaration, declared_facts)
            if target_declaration is None:
                legal = legal_lead_candidates(hand_labels, view.level)
            else:
                legal = legal_follow_candidates(hand_labels, view.level, target_declaration)
        except DanksV3AdapterError as exc:
            if exc.code == "FOLLOW_TARGET_UNSUPPORTED":
                raise ModelBackendError("MODEL_INPUT_UNSUPPORTED") from None
            raise
        core_result = self._recommend_core(hand_labels, context, legal)
        candidates = self._display_candidates(view, core_result)
        result = CanonicalAdviceResult(
            view.session_id, view.game_id, view.state_version, request_id,
            candidates=candidates,
            elapsed_ms=(perf_counter() - started) * 1000,
            model_fingerprint=self.bundle.manifest.artifact_sha256,
            score_kind=DANKS_V3_SCORE_KIND,
            provider=self.bundle.manifest.provider,
            model_version=self.bundle.manifest.model_version)
        validate_advice_for_view(view, result, declared_facts=declared_facts)
        return result

    def _recommend_core(self, hand_labels, context, legal):
        from .core import RecommendationEngine
        if self._engine is None:
            self._engine = RecommendationEngine(self._loaded)
        return self._engine.recommend(hand_labels, context, legal)

    def _resolve_target(self, view: CanonicalPublicGameView,
                        declared_facts: DeclaredFacts | None,
                        declaration_interpretations):
        """Confirmed declaration first; otherwise the ambiguity oracle."""
        fact = declared_facts.current_winning_declaration if declared_facts is not None \
            else None
        if view.free_lead:
            if fact is not None and fact.status == "known":
                raise DanksV3ProviderError("MODEL_INPUT_INVALID",
                                           "confirmed declaration on a free lead")
            return None
        if fact is not None and fact.status == "known":
            return fact.declaration
        interpretations = declaration_interpretations(
            tuple(view.lead_action.cards), view.level)
        if len(interpretations) == 1:
            return interpretations[0]
        if fact is not None and fact.status == "absent":
            raise DanksV3ProviderError("MODEL_INPUT_INVALID",
                                       "absent declaration on a follow state")
        raise ModelBackendError("MODEL_INPUT_AMBIGUOUS")

    def _display_candidates(self, view: CanonicalPublicGameView,
                            core_result) -> tuple[AdviceCandidate, ...]:
        """Model-ranked display candidates (<=5) with entity-card mapping.

        Rows are ordered by their policy logits (the selector's own ranking);
        each DanKS label is mapped back to concrete entity codes from the
        observer hand, deterministically lowest-code-first within one row.
        Scores are raw policy logits - explicitly not win rates.
        """
        from .adapter import wire_cards_to_labels
        paired = sorted(zip(core_result.logits, core_result.top10),
                        key=lambda pair: -pair[0])[:5]
        label_codes: dict[str, list[int]] = {}
        for code in view.hand:
            label = wire_cards_to_labels([code])[0]
            label_codes.setdefault(label, []).append(code)
        for codes in label_codes.values():
            codes.sort()
        candidates = []
        for order, (logit, row) in enumerate(paired, 1):
            kind = _KIND_TO_VALIDATOR.get(row.kind)
            if kind is None:
                raise ModelBackendError("MODEL_OUTPUT_INVALID")
            codes: list[int] = []
            taken: set[int] = set()
            for label in row.cards:
                available = [code for code in label_codes.get(label, [])
                             if code not in taken]
                if not available:
                    raise ModelBackendError("MODEL_OUTPUT_INVALID")
                chosen = available[0]
                taken.add(chosen)
                codes.append(chosen)
            candidates.append(AdviceCandidate(
                cards=tuple(sorted(codes)), kind=kind,
                label=_KIND_LABELS.get(row.kind, row.kind), score=float(logit),
                score_kind=DANKS_V3_SCORE_KIND, rank=order,
                metadata={"model_version": self.bundle.manifest.model_version}))
        return tuple(candidates)

    def recommend_confirmed(self, decision, request_id: str) -> CanonicalAdviceResult:
        raise ModelBackendError("REQUEST_KIND_UNSUPPORTED")

    def close(self) -> None:
        self._closed = True
        self._loaded = None
        self._engine = None


class DanksV3Provider(AbstractProviderFactory):
    def validate(self, manifest: ModelManifest) -> None:
        if manifest.provider != DANKS_V3_PROVIDER or manifest.backend != DANKS_V3_BACKEND:
            raise ModelBackendError("BACKEND_UNSUPPORTED")
        if manifest.runtime != DANKS_V3_RUNTIME:
            raise ModelBackendError("RUNTIME_UNSUPPORTED")
        if (manifest.input_contract not in PUBLIC_GAME_CONTRACT_ALIASES
                or manifest.output_contract not in ADVICE_CONTRACT_ALIASES):
            raise ModelBackendError("CONTRACT_UNSUPPORTED")
        if (manifest.runtime_contract is not None
                and manifest.runtime_contract != DANKS_V3_RUNTIME_CONTRACT
                and manifest.runtime_contract != DANKS_V3_FACTS_CONTRACT):
            raise ModelBackendError("RUNTIME_UNSUPPORTED")
        if manifest.artifact_format != DANKS_V3_ARTIFACT_FORMAT:
            raise ModelBackendError("MODEL_VERSION_UNSUPPORTED")
        expected = {"rules_version": DANKS_V3_RULES_VERSION,
                    "action_version": DANKS_V3_ACTION_VERSION,
                    "environment_version": DANKS_V3_ENVIRONMENT_VERSION,
                    "encoding_version": FEATURE_VERSION,
                    "algorithm": DANKS_V3_ALGORITHM,
                    "architecture": DANKS_V3_ARCHITECTURE}
        if any(getattr(manifest, name) != value for name, value in expected.items()):
            raise ModelBackendError("MODEL_VERSION_UNSUPPORTED")
        validate_danks_v3_claims(manifest)

    def validate_discovery(self, manifest: ModelManifest) -> None:
        """Family-level discovery: the stable contract subset only, so future
        same-contract weights stay discoverable without code changes."""
        if manifest.provider != DANKS_V3_PROVIDER or manifest.backend != DANKS_V3_BACKEND:
            raise ModelBackendError("BACKEND_UNSUPPORTED")
        if (manifest.input_contract not in PUBLIC_GAME_CONTRACT_ALIASES
                or manifest.output_contract not in ADVICE_CONTRACT_ALIASES):
            raise ModelBackendError("CONTRACT_UNSUPPORTED")
        if manifest.runtime_contract is not None and \
                manifest.runtime_contract not in (DANKS_V3_RUNTIME_CONTRACT,
                                                  DANKS_V3_FACTS_CONTRACT):
            raise ModelBackendError("RUNTIME_UNSUPPORTED")
        validate_danks_v3_claims(manifest)

    def create_observation_adapter(self) -> Any:
        return None  # the adapter functions are module-level in danks_v3

    def create_runtime(self, bundle: ModelBundle, *,
                       requested_device: str = "auto") -> Any:
        return None  # the loader inside DanksV3Advisor owns the runtime

    def create(self, bundle: ModelBundle, *,
               requested_device: str = "auto") -> DanksV3Advisor:
        self.validate(bundle.manifest)
        return DanksV3Advisor(bundle, requested_device=requested_device)


def build_bundle(checkpoint_path: str | Path, bundle_root: str | Path) -> ModelBundle:
    """Materialize a schema 2 catalog bundle for the frozen checkpoint.

    Copies the checkpoint bytes unchanged, writes the manifest with the
    original SHA-256 and a SHA256SUMS sidecar.  The manifest identity is the
    phase 1 frozen checkpoint; any other weights fail the loader.
    """
    source = Path(checkpoint_path)
    if not source.is_file():
        raise ModelBackendError("ARTIFACT_MISSING")
    digest = sha256_file(source)
    if digest != CHECKPOINT_SHA256:
        raise ModelBackendError("ARTIFACT_HASH_MISMATCH")
    root = Path(bundle_root)
    artifact_dir = root / "weights"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact = artifact_dir / "top10_selector_v3_online.pt"
    if not artifact.exists() or sha256_file(artifact) != digest:
        shutil.copyfile(source, artifact)
    manifest = ModelManifest.from_dict({
        "schema_version": 2,
        "model_id": DANKS_V3_MODEL_ID,
        "backend": DANKS_V3_BACKEND,
        "provider": DANKS_V3_PROVIDER,
        "artifact": "weights/top10_selector_v3_online.pt",
        "artifact_format": DANKS_V3_ARTIFACT_FORMAT,
        "artifact_sha256": digest,
        "artifact_bytes": source.stat().st_size,
        "input_contract": DANKS_V3_INPUT_CONTRACT,
        "output_contract": ADVICE_CONTRACT,
        "runtime_contract": DANKS_V3_FACTS_CONTRACT,
        "model_version": FEATURE_VERSION,
        "training_version": "online-unknown",
        "rules_version": DANKS_V3_RULES_VERSION,
        "action_version": DANKS_V3_ACTION_VERSION,
        "environment_version": DANKS_V3_ENVIRONMENT_VERSION,
        "encoding_version": FEATURE_VERSION,
        "algorithm": DANKS_V3_ALGORITHM,
        "architecture": DANKS_V3_ARCHITECTURE,
        "runtime": DANKS_V3_RUNTIME,
        "classification": "experimental",
        "readiness": {"experimental": True, "production_ready": False,
                      "strength_claim": False},
        "capabilities": {"recommend": True, "top_n": True, "complete_action": False,
                         "next_token_policy": False, "confirmed_decision": False,
                         "beam_search": False, "belief": False, "search": False,
                         "public_plus_self": True},
    })
    checksums = root / "SHA256SUMS"
    checksums.write_text(f"{digest} *weights/top10_selector_v3_online.pt\n",
                         encoding="utf-8")
    (root / "manifest.json").write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    bundle = ModelBundle(manifest, root)
    bundle.verify()
    return bundle
