"""Phase 5 offline acceptance runner: fixed real inputs, dual provider.

Runs, over the fixed read-only capture and the Phase 2 frozen runtime plus the
real iter2000 checkpoint:

* an input gate (capture/sidecar/bundle/runtime/manifest hashes);
* strict provider/validator eligibility for every confirmed decision;
* a real JSONL-worker recommendation for every strictly eligible decision, with
  per-recommendation legality/own-hand/declaration/wild/beat/pass/identity checks;
* a real FableDan public-view regression on the real ``best.npz``;
* negative cases on the real worker (missing history, missing legal rows, cache
  and counter mismatch, missing declaration) plus real bundle/checkpoint tamper;
* an A/B comparison restricted to identity/status/legality/latency/failure/score_kind.

Every evidence value is bounded and anonymous (identity hashes, counters, result
summaries). No frames, raw payloads, hidden hands, card ids, tensors, credentials
or private absolute paths are written. ``offline`` is never reported as ``live``.

The command is reproducible: ``--evidence-root`` selects an empty/authorized evidence
directory; a verifier may redirect it to a system temporary directory.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT / "src"))
sys.dont_write_bytecode = True

from guance.contracts import (  # noqa: E402
    CanonicalPublicGameView, ConfirmedDecisionRecommendationRequest, GameObservation,
    PublicRecommendationRequest, RequestIdentity, validate_advice_for_view,
)
from guance.domain.session import SessionEngine  # noqa: E402
from guance.infrastructure.capture.live_subset import NormalExchangeLiveSubsetProvider  # noqa: E402
from guance.infrastructure.model.client import InferenceClient  # noqa: E402
from guance.infrastructure.model.codec import dumps, envelope, read_message  # noqa: E402
from guance.infrastructure.model.errors import ModelBackendError  # noqa: E402
from guance.infrastructure.model.marvel_v2.decision_request import (  # noqa: E402
    decision_identity_sha256, parse_confirmed_decision, project_confirmed_decision,
)
from guance.infrastructure.model.marvel_v2.trick_replay import replay_trick_topology  # noqa: E402
from guance.infrastructure.model.marvel_v2.rules_oracle import (  # noqa: E402
    VerifiedGuandanRulesOracle,
)
from guance.public_state_contract import guandan_card_id_to_wire  # noqa: E402

PHASE2 = CODE_ROOT / "build" / "acceptance" / "guance-model-platform-20261016" / "phase2"
RUNTIME_ROOT = PHASE2 / "runtime" / "marvel-e5c82e7c-d7acbc8d"
RUNTIME_SOURCE = RUNTIME_ROOT / "source"
BUNDLE = (CODE_ROOT / "build" / "acceptance" / "guance-model-platform-20261016"
          / "phase3" / "bundles" / "marvel-v2-iter002000-confirmed")
CHECKPOINT = Path(r"D:\project\guandan\runs\comparison_delivery\dataset\m09_candidate_iter002000.pt")
CAPTURE_ID = "20261014-143204"
CAPTURE = CODE_ROOT / "debug" / "captures" / CAPTURE_ID / "frames.jsonl"
SIDECAR = CODE_ROOT / "debug" / "captures" / CAPTURE_ID / "capture_status.json"
GAMES_DIR = CODE_ROOT / "debug" / "captures" / CAPTURE_ID / "games"
BEST_NPZ = CODE_ROOT / "data" / "profiles" / "tencent_daguandan" / "models" / "best.npz"
ROOT_BEST_NPZ = CODE_ROOT / "models" / "best.npz"
OBSERVATION_FIXTURE = CODE_ROOT / "tests" / "fixtures" / "observations.json"

EXPECTED_CAPTURE_SHA256 = "29e91cf3ffc3169bb671b238fe7549944e60a9bc8b0a9a61d47192f6ba0e5e48"
EXPECTED_CAPTURE_BYTES = 934179
EXPECTED_CAPTURE_FRAMES = 1682
EXPECTED_SIDECAR_SHA256 = "c51864b4e016a4671190ef952431b04dd7b8ec5d79add148f2d5a9301d1bc21a"
EXPECTED_CHECKPOINT_SHA256 = "af0df1eaeb67c88e467ee78bf6b0494c49d000f5733d655ee81b40351d4ca9c8"
EXPECTED_CHECKPOINT_BYTES = 8588181
EXPECTED_DEPLOYMENT_SHA256 = "44c69bd301c9883e5dc1b25268b26037a61c949d79d9d529cef7beca6fdf2f22"
EXPECTED_DEPLOYMENT_BYTES = 40384
EXPECTED_RUNTIME_ID = "marvel-e5c82e7c27fb-d7acbc8d76c9"
EXPECTED_RUNTIME_CONTRACT = "marvel_v2.cpu.v1"
EXPECTED_RUNTIME_ENTRIES = 189
EXPECTED_BEST_NPZ_SHA256 = "86570527c988026f74e21e84bf2f50876ede1d38c962d196e444115cf3571310"
MODEL_VERSION = "MARVEL-V2-v1.3-iter002000"
MARVEL_SCORE_KIND = "marvel_v2_policy_path_score"
FABLEDAN_SCORE_KIND = "fabledan_q"
EXPECTED_PROVIDER_ELIGIBLE = 34
EXPECTED_STRICT_ELIGIBLE = 32
EXPECTED_STRICT_INELIGIBLE = 2

_FAMILY_KIND = {
    "SINGLE": "single", "PAIR": "pair", "TRIPLE": "triple", "FULL_HOUSE": "full",
    "STRAIGHT": "straight", "STRAIGHT_FLUSH": "sflush", "PAIR_SEQUENCE": "serial_pair",
    "TRIPLE_SEQUENCE": "serial_triple", "RANK_BOMB": "bomb", "FOUR_KINGS": "rocket",
}

_FORBIDDEN_OUTPUT_KEYS = {
    "authorization", "cookie", "cookies", "ca_private_key", "raw", "raw_data",
    "request_body", "response_body", "post_state", "outcome", "reward", "payload",
    "payload_b64", "hidden_hand", "hidden_hands", "hidden_opponent_hands",
    "remaining_hands", "own_hand", "hand", "cards", "public_history", "history",
    "legal_rows", "model_input", "model_output",
}
_SAFE_STRING_MAX = 256
_ALLOWED_BOUNDED_CODES = {
    "MODEL_INPUT_INVALID", "MODEL_INPUT_UNSUPPORTED", "MODEL_INFERENCE_FAILED",
    "MODEL_OUTPUT_INVALID", "MODEL_UNAVAILABLE", "MODEL_NOT_LOADED", "MODEL_LOAD_FAILED",
    "MODEL_TIMEOUT", "REQUEST_KIND_UNSUPPORTED", "PROTOCOL_INVALID",
    "LIVE_DECLARATION_AMBIGUOUS", "LIVE_EXCHANGE_VARIANT_UNSUPPORTED",
    "PREVIOUS_RESULT_ASSOCIATION_UNSUPPORTED", "CONFIRMED_DECISION_UNAVAILABLE",
    "frame_sequence_gap", "capture_paused",
}


def _fail(code: str) -> None:
    raise RuntimeError(code)


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _hash_json(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False)
                          .encode("utf-8")).hexdigest()


def _line_count(path: Path) -> int:
    count = 0
    with path.open("rb") as stream:
        for _ in stream:
            count += 1
    return count


def validate_safe_output(value: object) -> None:
    """Reject content-bearing evidence; only bounded aggregate metadata is allowed."""
    def walk(item: object):
        if isinstance(item, dict):
            for key, child in item.items():
                yield str(key)
                yield from walk(child)
        elif isinstance(item, list):
            for child in item:
                yield from walk(child)
    if _FORBIDDEN_OUTPUT_KEYS.intersection(key.lower() for key in walk(value)):
        _fail("PHASE5_PRIVACY_CONTRACT_INVALID")

    def visit(item: object) -> None:
        if item is None or type(item) in {bool, int, float}:
            return
        if isinstance(item, str):
            if len(item) > _SAFE_STRING_MAX or "\r" in item or "\n" in item:
                _fail("PHASE5_PRIVACY_CONTRACT_INVALID")
            if ":\\" in item or item.startswith(("/", "\\\\")):
                _fail("PHASE5_PRIVACY_CONTRACT_INVALID")
            return
        if isinstance(item, list):
            if len(item) > 512:
                _fail("PHASE5_PRIVACY_CONTRACT_INVALID")
            for child in item:
                visit(child)
            return
        if isinstance(item, dict):
            if len(item) > 64:
                _fail("PHASE5_PRIVACY_CONTRACT_INVALID")
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 64:
                    _fail("PHASE5_PRIVACY_CONTRACT_INVALID")
                visit(child)
            return
        _fail("PHASE5_PRIVACY_CONTRACT_INVALID")

    visit(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(encoded.encode("utf-8")) > 512 * 1024:
        _fail("PHASE5_PRIVACY_CONTRACT_INVALID")


@contextlib.contextmanager
def _env_scope(**values: str | None):
    saved = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _bounded_code(value: object, default: str = "MODEL_INPUT_INVALID") -> str:
    code = value if isinstance(value, str) else default
    return code if code in _ALLOWED_BOUNDED_CODES else default


# ---------------------------------------------------------------------------
# Input gate
# ---------------------------------------------------------------------------

def verify_inputs() -> dict[str, Any]:
    """Read-only verification of every fixed input; returns bounded metadata."""
    if ROOT_BEST_NPZ.exists():
        _fail("UNEXPECTED_ROOT_BEST_NPZ")
    capture_sha, capture_bytes = _sha256_file(CAPTURE)
    sidecar_sha, sidecar_bytes = _sha256_file(SIDECAR)
    if capture_sha != EXPECTED_CAPTURE_SHA256 or capture_bytes != EXPECTED_CAPTURE_BYTES:
        _fail("CAPTURE_IDENTITY_MISMATCH")
    if sidecar_sha != EXPECTED_SIDECAR_SHA256:
        _fail("SIDECAR_IDENTITY_MISMATCH")
    sidecar = json.loads(SIDECAR.read_text(encoding="utf-8"))
    frames = _line_count(CAPTURE)
    closed = (sidecar.get("schema_version") == 1 and sidecar.get("state") == "closed"
              and sidecar.get("source_sha256") == capture_sha
              and sidecar.get("frames") == frames == EXPECTED_CAPTURE_FRAMES
              and sidecar.get("bytes") == capture_bytes
              and sidecar.get("hosts") == {"hlxyxws.huanle.qq.com": frames}
              and isinstance(sidecar.get("connection_epochs"), list)
              and len(sidecar["connection_epochs"]) == 1
              and isinstance(sidecar.get("games"), list) and sidecar["games"])
    if not closed:
        _fail("CAPTURE_SIDECAR_INVALID")
    game_files = sorted(path.relative_to(GAMES_DIR).as_posix()
                        for path in GAMES_DIR.rglob("*") if path.is_file())
    if not game_files:
        _fail("CAPTURE_GAME_ARTIFACTS_MISSING")

    runtime = json.loads((RUNTIME_ROOT / "runtime.json").read_text(encoding="utf-8"))
    if (runtime.get("schema") != "guance-marvel-v2-runtime-v1"
            or runtime.get("runtime_id") != EXPECTED_RUNTIME_ID
            or runtime.get("runtime_contract") != EXPECTED_RUNTIME_CONTRACT):
        _fail("RUNTIME_IDENTITY_MISMATCH")
    entries = 0
    for line in (RUNTIME_ROOT / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, relative = line.partition("  ")
        target = RUNTIME_ROOT / relative
        if not target.is_file() or _sha256_file(target)[0] != digest:
            _fail("RUNTIME_SHA256SUMS_MISMATCH")
        entries += 1
    pycache = sum(1 for path in RUNTIME_ROOT.rglob("__pycache__") if path.is_dir())
    if entries != EXPECTED_RUNTIME_ENTRIES or pycache != 0:
        _fail("RUNTIME_INTEGRITY_INVALID")

    manifest = json.loads((BUNDLE / "manifest.json").read_text(encoding="utf-8"))
    deployment_sha, deployment_bytes = _sha256_file(BUNDLE / "deployment.json")
    if (manifest.get("provider") != "guandan_marvel_v2"
            or manifest.get("model_version") != MODEL_VERSION
            or manifest.get("artifact") != "deployment.json"
            or manifest.get("artifact_sha256") != deployment_sha
            or deployment_sha != EXPECTED_DEPLOYMENT_SHA256
            or manifest.get("artifact_bytes") != deployment_bytes != EXPECTED_DEPLOYMENT_BYTES
            or manifest.get("classification") != "evaluation_only"
            or manifest.get("readiness", {}).get("production_ready") is not False
            or manifest.get("readiness", {}).get("strength_claim") is not False
            or manifest.get("capabilities", {}).get("confirmed_decision") is not True):
        _fail("BUNDLE_IDENTITY_MISMATCH")

    checkpoint_sha, checkpoint_bytes = _sha256_file(CHECKPOINT)
    if checkpoint_sha != EXPECTED_CHECKPOINT_SHA256 or checkpoint_bytes != EXPECTED_CHECKPOINT_BYTES:
        _fail("CHECKPOINT_IDENTITY_MISMATCH")
    best_sha, _ = _sha256_file(BEST_NPZ)
    if best_sha != EXPECTED_BEST_NPZ_SHA256:
        _fail("FABLEDAN_WEIGHTS_MISMATCH")
    return {
        "schema": "guance-phase5-input-gate-v1",
        "provenance": "offline_fixed_inputs",
        "live_input_used": False,
        "real_input_used": True,
        "capture_id": CAPTURE_ID,
        "capture_sha256": capture_sha,
        "capture_bytes": capture_bytes,
        "capture_frames": frames,
        "capture_sidecar_sha256": sidecar_sha,
        "capture_sidecar_state": str(sidecar.get("state")),
        "capture_game_artifacts": len(game_files),
        "runtime_id": EXPECTED_RUNTIME_ID,
        "runtime_contract": EXPECTED_RUNTIME_CONTRACT,
        "runtime_sha256sums_entries": entries,
        "runtime_sha256sums_verified": True,
        "runtime_pycache_directories": pycache,
        "bundle_manifest_sha256": _sha256_file(BUNDLE / "manifest.json")[0],
        "deployment_sha256": deployment_sha,
        "deployment_bytes": deployment_bytes,
        "model_version": MODEL_VERSION,
        "checkpoint_sha256": checkpoint_sha,
        "checkpoint_bytes": checkpoint_bytes,
        "fabledan_weights_sha256": best_sha,
        "field_synthesis_used": False,
    }


# ---------------------------------------------------------------------------
# Capture replay and strict eligibility
# ---------------------------------------------------------------------------

class _EnvProxy:
    """Minimal monkeypatch-compatible environment proxy for shared helpers."""

    def setenv(self, key: str, value: str) -> None:
        os.environ[key] = value

    def delenv(self, key: str) -> None:
        os.environ.pop(key, None)


def replay_capture() -> dict[str, Any]:
    before_hash, before_bytes = _sha256_file(CAPTURE)
    provider = NormalExchangeLiveSubsetProvider(RUNTIME_SOURCE)
    provider.set_model_artifact_identity_provider(lambda: (MODEL_VERSION, EXPECTED_CHECKPOINT_SHA256))
    provider.bind_capture(CAPTURE_ID)
    engine = SessionEngine(
        "phase5-acceptance", aggregate_parser=_parse_aggregate(), accepted_host=_game_host(),
        event_adapter=provider.adapt_event, event_observer=provider.observe_applied,
        reset_observer=provider.reset_session)
    decisions = []
    for line in CAPTURE.open("rb"):
        record = json.loads(line.decode("utf-8-sig"))
        payload = b""
        if record.get("direction") == "server":
            payload = base64.b64decode(record["payload_b64"], validate=True)
        frame = _source_frame(record, payload)
        engine.feed(frame)
        decisions.extend(provider.drain_decisions())
    after_hash, after_bytes = _sha256_file(CAPTURE)
    return {
        "decisions": decisions,
        "provider_eligible": provider.model_adapter_eligible_count,
        "capture_unchanged": (before_hash == after_hash and before_bytes == after_bytes
                              and before_hash == EXPECTED_CAPTURE_SHA256),
        "capture_sha256": after_hash,
    }


def _parse_aggregate():
    from guance.infrastructure.protocol import live
    return live.parse_aggregate


def _game_host():
    from guance.infrastructure.protocol import live
    return live.GAME_HOST


def _source_frame(record: dict, payload: bytes):
    from guance.contracts import SourceFrame
    return SourceFrame(host=str(record["host"]), payload=payload,
                       received_at=float(record["received_at"]),
                       connection_id=str(record.get("connection_id", "")),
                       sequence=int(record["sequence"]), direction=str(record.get("direction")))


def _decision_identity(decision: Any, payload: dict | None) -> str:
    if payload is not None:
        return str(payload["decision_identity_sha256"])
    value = {
        "game_id": decision.public_state.identity.game_id,
        "state_version": decision.public_state.identity.state_version,
        "history_sha256": decision.history.history_sha256,
        "legal_generation": decision.action_session.legal_rows.rows[0].generation,
        "cache_request_id": decision.cache_request.request_id,
    }
    return _hash_json(value)


def _ineligible_reason(decision: Any) -> str:
    state = decision.marvel_state
    emptied = {seat for seat, count in enumerate(state.remaining_counts) if count == 0}
    if decision.action_session.phase == "FOLLOW_PLAY":
        if state.current_winner_seat is not None and state.current_winner_seat in emptied:
            return "CURRENT_WINNER_EMPTIED"
    if state.active_seat in emptied:
        return "ACTIVE_SEAT_EMPTIED"
    replay = replay_trick_topology(decision.history.steps, remaining_counts=state.remaining_counts)
    if replay.terminal:
        return "TERMINAL_REPLAY"
    if replay.trick_open:
        return "TRICK_NOT_CLOSED"
    return "TOPOLOGY_UNSUPPORTED"


class _Context:
    __slots__ = ("index", "decision_identity", "session_id", "game_id", "state_version",
                 "level", "phase", "own_hand_wire", "candidate_card_sets", "candidates",
                 "has_pass", "has_winning", "current_declaration", "candidate_wild_sets",
                 "payload_hash")

    def __init__(self, index, decision_identity, *, session_id, game_id, state_version,
                 level, phase, own_hand_wire, candidate_card_sets, candidates, has_pass,
                 has_winning, current_declaration, candidate_wild_sets, payload_hash):
        self.index = index
        self.decision_identity = decision_identity
        self.session_id = session_id
        self.game_id = game_id
        self.state_version = state_version
        self.level = level
        self.phase = phase
        self.own_hand_wire = own_hand_wire
        self.candidate_card_sets = candidate_card_sets
        self.candidates = candidates
        self.has_pass = has_pass
        self.has_winning = has_winning
        self.current_declaration = current_declaration
        self.candidate_wild_sets = candidate_wild_sets
        self.payload_hash = payload_hash


def _context(index: int, decision: Any, payload: dict) -> _Context:
    session = decision.action_session
    own_hand_wire = tuple(guandan_card_id_to_wire(card) for card in session.own_hand)
    candidates = tuple(session.candidates)
    candidate_sets = frozenset(
        frozenset(guandan_card_id_to_wire(card) for card in candidate.physical_cards)
        for candidate in candidates)
    wild_sets = {
        frozenset(guandan_card_id_to_wire(card) for card in candidate.physical_cards):
        bool(candidate.declaration is not None and candidate.declaration.wild_assignments)
        for candidate in candidates}
    current = (decision.public_state.current_winning.value.declaration.value
               if decision.public_state.current_winning.status.name == "KNOWN" else None)
    return _Context(
        index, _decision_identity(decision, payload),
        session_id=decision.public_state.identity.session_id,
        game_id=decision.public_state.identity.game_id,
        state_version=decision.public_state.identity.state_version,
        level=decision.marvel_state.level, phase=session.phase,
        own_hand_wire=own_hand_wire, candidate_card_sets=candidate_sets, candidates=candidates,
        has_pass=any(candidate.kind == "pass" for candidate in candidates),
        has_winning=bool(decision.marvel_state.current_winning_cards),
        current_declaration=current, candidate_wild_sets=wild_sets,
        payload_hash=_hash_json(payload))


def classify_decisions(decisions: list) -> dict[str, Any]:
    eligible, ineligible, ineligible_decisions = [], [], []
    for index, decision in enumerate(decisions, start=1):
        try:
            payload = project_confirmed_decision(
                decision, artifact_id=MODEL_VERSION, artifact_sha256=EXPECTED_CHECKPOINT_SHA256)
        except ModelBackendError as exc:
            ineligible.append({
                "index": index,
                "decision_identity": _decision_identity(decision, None),
                "phase": decision.action_session.phase,
                "code": _bounded_code(exc.code),
                "reason": _ineligible_reason(decision),
            })
            ineligible_decisions.append(decision)
            continue
        except Exception:
            ineligible.append({
                "index": index,
                "decision_identity": _decision_identity(decision, None),
                "phase": decision.action_session.phase,
                "code": "MODEL_INPUT_INVALID",
                "reason": "PROJECTION_FAILED",
            })
            ineligible_decisions.append(decision)
            continue
        eligible.append({"index": index, "decision": decision, "payload": payload,
                         "context": _context(index, decision, payload)})
    return {"eligible": eligible, "ineligible": ineligible,
            "ineligible_decisions": ineligible_decisions}


# ---------------------------------------------------------------------------
# Real model client wrapper
# ---------------------------------------------------------------------------

def _wait_for(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class _RealMarvelClient:
    """Synchronous wrapper over the real JSONL worker for sequential requests."""

    def __init__(self, *, on_status_extra=None):
        self.statuses: list[dict] = []
        self.results: list = []
        self.loaded_metadata: dict = {}
        self._on_status_extra = on_status_extra
        self.client = InferenceClient(BUNDLE / "manifest.json", on_result=self.results.append,
                                      on_status=self._on_status, timeout_seconds=600.0, threads=2)

    def _on_status(self, status) -> None:
        item = status.to_dict()
        self.statuses.append(item)
        if self._on_status_extra is not None:
            self._on_status_extra(item)
        if item.get("model_status") == "ready" and item.get("metadata"):
            self.loaded_metadata = dict(item["metadata"])

    def start(self) -> None:
        self.client.start()
        if not _wait_for(lambda: any(item.get("model_status") in
                                     {"ready", "unavailable", "error", "timeout"}
                                     for item in self.statuses), 600):
            _fail("MODEL_LOAD_TIMEOUT")
        if self.statuses[-1]["model_status"] != "ready":
            _fail("MODEL_NOT_READY")

    def submit(self, payload: dict, timeout: float = 420.0):
        count = len(self.results)
        request = ConfirmedDecisionRecommendationRequest(
            RequestIdentity("", payload["session_id"], payload["game_id"],
                            payload["state_version"]), payload)
        request_id = self.client.submit(request, timeout_seconds=timeout)
        if not isinstance(request_id, str) or not request_id:
            _fail("MODEL_REQUEST_REJECTED")
        if not _wait_for(lambda: len(self.results) > count
                         or any(item.get("model_status") in {"error", "timeout"}
                                for item in self.statuses[-4:]), timeout):
            _fail("MODEL_RECOMMENDATION_TIMEOUT")
        if len(self.results) <= count:
            _fail("MODEL_RECOMMENDATION_MISSING")
        return self.results[-1]

    def error_codes(self) -> dict[str, int]:
        codes = Counter(item.get("error_code") for item in self.statuses
                        if item.get("model_status") == "error" and item.get("error_code"))
        return dict(sorted(codes.items()))

    def close(self) -> None:
        self.client.close()


def validate_recommendation(context: _Context, result: Any, oracle: Any) -> dict[str, Any]:
    if (result.session_id, result.game_id, result.state_version) != (
            context.session_id, context.game_id, context.state_version):
        _fail("RESULT_IDENTITY_MISMATCH")
    if result.status != "ready" or result.error_code or result.error_message:
        _fail("RESULT_NOT_READY")
    if result.provider != "guandan_marvel_v2" or result.model_version != MODEL_VERSION:
        _fail("RESULT_PROVIDER_MISMATCH")
    if result.model_fingerprint != EXPECTED_CHECKPOINT_SHA256:
        _fail("RESULT_FINGERPRINT_MISMATCH")
    if result.score_kind != MARVEL_SCORE_KIND:
        _fail("RESULT_SCORE_KIND_MISMATCH")
    if len(result.candidates) != 1:
        _fail("RESULT_CANDIDATE_COUNT_INVALID")
    candidate = result.candidates[0]
    if candidate.rank != 1 or candidate.score_kind != MARVEL_SCORE_KIND:
        _fail("RESULT_CANDIDATE_INVALID")
    if candidate.score is None or not math.isfinite(float(candidate.score)) \
            or float(candidate.score) > 0.0:
        _fail("RESULT_SCORE_INVALID")
    record = {
        "index": context.index,
        "decision_identity": context.decision_identity,
        "phase": context.phase,
        "kind": str(candidate.kind),
        "cards_count": len(tuple(candidate.cards)),
        "wildcard": False,
        "declaration_present": False,
        "beat_validated": True,
        "path_score": float(candidate.score),
        "valid": True,
    }
    if candidate.kind == "pass":
        if tuple(candidate.cards) or not context.has_pass or not context.has_winning \
                or context.phase != "FOLLOW_PLAY" or context.current_declaration is None:
            _fail("RESULT_PASS_INVALID")
        record["kind"] = "pass"
        record["family"] = "pass"
        return record
    cards = tuple(candidate.cards)
    if not cards or len(set(cards)) != len(cards):
        _fail("RESULT_PLAY_CARDS_INVALID")
    if not set(cards) <= set(context.own_hand_wire):
        _fail("RESULT_OWN_HAND_VIOLATION")
    if frozenset(cards) not in context.candidate_card_sets:
        _fail("RESULT_LEGAL_ROW_VIOLATION")
    claimed_kind = str(candidate.kind)
    claimed_ranks = tuple(candidate.claim or ())
    card_matches = [item for item in context.candidates
                    if item.declaration is not None
                    and frozenset(guandan_card_id_to_wire(card) for card in item.physical_cards)
                    == frozenset(cards)]
    if not card_matches:
        _fail("RESULT_DECLARATION_UNAVAILABLE")
    precise = [item for item in card_matches
               if _FAMILY_KIND.get(item.declaration.family) == claimed_kind
               and tuple(item.declaration.declared_ranks) == claimed_ranks]
    if not precise:
        # A wild assignment can change the declared family for the same physical
        # cards; require every card-level interpretation to share the claimed kind.
        precise = [item for item in card_matches
                   if _FAMILY_KIND.get(item.declaration.family) == claimed_kind]
        if not precise:
            _fail("RESULT_FAMILY_MISMATCH")
    wildcard = False
    for item in precise:
        declaration = item.declaration
        declaration.validate(context.level)
        oracle.validate_resolved_play(declaration, wire_level=context.level,
                                      current=context.current_declaration)
        wildcard = wildcard or bool(declaration.wild_assignments)
    record["family"] = claimed_kind
    record["declaration_present"] = True
    record["wildcard"] = wildcard
    return record


def run_marvel_acceptance(classification: dict[str, Any]) -> dict[str, Any]:
    eligible = classification["eligible"]
    if len(eligible) != EXPECTED_STRICT_ELIGIBLE:
        _fail("STRICT_ELIGIBLE_COUNT_MISMATCH")
    oracle = VerifiedGuandanRulesOracle(RUNTIME_SOURCE)
    records = []
    runtime = {}
    with _env_scope(PYTHONDONTWRITEBYTECODE="1", GUANCE_MARVEL_V2_SOURCE=str(RUNTIME_SOURCE),
                    GUANCE_MARVEL_V2_CHECKPOINT=str(CHECKPOINT)):
        client = _RealMarvelClient()
        try:
            client.start()
            metadata = dict(client.loaded_metadata)
            runtime = {
                "load_status": "ready",
                "provider": metadata.get("provider"),
                "model_version": metadata.get("model_version"),
                "model_sha256": metadata.get("model_sha256"),
                "iteration": metadata.get("iteration"),
                "runtime_contract": metadata.get("runtime_contract"),
                "bundle_fingerprint": metadata.get("bundle_fingerprint"),
                "deployment_fingerprint": metadata.get("deployment_fingerprint"),
                "capabilities": dict(metadata.get("capabilities", {})),
                "wire_input_contract": metadata.get("wire_input_contract"),
            }
            if (runtime["provider"] != "guandan_marvel_v2"
                    or runtime["model_version"] != MODEL_VERSION
                    or runtime["model_sha256"] != EXPECTED_CHECKPOINT_SHA256
                    or runtime["iteration"] != 2000
                    or runtime["runtime_contract"] != EXPECTED_RUNTIME_CONTRACT
                    or runtime["capabilities"].get("confirmed_decision") is not True
                    or runtime["wire_input_contract"] != "GuandanMARVELV2.ConfirmedDecision.v1"):
                _fail("MODEL_METADATA_MISMATCH")
            seen: set[str] = set()
            per_game_versions: dict[str, int] = {}
            for item in eligible:
                context = item["context"]
                if context.decision_identity in seen:
                    _fail("DUPLICATE_DECISION_IDENTITY")
                seen.add(context.decision_identity)
                previous = per_game_versions.get(context.game_id)
                if previous is not None and context.state_version <= previous:
                    _fail("STATE_VERSION_NOT_ADVANCING")
                per_game_versions[context.game_id] = context.state_version
                payload = item["payload"]
                before_hash = _hash_json(payload)
                started = time.perf_counter()
                result = client.submit(payload)
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                after_hash = _hash_json(payload)
                if before_hash != after_hash or before_hash != context.payload_hash:
                    _fail("MODEL_INPUT_MUTATED")
                record = validate_recommendation(context, result, oracle)
                record["latency_ms"] = round(elapsed_ms, 3)
                records.append(record)
            if client.error_codes():
                _fail("MODEL_ERRORS_PRESENT")
        finally:
            client.close()
    return {"records": records, "runtime": runtime}


# ---------------------------------------------------------------------------
# FableDan regression
# ---------------------------------------------------------------------------

def run_fabledan_acceptance() -> dict[str, Any]:
    record = json.loads(OBSERVATION_FIXTURE.read_text(encoding="utf-8"))[0]
    observation = GameObservation.from_dict(record)
    view = CanonicalPublicGameView.from_observation(observation)
    statuses: list[dict] = []
    results: list = []
    started = time.perf_counter()
    with _env_scope(PYTHONDONTWRITEBYTECODE="1", GUANCE_MARVEL_V2_SOURCE=None,
                    GUANCE_MARVEL_V2_CHECKPOINT=None):
        client = InferenceClient(BEST_NPZ, on_result=results.append,
                                 on_status=lambda status: statuses.append(status.to_dict()),
                                 timeout_seconds=60.0, threads=2)
        try:
            client.start()
            if not _wait_for(lambda: any(item.get("model_status") in
                                         {"ready", "unavailable", "error"} for item in statuses), 180):
                _fail("FABLEDAN_LOAD_TIMEOUT")
            metadata = dict(statuses[-1].get("metadata") or {})
            if statuses[-1]["model_status"] != "ready":
                _fail("FABLEDAN_NOT_READY")
            latency_ms = None
            if (metadata.get("provider") != "fabledan"
                    or metadata.get("score_kind") != FABLEDAN_SCORE_KIND
                    or metadata.get("wire_input_contract") != "CanonicalPublicGameView.v1"):
                _fail("FABLEDAN_METADATA_MISMATCH")
            submit_started = time.perf_counter()
            request_id = client.submit(PublicRecommendationRequest(
                RequestIdentity("", view.session_id, view.game_id, view.state_version), view))
            if not isinstance(request_id, str) or not request_id:
                _fail("FABLEDAN_REQUEST_REJECTED")
            if not _wait_for(lambda: bool(results), 120):
                _fail("FABLEDAN_RECOMMENDATION_TIMEOUT")
            latency_ms = round((time.perf_counter() - submit_started) * 1000.0, 3)
            result = results[0]
            if result.provider != "fabledan" or result.score_kind != FABLEDAN_SCORE_KIND:
                _fail("FABLEDAN_SCORE_KIND_MISMATCH")
            if result.status != "ready" or result.error_code:
                _fail("FABLEDAN_RESULT_NOT_READY")
            validate_advice_for_view(view, result)
        finally:
            client.close()
    heavy = sorted(name for name in ("torch", "guandan_marvel", "nanopokerlib")
                   if name in sys.modules)
    return {
        "provider": "fabledan",
        "status": "ready",
        "model_sha256": EXPECTED_BEST_NPZ_SHA256,
        "score_kind": FABLEDAN_SCORE_KIND,
        "wire_input_contract": "CanonicalPublicGameView.v1",
        "legality": "validate_advice_for_view_passed",
        "failure": None,
        "latency_ms": latency_ms,
        "process_heavy_runtime_import_evidence": "see fresh-subprocess regression gate",
        "heavy_modules_in_this_runner_process": heavy,
    }


# ---------------------------------------------------------------------------
# Negative cases on the real worker and real bundle
# ---------------------------------------------------------------------------

class _RawWorker:
    """Real model worker process (production launch spec) driven by raw JSONL."""

    def __init__(self) -> None:
        probe = InferenceClient(BUNDLE / "manifest.json", on_result=lambda _result: None,
                                on_status=lambda _status: None,
                                timeout_seconds=600.0, threads=2)
        try:
            self.command, self.env, self.flags = probe._worker_process_spec()
        finally:
            probe.close()
        self.process = None
        self.messages: queue.Queue = queue.Queue()

    def __enter__(self):
        self.process = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="strict",
            bufsize=1, env=self.env, creationflags=self.flags)

        def read():
            try:
                while True:
                    message = read_message(self.process.stdout)
                    if message is None:
                        break
                    self.messages.put(message)
            except Exception:
                pass

        threading.Thread(target=read, name="phase5-raw-worker-reader", daemon=True).start()
        return self

    def send(self, kind: str, ids: dict, **body) -> None:
        assert self.process is not None and self.process.stdin is not None
        self.process.stdin.write(dumps(envelope(kind, ids, **body)))
        self.process.stdin.flush()

    def receive(self, timeout: float = 600.0):
        return self.messages.get(timeout=timeout)

    def __exit__(self, *_exc):
        process = self.process
        if process is None:
            return
        try:
            if process.poll() is None and process.stdin is not None:
                process.stdin.write(dumps(envelope("stop", _control_ids())))
                process.stdin.flush()
                process.wait(timeout=10)
        except Exception:
            pass
        try:
            if process.poll() is None:
                process.terminate()
        except Exception:
            pass


def _control_ids(session_id: str = "", game_id: str = "", state_version: int = 0) -> dict:
    import uuid
    return {"request_id": uuid.uuid4().hex, "session_id": session_id, "game_id": game_id,
            "state_version": state_version, "epoch": 0, "generation": 0}


def _recompute(payload: dict) -> dict:
    payload["decision_identity_sha256"] = decision_identity_sha256(payload)
    return payload


def _negative_payloads(eligible: list[dict]) -> dict[str, dict]:
    import copy
    base = copy.deepcopy(eligible[0]["payload"])
    follow = next(item for item in eligible
                  if item["context"].phase == "FOLLOW_PLAY")["payload"]
    cases = {}
    missing_history = copy.deepcopy(base)
    missing_history["history"]["steps"] = []
    cases["missing_history"] = _recompute(missing_history)
    missing_rows = copy.deepcopy(base)
    missing_rows["session"]["legal_row_count"] = 0
    cases["missing_legal_rows"] = _recompute(missing_rows)
    cache_mismatch = copy.deepcopy(base)
    cache_mismatch["cache_request"]["history"]["state_id"] = "phase5-tampered"
    cases["cache_history_mismatch"] = _recompute(cache_mismatch)
    counter_mismatch = copy.deepcopy(base)
    counter_mismatch["session"]["token_step"] = int(base["session"]["token_step"]) + 1
    cases["counter_mismatch"] = _recompute(counter_mismatch)
    missing_declaration = copy.deepcopy(follow)
    missing_declaration["current_winning_declaration"] = None
    state = missing_declaration["marvel_state"]
    state.update({"current_winner_seat": None, "current_family": None,
                  "current_comparison_rank": None, "current_winning_cards": [],
                  "current_winning_size": 0})
    cases["missing_declaration"] = _recompute(missing_declaration)
    return cases


def run_negative_worker_cases(classification: dict[str, Any]) -> dict[str, Any]:
    cases = _negative_payloads(classification["eligible"])
    entries = []
    valid = classification["eligible"][0]["payload"]
    with _env_scope(PYTHONDONTWRITEBYTECODE="1", GUANCE_MARVEL_V2_SOURCE=str(RUNTIME_SOURCE),
                    GUANCE_MARVEL_V2_CHECKPOINT=str(CHECKPOINT)):
        with _RawWorker() as worker:
            worker.send("load", _control_ids(), source=str(BUNDLE / "manifest.json"))
            while True:
                reply = worker.receive()
                if reply.get("type") == "loaded":
                    break
                if reply.get("type") != "status" or reply.get("model_status") != "loading":
                    _fail("RAW_WORKER_LOAD_FAILED")
            for name, payload in cases.items():
                worker.send("recommend", _control_ids(payload["session_id"], payload["game_id"],
                                                      payload["state_version"]),
                            request_kind="confirmed_decision", decision=payload)
                reply = worker.receive()
                if reply.get("type") != "error" or "result" in reply:
                    _fail("NEGATIVE_CASE_PRODUCED_RESULT")
                entries.append({"case": name, "reply_type": str(reply.get("type")),
                                "error_code": _bounded_code(reply.get("error_code")),
                                "result_present": False, "fallback": False})
            worker.send("recommend", _control_ids(valid["session_id"], valid["game_id"],
                                                  valid["state_version"]),
                        request_kind="confirmed_decision", decision=valid)
            reply = worker.receive(timeout=420.0)
            if reply.get("type") != "result" or "result" not in reply:
                _fail("VALID_DECISION_AFTER_NEGATIVES_FAILED")
    return {"cases": entries, "valid_after_negatives": True}


def _first_code(statuses: list[dict]) -> str:
    for item in reversed(statuses):
        code = item.get("error_code")
        if isinstance(code, str) and code:
            return code
    return "MODEL_UNAVAILABLE"


def _tamper_load(manifest_path: Path, *, source: str, checkpoint: str,
                 workload_timeout: float) -> dict[str, Any]:
    statuses: list[dict] = []
    results: list = []

    def on_status(status) -> None:
        statuses.append(status.to_dict())

    client = InferenceClient(manifest_path, on_result=results.append, on_status=on_status,
                             timeout_seconds=workload_timeout, threads=2)
    try:
        with _env_scope(PYTHONDONTWRITEBYTECODE="1", GUANCE_MARVEL_V2_SOURCE=source,
                        GUANCE_MARVEL_V2_CHECKPOINT=checkpoint):
            client.start()
            _wait_for(lambda: any(item.get("model_status") in
                                  {"ready", "unavailable", "error", "timeout"}
                                  for item in statuses), max(workload_timeout, 180.0))
    finally:
        client.close()
    loaded = bool(statuses) and statuses[-1].get("model_status") == "ready"
    return {"loaded": loaded, "recommendation_count": len(results),
            "fallback": False, "code": _bounded_code(_first_code(statuses))}


def run_artifact_tamper(workdir: Path) -> dict[str, Any]:
    entries = []
    # (a) Bundle deployment tampered; manifest identity unchanged.
    tampered_bundle = workdir / "bundle-tampered"
    shutil.copytree(BUNDLE, tampered_bundle)
    (tampered_bundle / "deployment.json").write_bytes(
        (tampered_bundle / "deployment.json").read_bytes() + b"x")
    result = _tamper_load(tampered_bundle / "manifest.json", source=str(RUNTIME_SOURCE),
                          checkpoint=str(CHECKPOINT), workload_timeout=120.0)
    if result["loaded"] or result["recommendation_count"]:
        _fail("TAMPERED_BUNDLE_ACCEPTED")
    entries.append({"case": "bundle_deployment_tamper", **result})
    # (b) Checkpoint tampered copy.
    tampered_checkpoint = workdir / "checkpoint-tampered.pt"
    shutil.copyfile(CHECKPOINT, tampered_checkpoint)
    with tampered_checkpoint.open("ab") as stream:
        stream.write(b"x")
    result = _tamper_load(BUNDLE / "manifest.json", source=str(RUNTIME_SOURCE),
                          checkpoint=str(tampered_checkpoint), workload_timeout=180.0)
    if result["loaded"] or result["recommendation_count"]:
        _fail("TAMPERED_CHECKPOINT_ACCEPTED")
    entries.append({"case": "checkpoint_tamper", **result})
    # (c) Runtime source tampered copy; valid checkpoint.
    tampered_source = workdir / "source-tampered"
    shutil.copytree(RUNTIME_SOURCE, tampered_source)
    target = tampered_source / "guandan_marvel" / "__init__.py"
    with target.open("ab") as stream:
        stream.write(b"\n# tampered\n")
    result = _tamper_load(BUNDLE / "manifest.json", source=str(tampered_source),
                          checkpoint=str(CHECKPOINT), workload_timeout=180.0)
    if result["loaded"] or result["recommendation_count"]:
        _fail("TAMPERED_RUNTIME_SOURCE_ACCEPTED")
    entries.append({"case": "runtime_source_tamper", **result})
    return {"cases": entries}


# ---------------------------------------------------------------------------
# Historical / offline replay over the fixed captures (frozen rules source)
# ---------------------------------------------------------------------------

def run_historical_replay() -> dict[str, Any]:
    """Replay every fixed offline capture with the frozen runtime rules source.

    The external Guandan worktree is deliberately not used as a trusted rules
    source (external drift would otherwise turn this into a false failure).
    """
    from guance.infrastructure.capture.offline_replay import OfflineReplayError, strict_replay
    captures_dir = CODE_ROOT / "debug" / "captures"
    entries = []
    for directory in sorted(captures_dir.iterdir(), key=lambda item: item.name):
        frames = directory / "frames.jsonl"
        if not frames.is_file():
            continue
        try:
            value = strict_replay(directory.name, frames, rules_source_root=RUNTIME_SOURCE)
        except OfflineReplayError:
            entries.append({"capture_id": directory.name, "status": "direction_unavailable"})
            continue
        except Exception:
            entries.append({"capture_id": directory.name, "status": "replay_error"})
            continue
        errors = value.get("error_counts", {})
        entries.append({
            "capture_id": directory.name,
            "status": "replayed",
            "protocol_rejected": int(value["protocol_rejected"]),
            "frame_integrity_errors": int(value["frame_integrity_errors"]),
            "sequence_errors": int(value["sequence_errors"]),
            "engine_paused": bool(value["engine_paused"]),
            "supported_decision_count": int(value["supported_decision_count"]),
            "model_adapter_eligible_count": int(value["model_adapter_eligible_count"]),
            "unsupported_codes": {str(key): int(count) for key, count in sorted(errors.items())},
        })
    return {
        "schema": "guance-phase5-historical-offline-replay-v1",
        "provenance": "offline_replay",
        "live_input_used": False,
        "rules_source": "frozen_runtime_source",
        "captures": entries,
    }


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

def _write_evidence(root: Path, name: str, value: dict[str, Any]) -> None:
    validate_safe_output(value)
    (root / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")


def run(evidence_root: Path | None) -> dict[str, Any]:
    if not os.environ.get("PYTHONDONTWRITEBYTECODE"):
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    gate = verify_inputs()
    replay = replay_capture()
    classification = classify_decisions(replay["decisions"])
    provider_eligible = replay["provider_eligible"]
    strict_eligible = len(classification["eligible"])
    strict_ineligible = len(classification["ineligible"])
    if (provider_eligible != EXPECTED_PROVIDER_ELIGIBLE
            or strict_eligible != EXPECTED_STRICT_ELIGIBLE
            or strict_ineligible != EXPECTED_STRICT_INELIGIBLE):
        _fail("ELIGIBILITY_COUNT_MISMATCH")
    if not replay["capture_unchanged"]:
        _fail("CAPTURE_MUTATED_BY_REPLAY")

    summary: dict[str, Any] = {
        "schema": "guance-phase5-offline-acceptance-v1",
        "provenance": "offline_confirmed_decision",
        "live_input_used": False,
        "recommendation_displayed": False,
        "provider_eligible_count": provider_eligible,
        "strict_eligible_count": strict_eligible,
        "strict_ineligible_count": strict_ineligible,
        "recommendation_count": 0,
        "recommendation_error_count": 0,
        "errors_by_code": {},
        "invalid_count": strict_ineligible,
        "stale_or_fallback_count": 0,
        "action_family_coverage": {},
    }
    if evidence_root is not None:
        evidence_root.mkdir(parents=True, exist_ok=True)
        _write_evidence(evidence_root, "phase5-input-gate.json", gate)
        _write_evidence(evidence_root, "phase5-strict-eligibility.json", {
            "schema": "guance-phase5-strict-eligibility-v1",
            "provenance": "offline_confirmed_decision",
            "live_input_used": False,
            "capture_id": CAPTURE_ID,
            "capture_sha256": replay["capture_sha256"],
            "capture_unchanged": replay["capture_unchanged"],
            "provider_eligible_count": provider_eligible,
            "strict_eligible_count": strict_eligible,
            "strict_ineligible_count": strict_ineligible,
            "strict_ineligible": classification["ineligible"],
            "silent_skip": False,
        })

    marvel = run_marvel_acceptance(classification)
    records = marvel["records"]
    families = Counter(record["family"] for record in records)
    summary["recommendation_count"] = len(records)
    summary["action_family_coverage"] = {
        "observed": dict(sorted(families.items())),
        "lead": sum(1 for record in records if record["phase"] == "LEAD_PLAY"),
        "follow": sum(1 for record in records if record["phase"] == "FOLLOW_PLAY"),
        "wildcard_declarations": sum(1 for record in records if record["wildcard"]),
        "unverified_families": sorted(
            name for name in ("single", "pair", "triple", "straight", "serial_pair",
                              "serial_triple", "sflush", "bomb", "rocket", "full", "pass")
            if families.get(name, 0) == 0),
    }
    if evidence_root is not None:
        _write_evidence(evidence_root, "phase5-marvel-decision-results.json", {
            "schema": "guance-phase5-marvel-decision-results-v1",
            "provenance": "offline_confirmed_decision",
            "live_input_used": False,
            "model_version": MODEL_VERSION,
            "checkpoint_sha256": EXPECTED_CHECKPOINT_SHA256,
            "score_kind": MARVEL_SCORE_KIND,
            "runtime": marvel["runtime"],
            "strict_eligible_count": strict_eligible,
            "recommendation_count": len(records),
            "error_count": 0,
            "fallback_count": 0,
            "records": records,
        })
        _write_evidence(evidence_root, "phase5-action-family-coverage.json", {
            "schema": "guance-phase5-action-family-coverage-v1",
            "provenance": "offline_confirmed_decision",
            **summary["action_family_coverage"],
        })

    fabledan = run_fabledan_acceptance()
    if evidence_root is not None:
        _write_evidence(evidence_root, "phase5-fabledan-regression.json", {
            "schema": "guance-phase5-fabledan-regression-v1",
            "provenance": "offline_public_view",
            "live_input_used": False,
            **fabledan,
        })

    negative = {"cases": []}
    if classification["eligible"]:
        negative = run_negative_worker_cases(classification)
    with tempfile.TemporaryDirectory(prefix="phase5-tamper-") as workdir:
        tamper = run_artifact_tamper(Path(workdir))
    historic = run_historical_replay()
    if evidence_root is not None:
        _write_evidence(evidence_root, "phase5-negative-matrix.json", {
            "schema": "guance-phase5-negative-matrix-v1",
            "provenance": "offline_fixed_inputs",
            "live_input_used": False,
            "real_worker_cases": negative,
            "artifact_tamper_cases": tamper,
            "positive_recommendation_count": 0,
            "fallback_count": 0,
        })
        _write_evidence(evidence_root, "phase5-historical-offline-replay.json", historic)

    comparison = {
        "schema": "guance-phase5-ab-comparison-v1",
        "compared_fields": ["identity", "status", "legality", "latency_ms", "failure", "score_kind"],
        "raw_score_compared": False,
        "strength_claim": False,
        "marvel": {
            "identity": {"provider": "guandan_marvel_v2", "model_version": MODEL_VERSION,
                         "model_sha256": EXPECTED_CHECKPOINT_SHA256,
                         "runtime_contract": EXPECTED_RUNTIME_CONTRACT},
            "status": "ready",
            "legality": "legal_rows_own_hand_declaration_beat_validated",
            "latency_ms": round(sum(record["latency_ms"] for record in records) / len(records), 3),
            "failure": None,
            "score_kind": MARVEL_SCORE_KIND,
        },
        "fabledan": {
            "identity": {"provider": "fabledan", "model_sha256": EXPECTED_BEST_NPZ_SHA256},
            "status": fabledan["status"],
            "legality": fabledan["legality"],
            "latency_ms": fabledan["latency_ms"],
            "failure": fabledan["failure"],
            "score_kind": fabledan["score_kind"],
        },
    }
    if evidence_root is not None:
        _write_evidence(evidence_root, "phase5-ab-comparison.json", comparison)

    # Final immutability re-check: capture, runtime, bundle and checkpoint.
    final_gate = verify_inputs()
    if final_gate["capture_sha256"] != gate["capture_sha256"] \
            or final_gate["runtime_sha256sums_entries"] != EXPECTED_RUNTIME_ENTRIES \
            or final_gate["runtime_pycache_directories"] != 0 \
            or final_gate["deployment_sha256"] != EXPECTED_DEPLOYMENT_SHA256 \
            or final_gate["checkpoint_sha256"] != EXPECTED_CHECKPOINT_SHA256:
        _fail("INPUT_MUTATED_DURING_ACCEPTANCE")
    summary["input_immutable"] = True
    summary["runtime_pycache_directories"] = final_gate["runtime_pycache_directories"]
    summary["historical_replay_captures"] = len(historic["captures"])
    summary["historical_replay_supported_total"] = sum(
        int(item.get("supported_decision_count", 0)) for item in historic["captures"])
    return summary


def _safe_reason(exc: BaseException) -> str:
    value = str(exc)
    if value and len(value) <= 64 and all(ch.isupper() or ch.isdigit() or ch == "_"
                                          for ch in value):
        return value
    return "PHASE5_ACCEPTANCE_FAILED"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, default=None)
    args = parser.parse_args()
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    sys.dont_write_bytecode = True
    evidence_root = args.evidence_root.resolve() if args.evidence_root else None
    try:
        summary = run(evidence_root)
    except Exception as exc:  # bounded fail-closed summary
        print(json.dumps({"schema": "guance-phase5-offline-acceptance-v1",
                          "status": "failed",
                          "reason": _safe_reason(exc),
                          "live_input_used": False,
                          "recommendation_displayed": False}, separators=(",", ":")))
        return 2
    summary["status"] = "passed"
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
