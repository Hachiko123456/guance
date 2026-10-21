"""Phase 3 acceptance: real iter2000 confirmed-decision recommendations.

Runs the real InferenceClient/worker/provider/runtime chain (Phase 2 frozen
runtime + real iter2000 checkpoint) over strictly built decisions:

* a bounded subset of decisions derived from the fixed offline capture through
  the NormalExchangeLiveSubsetProvider (rules loaded from the frozen runtime
  source, never the Guandan worktree);
* strict synthetic lead fixtures built from the verified rules oracle so real
  model probabilities and the real autoregressive decode cover several action
  families.

Every produced recommendation is re-checked for identity, own-hand membership
and exact legal-candidate membership. Evidence is bounded; no hidden hands, raw
payloads, tensors or private paths are written.
"""
from __future__ import annotations

import copy
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from guance.contracts import (  # noqa: E402
    CONFIRMED_DECISION_RECOMMENDATION_KIND, CanonicalAdviceResult,
    ConfirmedDecisionRecommendationRequest, RequestIdentity, decode_canonical_advice,
)
from guance.infrastructure.model.client import InferenceClient  # noqa: E402
from guance.infrastructure.model.codec import dumps, envelope, read_message  # noqa: E402
from guance.infrastructure.model.errors import ModelBackendError  # noqa: E402
from guance.public_state_contract import guandan_card_id_to_wire  # noqa: E402

EVIDENCE = ROOT / "build" / "acceptance" / "guance-model-platform-20261016" / "phase3"
RUNTIME = (ROOT / "build" / "acceptance" / "guance-model-platform-20261016" / "phase2"
           / "runtime" / "marvel-e5c82e7c-d7acbc8d")
BUNDLE = EVIDENCE / "bundles" / "marvel-v2-iter002000-confirmed"
CHECKPOINT = Path(r"D:\project\guandan\runs\comparison_delivery\dataset\m09_candidate_iter002000.pt")
CAPTURE = ROOT / "debug" / "captures" / "20261014-143204" / "frames.jsonl"
CHECKPOINT_SHA256 = "af0df1eaeb67c88e467ee78bf6b0494c49d000f5733d655ee81b40351d4ca9c8"
MODEL_VERSION = "MARVEL-V2-v1.3-iter002000"
SCORE_KIND = "marvel_v2_policy_path_score"

pytestmark = pytest.mark.skipif(
    not (BUNDLE / "manifest.json").is_file() or not CHECKPOINT.is_file()
    or not (RUNTIME / "runtime.json").is_file() or not CAPTURE.is_file(),
    reason="phase 3 acceptance materials unavailable")

if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


class _Context:
    """Bounded per-request validation context (never a raw capture payload)."""

    def __init__(self, fixture_id, *, session_id, game_id, state_version, level, phase,
                 own_hand_wire, candidate_card_sets, has_pass, has_winning, identity_sha256,
                 candidate_wild_sets=None):
        self.fixture_id = fixture_id
        self.session_id = session_id
        self.game_id = game_id
        self.state_version = state_version
        self.level = level
        self.phase = phase
        self.own_hand_wire = own_hand_wire
        self.candidate_card_sets = candidate_card_sets
        self.has_pass = has_pass
        self.has_winning = has_winning
        self.identity_sha256 = identity_sha256
        self.candidate_wild_sets = candidate_wild_sets or {}


def _configure_environment(monkeypatch) -> None:
    monkeypatch.setenv("GUANCE_MARVEL_V2_SOURCE", str(RUNTIME / "source"))
    monkeypatch.setenv("GUANCE_MARVEL_V2_CHECKPOINT", str(CHECKPOINT))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")


def _wait_for(predicate, timeout=300.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class _RealMarvelClient:
    """Synchronous wrapper over the real JSONL worker for sequential requests."""

    def __init__(self, monkeypatch):
        _configure_environment(monkeypatch)
        self.statuses = []
        self.results = []
        self.loaded_metadata = {}
        self.client = InferenceClient(BUNDLE / "manifest.json",
                                      on_result=self.results.append,
                                      on_status=self._on_status,
                                      timeout_seconds=600.0, threads=2)

    def _on_status(self, status):
        item = status.to_dict()
        self.statuses.append(item)
        if item.get("model_status") == "ready" and item.get("metadata"):
            self.loaded_metadata = dict(item["metadata"])

    def start(self):
        self.client.start()
        assert _wait_for(lambda: any(
            item.get("model_status") in {"ready", "unavailable", "error", "timeout"}
            for item in self.statuses), 600), self.statuses[-3:]
        assert self.statuses[-1]["model_status"] == "ready", self.statuses[-1]

    def submit(self, payload, timeout=420.0):
        count = len(self.results)
        request = ConfirmedDecisionRecommendationRequest(
            RequestIdentity("", payload["session_id"], payload["game_id"],
                            payload["state_version"]), payload)
        request_id = self.client.submit(request, timeout_seconds=timeout)
        assert isinstance(request_id, str) and request_id
        assert _wait_for(lambda: len(self.results) > count
                         or any(item.get("model_status") in {"error", "timeout"}
                                for item in self.statuses[-4:]), timeout), "recommendation timeout"
        if len(self.results) <= count:
            summary = [(item.get("model_status"), item.get("error_code"),
                        str(item.get("message", ""))[:60]) for item in self.statuses]
            raise AssertionError(f"no recommendation; statuses={summary!r}")
        return self.results[-1]

    def close(self):
        self.client.close()


def _capture_decisions():
    import base64
    from guance.contracts import SourceFrame
    from guance.domain.session import SessionEngine
    from guance.infrastructure.capture.live_subset import NormalExchangeLiveSubsetProvider
    from guance.infrastructure.protocol import live
    from scripts.run_phase7_offline_checkpoint_acceptance import _decision_model_identity

    provider = NormalExchangeLiveSubsetProvider(RUNTIME / "source")
    provider.set_model_artifact_identity_provider(lambda: (MODEL_VERSION, CHECKPOINT_SHA256))
    provider.bind_capture("20261014-143204")
    engine = SessionEngine("phase3-acceptance", aggregate_parser=live.parse_aggregate,
                           accepted_host=live.GAME_HOST, event_adapter=provider.adapt_event,
                           event_observer=provider.observe_applied,
                           reset_observer=provider.reset_session)
    decisions = []
    for line in CAPTURE.open("rb"):
        record = json.loads(line.decode("utf-8-sig"))
        payload = b""
        if record.get("direction") == "server":
            payload = base64.b64decode(record["payload_b64"], validate=True)
        frame = SourceFrame(host=str(record["host"]), payload=payload,
                            received_at=float(record["received_at"]),
                            connection_id=str(record.get("connection_id", "")),
                            sequence=int(record["sequence"]), direction=str(record.get("direction")))
        engine.feed(frame)
        decisions.extend(provider.drain_decisions())
    return decisions, _decision_model_identity


def _capture_context(decision, identity_fn):
    session = decision.action_session
    own_hand_wire = tuple(guandan_card_id_to_wire(card) for card in session.own_hand)
    candidate_sets = frozenset(
        frozenset(guandan_card_id_to_wire(card) for card in candidate.physical_cards)
        for candidate in session.candidates
    )
    wild_sets = {
        frozenset(guandan_card_id_to_wire(card) for card in candidate.physical_cards):
        bool(candidate.declaration is not None
             and candidate.declaration.wild_assignments)
        for candidate in session.candidates
    }
    return _Context(
        "capture-decision", session_id=decision.public_state.identity.session_id,
        game_id=decision.public_state.identity.game_id,
        state_version=decision.public_state.identity.state_version,
        level=decision.marvel_state.level, phase=session.phase,
        own_hand_wire=own_hand_wire, candidate_card_sets=candidate_sets,
        has_pass=any(candidate.kind == "pass" for candidate in session.candidates),
        has_winning=bool(decision.marvel_state.current_winning_cards),
        identity_sha256=identity_fn(decision), candidate_wild_sets=wild_sets)


def _synthetic_payload(*, fixture_id, game_id, state_version, observer, level, phase,
                       own_hand_ids, winning_candidate=None, sequence, fixture_index):
    """Build a strict confirmed-decision payload from verified rules about a state.

    Uses the real rules oracle for candidates and the real wire serializers so
    the worker regenerates exactly the same legal rows.
    """
    from guance.infrastructure.model.marvel_v2.action_decoder import declaration_tokens
    from guance.infrastructure.model.marvel_v2.cache_lifecycle import (
        AbstractCacheLifecycle,
    )
    from guance.infrastructure.model.marvel_v2.contracts import (
        COMMIT_TOKEN, ExchangeFacts, MarvelEvent, MarvelState,
    )
    from guance.infrastructure.model.marvel_v2.decision_request import (
        _cache_request_wire, _history_identity_wire, _marvel_state_wire,
        candidate_sequences_sha256, decision_identity_sha256,
    )
    from guance.infrastructure.model.marvel_v2.history import ExactHistoryJournal
    from guance.infrastructure.model.marvel_v2.action_protocol import ActionProtocolSession
    from guance.infrastructure.model.marvel_v2.rules_oracle import (
        VerifiedGuandanRulesOracle, _public_declaration,
    )
    from guance.public_state_contract import wire_card_to_guandan_id

    oracle = VerifiedGuandanRulesOracle(RUNTIME / "source")
    events = [MarvelEvent("RESET", "LEAD_PLAY", 1 if winning_candidate else observer,
                          level=level)]
    current_public = None
    winning_cards = ()
    winning_family = None
    comparison = None
    if winning_candidate is not None:
        declaration = winning_candidate.declaration
        tokens = declaration_tokens(declaration, level)
        for token in tokens[:-1]:
            events.append(MarvelEvent("PRIVATE_PREFIX", "LEAD_PLAY", 1, tokens=(token,)))
        events.append(MarvelEvent("PUBLIC_COMMIT", "LEAD_PLAY", 1, tokens=tokens))
        current_public = _public_declaration(declaration)
        winning_cards = tuple(wire_card_to_guandan_id(card)
                              for card in declaration.canonical_action_cards)
        winning_family = declaration.family
        comparison = declaration.comparison_rank
    journal = ExactHistoryJournal.from_events(
        session_id="phase3-fixture", game_id=game_id, state_id=f"fixture-{fixture_index}",
        artifact_id=MODEL_VERSION, artifact_sha256=CHECKPOINT_SHA256, events=tuple(events))
    identity = journal.identity
    state = MarvelState(
        observer_seat=observer, active_seat=observer,
        leader_seat=1 if winning_candidate else observer,
        current_winner_seat=1 if winning_candidate else None, phase=phase, level=level,
        own_hand=tuple(own_hand_ids), current_winning_cards=winning_cards,
        self_exchange_cards=(), private_prefix=(),
        remaining_counts=tuple([len(own_hand_ids) if seat == observer else 27 for seat in range(4)]),
        finishing_ranks=(0, 0, 0, 0), emptied_seats=(), consecutive_passes=0,
        current_family=winning_family, current_comparison_rank=comparison,
        current_winning_size=len(winning_cards), previous_result=None,
        exchange=ExchangeFacts(), token_step=identity.token_step,
        committed_step=identity.committed_step, history_complete=True)
    state.validate()
    candidates = oracle.legal_action_candidates(
        tuple(guandan_card_id_to_wire(card) for card in own_hand_ids),
        wire_level=level, phase=phase, current=current_public)
    session = ActionProtocolSession(
        "phase3-fixture", state_version, phase, tuple(own_hand_ids), candidates,
        confirmed_domain=None, prefix=(), token_step=identity.token_step,
        committed_step=identity.committed_step)
    rows = session.legal_rows
    cache = AbstractCacheLifecycle(lifecycle_id="phase3-fixture", session_id="phase3-fixture",
                                   game_id=game_id, artifact_id=MODEL_VERSION,
                                   artifact_sha256=CHECKPOINT_SHA256, initial_sequence=sequence - 1)
    request = cache.begin_request(journal, request_key=f"fixture-{fixture_index}")
    payload = {
        "schema": "GuandanMARVELV2.ConfirmedDecision.v1",
        "capture_id": f"phase3-fixture-{fixture_index}",
        "frame_sequence": state_version,
        "session_id": "phase3-fixture", "game_id": game_id, "state_version": state_version,
        "model": {"artifact_id": MODEL_VERSION, "artifact_sha256": CHECKPOINT_SHA256},
        "marvel_state": _marvel_state_wire(state),
        "current_winning_declaration": current_public.to_dict() if current_public else None,
        "session": {
            "phase": phase,
            "own_hand": [int(card) for card in own_hand_ids],
            "prefix": [], "token_step": identity.token_step,
            "committed_step": identity.committed_step,
            "candidates_sha256": candidate_sequences_sha256(candidates),
            "legal_generation": rows.rows[0].generation,
            "legal_row_count": len(rows.rows),
        },
        "history": {"identity": _history_identity_wire(identity),
                    "steps": [[list(row) for row in step.rows] for step in journal.steps]},
        "cache_request": _cache_request_wire(request),
    }
    payload["decision_identity_sha256"] = decision_identity_sha256(payload)
    context = _Context(
        fixture_id, session_id="phase3-fixture", game_id=game_id,
        state_version=state_version, level=level, phase=phase,
        own_hand_wire=tuple(guandan_card_id_to_wire(card) for card in own_hand_ids),
        candidate_card_sets=frozenset(
            frozenset(guandan_card_id_to_wire(card) for card in candidate.physical_cards)
            for candidate in candidates),
        has_pass=any(candidate.kind == "pass" for candidate in candidates),
        has_winning=winning_candidate is not None,
        identity_sha256=payload["decision_identity_sha256"],
        candidate_wild_sets={
            frozenset(guandan_card_id_to_wire(card) for card in candidate.physical_cards):
            bool(candidate.declaration is not None and candidate.declaration.wild_assignments)
            for candidate in candidates})
    return payload, context


@dataclass(frozen=True, slots=True)
class _TrickCommit:
    """One explicit PUBLIC_COMMIT in a bounded trick-history fixture."""

    kind: str
    actor: int
    phase: str
    declaration: object | None = None


def _play_commit(actor, *, phase, declaration):
    return _TrickCommit("play", actor, phase, declaration)


def _pass_commit(actor):
    return _TrickCommit("pass", actor, "FOLLOW_PLAY", None)


def _trick_journal(*, level, game_id, commits, fixture_index):
    """Build a real ExactHistoryJournal from explicit played/passed commits.

    The RESET event matches the first commit exactly, so the journal is
    structurally valid for the same strict parser the production path uses.
    """
    from guance.infrastructure.model.marvel_v2.action_decoder import declaration_tokens
    from guance.infrastructure.model.marvel_v2.contracts import MarvelEvent, PASS_TOKEN
    from guance.infrastructure.model.marvel_v2.history import ExactHistoryJournal

    first = commits[0]
    events = [MarvelEvent("RESET", first.phase, first.actor, level=level)]
    for commit in commits:
        if commit.kind == "pass":
            events.append(MarvelEvent("PUBLIC_COMMIT", "FOLLOW_PLAY", commit.actor,
                                      tokens=(PASS_TOKEN,)))
            continue
        tokens = declaration_tokens(commit.declaration, level)
        for token in tokens[:-1]:
            events.append(MarvelEvent("PRIVATE_PREFIX", commit.phase, commit.actor,
                                      tokens=(token,)))
        events.append(MarvelEvent("PUBLIC_COMMIT", commit.phase, commit.actor,
                                  tokens=tokens))
    return ExactHistoryJournal.from_events(
        session_id="phase3-fixture", game_id=game_id, state_id=f"fixture-{fixture_index}",
        artifact_id=MODEL_VERSION, artifact_sha256=CHECKPOINT_SHA256, events=tuple(events))


def _oracle_and_declaration(runtime_source=None):
    from guance.infrastructure.model.marvel_v2.rules_oracle import (
        VerifiedGuandanRulesOracle,
    )
    return VerifiedGuandanRulesOracle(runtime_source or (RUNTIME / "source"))


def _trick_payload(*, commits, observer, own_hand_ids, level, game_id, state_version,
                   phase, active, leader, winner, declaration=None,
                   consecutive_passes=0, remaining_counts, finishing_ranks=(0, 0, 0, 0),
                   emptied_seats=None, sequence, fixture_index,
                   session_id="phase3-fixture"):
    """Build one fully identity-bound confirmed-decision payload for an explicit
    claimed topology. All hashes (history/candidates/cache/decision) are recomputed
    with the production functions; only the claimed topology is under test."""
    from guance.infrastructure.model.marvel_v2.action_protocol import ActionProtocolSession
    from guance.infrastructure.model.marvel_v2.cache_lifecycle import AbstractCacheLifecycle
    from guance.infrastructure.model.marvel_v2.contracts import ExchangeFacts, MarvelState
    from guance.infrastructure.model.marvel_v2.decision_request import (
        _cache_request_wire, _history_identity_wire, _marvel_state_wire,
        candidate_sequences_sha256, decision_identity_sha256,
    )
    from guance.infrastructure.model.marvel_v2.rules_oracle import _public_declaration
    from guance.public_state_contract import wire_card_to_guandan_id

    oracle = _oracle_and_declaration()
    journal = _trick_journal(level=level, game_id=game_id, commits=tuple(commits),
                             fixture_index=fixture_index)
    identity = journal.identity
    own_hand = tuple(own_hand_ids)
    current_public = None
    winning_cards = ()
    winning_family = None
    comparison = None
    if declaration is not None:
        current_public = _public_declaration(declaration)
        winning_cards = tuple(wire_card_to_guandan_id(card)
                              for card in declaration.canonical_cards)
        winning_family = declaration.family
        comparison = declaration.comparison_rank
    counts = tuple(remaining_counts)
    if emptied_seats is None:
        emptied_seats = tuple(seat for seat in range(4) if counts[seat] == 0)
    state = MarvelState(
        observer_seat=observer, active_seat=active, leader_seat=leader,
        current_winner_seat=winner, phase=phase, level=level,
        own_hand=own_hand, current_winning_cards=winning_cards,
        self_exchange_cards=(), private_prefix=(),
        remaining_counts=counts, finishing_ranks=tuple(finishing_ranks),
        emptied_seats=tuple(emptied_seats), consecutive_passes=consecutive_passes,
        current_family=winning_family, current_comparison_rank=comparison,
        current_winning_size=len(winning_cards), previous_result=None,
        exchange=ExchangeFacts(), token_step=identity.token_step,
        committed_step=identity.committed_step, history_complete=True)
    state.validate()
    candidates = oracle.legal_action_candidates(
        tuple(guandan_card_id_to_wire(card) for card in own_hand),
        wire_level=level, phase=phase, current=current_public)
    assert candidates, "fixture must produce at least one legal candidate"
    protocol = ActionProtocolSession(
        session_id, state_version, phase, own_hand, candidates,
        confirmed_domain=None, prefix=(), token_step=identity.token_step,
        committed_step=identity.committed_step)
    rows = protocol.legal_rows
    cache = AbstractCacheLifecycle(lifecycle_id="phase3-fixture", session_id=session_id,
                                   game_id=game_id, artifact_id=MODEL_VERSION,
                                   artifact_sha256=CHECKPOINT_SHA256,
                                   initial_sequence=sequence - 1)
    request = cache.begin_request(journal, request_key=f"fixture-{fixture_index}")
    payload = {
        "schema": "GuandanMARVELV2.ConfirmedDecision.v1",
        "capture_id": f"phase3-fixture-{fixture_index}",
        "frame_sequence": state_version,
        "session_id": session_id, "game_id": game_id, "state_version": state_version,
        "model": {"artifact_id": MODEL_VERSION, "artifact_sha256": CHECKPOINT_SHA256},
        "marvel_state": _marvel_state_wire(state),
        "current_winning_declaration": current_public.to_dict() if current_public else None,
        "session": {
            "phase": phase,
            "own_hand": [int(card) for card in own_hand],
            "prefix": [], "token_step": identity.token_step,
            "committed_step": identity.committed_step,
            "candidates_sha256": candidate_sequences_sha256(candidates),
            "legal_generation": rows.rows[0].generation,
            "legal_row_count": len(rows.rows),
        },
        "history": {"identity": _history_identity_wire(identity),
                    "steps": [[list(row) for row in step.rows] for step in journal.steps]},
        "cache_request": _cache_request_wire(request),
    }
    payload["decision_identity_sha256"] = decision_identity_sha256(payload)
    return payload


def _wire(suit, rank):
    from guance.public_state_contract import wire_card_to_guandan_id
    return wire_card_to_guandan_id((suit << 4) | rank)


def _trick_declaration(hand_ids, *, family, level=2):
    from guance.public_state_contract import guandan_card_id_to_wire
    oracle = _oracle_and_declaration()
    candidates = oracle.legal_action_candidates(
        tuple(guandan_card_id_to_wire(card) for card in hand_ids),
        wire_level=level, phase="LEAD_PLAY", current=None)
    return next(candidate.declaration for candidate in candidates
                if candidate.declaration is not None and candidate.declaration.family == family)


# ---------------------------------------------------------------------------
# Phase 3 Repair-3 fixed counterexamples (R1/R2/R3 + section-six list).
#
# Every payload below is wire-valid and fully identity-bound; only the claimed
# trick topology is impossible. `S=1` plays a single, `S=3` plays a 4-card bomb
# (so seat 3 empties), `S=1` plays a pair in the second trick, and the pass
# actors follow the project's decreasing rotation where the history itself is
# meant to be replay-valid.
# ---------------------------------------------------------------------------

def _trick_counterexample_r1():
    """R1: LEAD while the previous trick's last play has only 1 following pass."""
    single = _trick_declaration((_wire(1, 6),), family="SINGLE")
    return _trick_payload(
        commits=[_play_commit(1, phase="LEAD_PLAY", declaration=single),
                 _pass_commit(0)],
        observer=0, own_hand_ids=(_wire(2, 8), _wire(2, 9)), level=2,
        game_id="phase3-fixture-trick", state_version=700, phase="LEAD_PLAY",
        active=0, leader=0, winner=None, declaration=None, consecutive_passes=0,
        remaining_counts=(2, 27, 27, 27), sequence=701, fixture_index=701)


def _trick_counterexample_r2():
    """R2: LEAD after closure whose leader is not the winner/inherited leader."""
    single = _trick_declaration((_wire(1, 6),), family="SINGLE")
    return _trick_payload(
        commits=[_play_commit(1, phase="LEAD_PLAY", declaration=single),
                 _pass_commit(0), _pass_commit(3), _pass_commit(2)],
        observer=0, own_hand_ids=(_wire(2, 8), _wire(2, 9)), level=2,
        game_id="phase3-fixture-trick", state_version=710, phase="LEAD_PLAY",
        active=0, leader=0, winner=None, declaration=None, consecutive_passes=0,
        remaining_counts=(2, 27, 27, 27), sequence=711, fixture_index=711)


def _trick_counterexample_r3():
    """R3: FOLLOW whose active actor is the seat that just passed."""
    single = _trick_declaration((_wire(1, 6),), family="SINGLE")
    return _trick_payload(
        commits=[_play_commit(1, phase="LEAD_PLAY", declaration=single),
                 _pass_commit(0)],
        observer=0, own_hand_ids=(_wire(2, 8), _wire(2, 9)), level=2,
        game_id="phase3-fixture-trick", state_version=720, phase="FOLLOW_PLAY",
        active=0, leader=1, winner=1, declaration=single, consecutive_passes=1,
        remaining_counts=(2, 27, 27, 27), sequence=721, fixture_index=721)


def _trick_counterexample_skip_alive_actor():
    """FOLLOW that skips the rotation and lands on another alive seat."""
    single = _trick_declaration((_wire(1, 6),), family="SINGLE")
    return _trick_payload(
        commits=[_play_commit(1, phase="LEAD_PLAY", declaration=single)],
        observer=3, own_hand_ids=(_wire(2, 8), _wire(2, 9)), level=2,
        game_id="phase3-fixture-trick", state_version=730, phase="FOLLOW_PLAY",
        active=3, leader=1, winner=1, declaration=single, consecutive_passes=0,
        remaining_counts=(27, 27, 27, 2), sequence=731, fixture_index=731)


def _trick_counterexample_repeated_actor():
    """FOLLOW history where the same actor commits two consecutive passes."""
    single = _trick_declaration((_wire(1, 6),), family="SINGLE")
    return _trick_payload(
        commits=[_play_commit(1, phase="LEAD_PLAY", declaration=single),
                 _pass_commit(0), _pass_commit(0)],
        observer=0, own_hand_ids=(_wire(2, 8), _wire(2, 9)), level=2,
        game_id="phase3-fixture-trick", state_version=740, phase="FOLLOW_PLAY",
        active=0, leader=1, winner=1, declaration=single, consecutive_passes=2,
        remaining_counts=(2, 27, 27, 27), sequence=741, fixture_index=741)


def _trick_counterexample_pass_actor_acts_again():
    """FOLLOW history where a passed seat acts again inside the same open trick."""
    first = _trick_declaration((_wire(1, 6),), family="SINGLE")
    second = _trick_declaration((_wire(2, 4), _wire(3, 4)), family="PAIR")
    return _trick_payload(
        commits=[_play_commit(1, phase="LEAD_PLAY", declaration=first),
                 _pass_commit(0),
                 _play_commit(0, phase="FOLLOW_PLAY", declaration=second)],
        observer=2, own_hand_ids=(_wire(1, 8), _wire(1, 9)), level=2,
        game_id="phase3-fixture-trick", state_version=750, phase="FOLLOW_PLAY",
        active=2, leader=1, winner=0, declaration=second, consecutive_passes=0,
        remaining_counts=(27, 27, 2, 27), sequence=751, fixture_index=751)


def _trick_counterexample_winner_mismatch():
    """FOLLOW whose claimed current winner is not the replay's open-trick winner."""
    first = _trick_declaration((_wire(1, 6),), family="SINGLE")
    second = _trick_declaration((_wire(2, 4), _wire(3, 4)), family="PAIR")
    return _trick_payload(
        commits=[_play_commit(1, phase="LEAD_PLAY", declaration=first),
                 _play_commit(0, phase="FOLLOW_PLAY", declaration=second),
                 _pass_commit(3)],
        observer=2, own_hand_ids=(_wire(1, 8), _wire(1, 9)), level=2,
        game_id="phase3-fixture-trick", state_version=760, phase="FOLLOW_PLAY",
        active=2, leader=1, winner=1, declaration=first, consecutive_passes=1,
        remaining_counts=(27, 27, 2, 27), sequence=761, fixture_index=761)


def _trick_counterexample_leader_mismatch():
    """FOLLOW whose claimed trick leader is not the exact-history LEAD actor."""
    first = _trick_declaration((_wire(1, 6),), family="SINGLE")
    second = _trick_declaration((_wire(2, 4), _wire(3, 4)), family="PAIR")
    return _trick_payload(
        commits=[_play_commit(1, phase="LEAD_PLAY", declaration=first),
                 _play_commit(0, phase="FOLLOW_PLAY", declaration=second)],
        observer=3, own_hand_ids=(_wire(2, 8), _wire(2, 9)), level=2,
        game_id="phase3-fixture-trick", state_version=770, phase="FOLLOW_PLAY",
        active=3, leader=2, winner=0, declaration=second, consecutive_passes=0,
        remaining_counts=(27, 27, 27, 2), sequence=771, fixture_index=771)


def _trick_counterexample_lead_missing_pass():
    """LEAD while one alive seat never passed the still-open trick."""
    single = _trick_declaration((_wire(1, 6),), family="SINGLE")
    return _trick_payload(
        commits=[_play_commit(1, phase="LEAD_PLAY", declaration=single),
                 _pass_commit(0), _pass_commit(3)],
        observer=2, own_hand_ids=(_wire(1, 8), _wire(1, 9)), level=2,
        game_id="phase3-fixture-trick", state_version=780, phase="LEAD_PLAY",
        active=2, leader=2, winner=None, declaration=None, consecutive_passes=0,
        remaining_counts=(27, 27, 2, 27), sequence=781, fixture_index=781)


def _trick_counterexample_wrong_inherited_leader():
    """LEAD after an emptied winner whose inherited lead is the partner, not 0."""
    bomb = _trick_declaration((_wire(1, 4), _wire(2, 4), _wire(3, 4), _wire(4, 4)),
                              family="RANK_BOMB")
    return _trick_payload(
        commits=[_play_commit(3, phase="LEAD_PLAY", declaration=bomb),
                 _pass_commit(2), _pass_commit(1), _pass_commit(0)],
        observer=0, own_hand_ids=(_wire(2, 8), _wire(2, 9)), level=2,
        game_id="phase3-fixture-trick", state_version=790, phase="LEAD_PLAY",
        active=0, leader=0, winner=None, declaration=None, consecutive_passes=0,
        remaining_counts=(2, 27, 27, 0), sequence=791, fixture_index=791)


def _trick_counterexample_terminal_disguised_as_lead():
    """Terminal exact history (team 1/3 both emptied) disguised as a LEAD."""
    bomb = _trick_declaration((_wire(1, 4), _wire(2, 4), _wire(3, 4), _wire(4, 4)),
                              family="RANK_BOMB")
    pair = _trick_declaration((_wire(2, 5), _wire(3, 5)), family="PAIR")
    return _trick_payload(
        commits=[_play_commit(3, phase="LEAD_PLAY", declaration=bomb),
                 _pass_commit(2), _pass_commit(1), _pass_commit(0),
                 _play_commit(1, phase="LEAD_PLAY", declaration=pair),
                 _pass_commit(2)],
        observer=0, own_hand_ids=(_wire(2, 8), _wire(2, 9)), level=2,
        game_id="phase3-fixture-trick", state_version=800, phase="LEAD_PLAY",
        active=0, leader=0, winner=None, declaration=None, consecutive_passes=0,
        remaining_counts=(2, 0, 27, 0), sequence=801, fixture_index=801)


TRICK_COUNTEREXAMPLE_BUILDERS = (
    ("R1-lead-open-trick", _trick_counterexample_r1),
    ("R2-wrong-lead-leader", _trick_counterexample_r2),
    ("R3-follow-just-passed-active", _trick_counterexample_r3),
    ("skip-alive-actor", _trick_counterexample_skip_alive_actor),
    ("repeated-actor", _trick_counterexample_repeated_actor),
    ("pass-actor-acts-again", _trick_counterexample_pass_actor_acts_again),
    ("winner-mismatch", _trick_counterexample_winner_mismatch),
    ("leader-mismatch", _trick_counterexample_leader_mismatch),
    ("lead-missing-pass", _trick_counterexample_lead_missing_pass),
    ("wrong-inherited-leader", _trick_counterexample_wrong_inherited_leader),
    ("terminal-disguised-as-lead", _trick_counterexample_terminal_disguised_as_lead),
)


# ---------------------------------------------------------------------------
# Positive controls: complete closed LEAD, legal 0/1/2-pass FOLLOWs, emptied
# seat rotation skip, and the legal inherited lead of an emptied winner.
# ---------------------------------------------------------------------------

def _trick_positive_lead_continuity():
    single = _trick_declaration((_wire(1, 6),), family="SINGLE")
    return _trick_payload(
        commits=[_play_commit(1, phase="LEAD_PLAY", declaration=single),
                 _pass_commit(0), _pass_commit(3), _pass_commit(2)],
        observer=1, own_hand_ids=(_wire(2, 8), _wire(2, 9)), level=2,
        game_id="phase3-fixture-trick", state_version=810, phase="LEAD_PLAY",
        active=1, leader=1, winner=None, declaration=None, consecutive_passes=0,
        remaining_counts=(27, 2, 27, 27), sequence=811, fixture_index=811)


def _trick_positive_follow_passes(passes):
    single = _trick_declaration((_wire(1, 6),), family="SINGLE")
    commits = [_play_commit(1, phase="LEAD_PLAY", declaration=single)]
    if passes >= 1:
        commits.append(_pass_commit(0))
    if passes >= 2:
        commits.append(_pass_commit(3))
    # Rotation: after seat 1 -> 0 -> 3 -> 2.
    active = (0, 3, 2)[passes]
    counts = [27 if seat != active else 2 for seat in range(4)]
    return _trick_payload(
        commits=commits, observer=active, own_hand_ids=(_wire(2, 8), _wire(2, 9)),
        level=2, game_id="phase3-fixture-trick", state_version=820 + passes,
        phase="FOLLOW_PLAY", active=active, leader=1, winner=1, declaration=single,
        consecutive_passes=passes, remaining_counts=tuple(counts),
        sequence=821 + passes, fixture_index=821 + passes)


def _trick_positive_emptied_skip():
    bomb = _trick_declaration((_wire(1, 4), _wire(2, 4), _wire(3, 4), _wire(4, 4)),
                              family="RANK_BOMB")
    pair = _trick_declaration((_wire(2, 5), _wire(3, 5)), family="PAIR")
    return _trick_payload(
        commits=[_play_commit(3, phase="LEAD_PLAY", declaration=bomb),
                 _pass_commit(2), _pass_commit(1), _pass_commit(0),
                 _play_commit(1, phase="LEAD_PLAY", declaration=pair),
                 _pass_commit(0)],
        observer=2, own_hand_ids=(_wire(1, 8), _wire(1, 9)), level=2,
        game_id="phase3-fixture-trick", state_version=830, phase="FOLLOW_PLAY",
        active=2, leader=1, winner=1, declaration=pair, consecutive_passes=1,
        remaining_counts=(27, 27, 2, 0), sequence=831, fixture_index=831)


def _trick_positive_inherited_lead():
    bomb = _trick_declaration((_wire(1, 4), _wire(2, 4), _wire(3, 4), _wire(4, 4)),
                              family="RANK_BOMB")
    return _trick_payload(
        commits=[_play_commit(3, phase="LEAD_PLAY", declaration=bomb),
                 _pass_commit(2), _pass_commit(1), _pass_commit(0)],
        observer=1, own_hand_ids=(_wire(2, 8), _wire(2, 9)), level=2,
        game_id="phase3-fixture-trick", state_version=840, phase="LEAD_PLAY",
        active=1, leader=1, winner=None, declaration=None, consecutive_passes=0,
        remaining_counts=(27, 2, 27, 0), sequence=841, fixture_index=841)


TRICK_POSITIVE_BUILDERS = (
    ("lead-continuity", _trick_positive_lead_continuity),
    ("follow-zero-passes", lambda: _trick_positive_follow_passes(0)),
    ("follow-one-pass", lambda: _trick_positive_follow_passes(1)),
    ("follow-two-passes", lambda: _trick_positive_follow_passes(2)),
    ("emptied-seat-skip", _trick_positive_emptied_skip),
    ("inherited-lead", _trick_positive_inherited_lead),
)


def _synthetic_fixtures():
    from guance.infrastructure.model.marvel_v2.rules_oracle import VerifiedGuandanRulesOracle
    from guance.public_state_contract import guandan_card_id_to_wire, wire_card_to_guandan_id

    oracle = VerifiedGuandanRulesOracle(RUNTIME / "source")
    level = 2

    # wire card helper: suit 1..4 (CLUBS=1,DIAMONDS=2,HEARTS=3,SPADES=4), rank 1..13
    def wc(suit, rank):
        return wire_card_to_guandan_id((suit << 4) | rank)

    fixtures = []
    lead_specs = [
        ("lead-single-only", [wc(1, 4), wc(2, 5)]),
        ("lead-pair-only", [wc(1, 4), wc(2, 4)]),
        ("lead-triple-cards", [wc(1, 4), wc(2, 4), wc(3, 4)]),
        ("lead-straight-five", [wc(1, 4), wc(2, 5), wc(3, 6), wc(4, 7), wc(1, 8)]),
        ("lead-sequences", [wc(1, 4), wc(2, 4), wc(3, 5), wc(1, 5), wc(2, 6), wc(3, 6)]),
        ("lead-straight-flush-five", [wc(1, 4), wc(1, 5), wc(1, 6), wc(1, 7), wc(1, 8)]),
        ("lead-bomb-quad", [wc(1, 4), wc(2, 4), wc(3, 4), wc(4, 4)]),
        ("lead-jokers", [52, 53, 106, 107]),
        ("lead-wild-pair", [wc(3, 2), wc(1, 7), wc(2, 7)]),
    ]
    for index, (name, hand_ids) in enumerate(lead_specs, 1):
        payload, context = _synthetic_payload(
            fixture_id=name, game_id="phase3-fixture-lead", state_version=100 + index,
            observer=0, level=level, phase="LEAD_PLAY", own_hand_ids=tuple(hand_ids),
            winning_candidate=None, sequence=index, fixture_index=index)
        fixtures.append((payload, context))
    follow_specs = [
        ("follow-single", [wc(1, 6)], [wc(2, 8), wc(2, 9)], "SINGLE"),
        ("follow-pair", [wc(1, 3), wc(2, 3)], [wc(3, 4), wc(4, 4), wc(3, 5), wc(4, 5)],
         "PAIR"),
    ]
    offset = len(lead_specs)
    follow_index = 0
    for name, winning_hand, follower_hand, family in follow_specs:
        follow_index += 1
        winning_candidates = oracle.legal_action_candidates(
            tuple(guandan_card_id_to_wire(card) for card in winning_hand),
            wire_level=level, phase="LEAD_PLAY", current=None)
        winning = next(candidate for candidate in winning_candidates
                      if candidate.declaration is not None
                      and candidate.declaration.family == family)
        payload, context = _synthetic_payload(
            fixture_id=name, game_id="phase3-fixture-follow",
            state_version=200 + follow_index, observer=0, level=level,
            phase="FOLLOW_PLAY", own_hand_ids=tuple(follower_hand),
            winning_candidate=winning, sequence=offset + follow_index,
            fixture_index=offset + follow_index)
        fixtures.append((payload, context))
    return fixtures


def _validate_result(context: _Context, result: CanonicalAdviceResult) -> dict:
    assert isinstance(result, CanonicalAdviceResult)
    record = {"fixture": context.fixture_id, "valid": True}
    assert (result.session_id, result.game_id, result.state_version) == (
        context.session_id, context.game_id, context.state_version)
    assert result.status == "ready" and not result.error_code and not result.error_message
    assert result.provider == "guandan_marvel_v2"
    assert result.model_version == MODEL_VERSION
    assert result.model_fingerprint == CHECKPOINT_SHA256
    assert result.score_kind == SCORE_KIND
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.rank == 1 and candidate.score_kind == SCORE_KIND
    assert candidate.score is not None and math.isfinite(float(candidate.score))
    assert float(candidate.score) <= 0.0  # complete-path log-likelihood
    record.update({"kind": candidate.kind, "cards_count": len(candidate.cards),
                   "score_kind": candidate.score_kind,
                   "path_score": float(candidate.score),
                   "declaration": candidate.claim_kind or "",
                   "declared_ranks": list(candidate.claim),
                   "phase": context.phase})
    if candidate.kind == "pass":
        assert not candidate.cards and context.has_pass and context.has_winning
        record["family"] = "pass"
        record["wildcard"] = False
    else:
        cards = tuple(candidate.cards)
        assert cards and len(set(cards)) == len(cards)
        assert set(cards) <= set(context.own_hand_wire)
        assert frozenset(cards) in context.candidate_card_sets
        record["family"] = candidate.kind
        record["wildcard"] = bool(context.candidate_wild_sets.get(frozenset(cards)))
    return record


def test_real_iter2000_confirmed_decision_recommendations(monkeypatch, tmp_path):
    decisions, identity_fn = _capture_decisions()
    assert decisions, "fixed capture produced no confirmed decisions"

    # Bounded real-capture subset: first decision of every game plus the longest
    # history decision, keeping the acceptance runtime reasonable.
    first_by_game = {}
    for decision in decisions:
        game = decision.public_state.identity.game_id
        first_by_game.setdefault(game, decision)
    longest = max(decisions, key=lambda item: len(item.history.steps))
    selected, seen = [], set()
    for decision in list(first_by_game.values()) + [longest]:
        key = identity_fn(decision)
        if key in seen:
            continue
        seen.add(key)
        selected.append(decision)

    fixtures = _synthetic_fixtures()
    client = _RealMarvelClient(monkeypatch)
    records = []
    try:
        client.start()
        assert client.loaded_metadata.get("provider") == "guandan_marvel_v2"
        capabilities = client.loaded_metadata.get("capabilities", {})
        assert capabilities.get("confirmed_decision") is True
        assert capabilities.get("complete_action") is True
        assert client.loaded_metadata.get("wire_input_contract") == "GuandanMARVELV2.ConfirmedDecision.v1"
        for decision in selected:
            context = _capture_context(decision, identity_fn)
            from guance.infrastructure.model.marvel_v2.decision_request import (
                project_confirmed_decision,
            )
            payload = project_confirmed_decision(
                decision, artifact_id=MODEL_VERSION, artifact_sha256=CHECKPOINT_SHA256)
            result = client.submit(payload)
            records.append(_validate_result(context, result))
        for payload, context in fixtures:
            result = client.submit(payload)
            records.append(_validate_result(context, result))
    finally:
        client.close()

    families = Counter(record["family"] for record in records)
    summary = {
        "schema": "guance-phase3-actual-recommendation-summary-v1",
        "provenance": "offline_confirmed_decision",
        "live_input_used": False,
        "runtime_id": "marvel-e5c82e7c27fb-d7acbc8d76c9",
        "checkpoint_sha256": CHECKPOINT_SHA256,
        "model_version": MODEL_VERSION,
        "capture_decision_count_total": len(decisions),
        "submitted_capture_decisions": len(selected),
        "submitted_synthetic_fixtures": len(fixtures),
        "recommendation_count": len(records),
        "stale_or_fallback_count": 0,
        "score_kind": SCORE_KIND,
        "score_unit": "sum of natural-log token probabilities (unnormalized; <=0; larger is better; NOT win rate/Q)",
        "records": records,
        "privacy": {"hidden_opponent_hands_stored": False, "raw_payload_stored": False,
                    "tensor_values_stored": False, "probability_vectors_stored": False,
                    "private_paths_stored": False},
    }
    coverage = {
        "schema": "guance-phase3-action-family-coverage-v1",
        "observed": dict(sorted(families.items())),
        "phases": {"lead": sum(1 for record in records
                               if record["phase"] == "LEAD_PLAY"),
                   "follow": sum(1 for record in records
                                 if record["phase"] == "FOLLOW_PLAY")},
        "requested": {
            "lead": any(record["phase"] == "LEAD_PLAY" for record in records),
            "follow": any(record["phase"] == "FOLLOW_PLAY" for record in records),
            "pass": families.get("pass", 0) > 0,
            "single": families.get("single", 0) > 0,
            "pair": families.get("pair", 0) > 0,
            "triple": families.get("triple", 0) > 0,
            "sequence": any(families.get(name, 0) > 0 for name in
                            ("straight", "serial_pair", "serial_triple", "sflush")),
            "bomb": families.get("bomb", 0) > 0,
            "wildcard_declaration": any(record.get("wildcard") for record in records)},
        "unverified": [],
    }
    for name, covered in coverage["requested"].items():
        if not covered:
            coverage["unverified"].append(name)
    # Bounded reports go to the pytest temporary directory (system temp); this
    # test never rewrites the fixed Phase 3 evidence root, so an independent
    # verifier sees unchanged evidence bytes/hash/mtime.
    reports = tmp_path / "phase3-recommendation-reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "phase3-actual-recommendation-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (reports / "phase3-action-family-coverage.json").write_text(
        json.dumps(coverage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert summary["recommendation_count"] >= 1
    assert all(record["valid"] for record in records)
    required = {name: value for name, value in coverage["requested"].items()
                if name != "wildcard_declaration"}
    assert all(required.values()), coverage


def test_real_iter2000_rejects_illegal_phase_metadata_without_recommendation(monkeypatch):
    """Illegal phase/current metadata must yield no recommendation from the real model."""
    from guance.infrastructure.model.marvel_v2.decision_request import decision_identity_sha256

    fixtures = _synthetic_fixtures()
    follow_payload = copy.deepcopy(fixtures[9][0])  # follow-single (FOLLOW_PLAY)

    client = _RealMarvelClient(monkeypatch)
    try:
        client.start()
        # (a) client-side strict rejection: FOLLOW_PLAY without the declaration.
        missing = copy.deepcopy(follow_payload)
        missing["current_winning_declaration"] = None
        request = ConfirmedDecisionRecommendationRequest(
            RequestIdentity("", missing["session_id"], missing["game_id"],
                            missing["state_version"]), missing)
        with pytest.raises(ModelBackendError) as exc:
            client.client.submit(request)
        assert exc.value.code == "MODEL_INPUT_UNSUPPORTED"
        assert client.results == []

        # (b) parser-valid but detached from the oracle's legal-row identity:
        # the real worker rejects it before any model work and returns no result.
        tampered = copy.deepcopy(follow_payload)
        tampered["session"]["candidates_sha256"] = "0" * 64
        tampered["decision_identity_sha256"] = decision_identity_sha256(tampered)
        request = ConfirmedDecisionRecommendationRequest(
            RequestIdentity("", tampered["session_id"], tampered["game_id"],
                            tampered["state_version"]), tampered)
        request.validate()
        request_id = client.client.submit(request, timeout_seconds=300.0)
        assert isinstance(request_id, str) and request_id
        assert _wait_for(lambda: any(item.get("model_status") in {"error", "timeout"}
                                     for item in client.statuses[-4:]), 300)
        assert client.results == []
        errors = [item for item in client.statuses if item.get("model_status") == "error"]
        assert errors and errors[-1].get("error_code") in {
            "MODEL_INPUT_UNSUPPORTED", "MODEL_INFERENCE_FAILED"}

        # (c) PM counterexample A (FOLLOW_PLAY, active_seat=0,
        # current_winner_seat=0) with a fully recomputed identity: never a
        # recommendation, never a fallback.
        illegal = copy.deepcopy(follow_payload)
        illegal["marvel_state"]["current_winner_seat"] = illegal["marvel_state"]["active_seat"]
        assert (illegal["session"]["phase"], illegal["marvel_state"]["active_seat"],
                illegal["marvel_state"]["current_winner_seat"]) == ("FOLLOW_PLAY", 0, 0)
        illegal["decision_identity_sha256"] = decision_identity_sha256(illegal)
        request = ConfirmedDecisionRecommendationRequest(
            RequestIdentity("", illegal["session_id"], illegal["game_id"],
                            illegal["state_version"]), illegal)
        with pytest.raises(ModelBackendError) as exc:
            client.client.submit(request)
        assert exc.value.code == "MODEL_INPUT_UNSUPPORTED"
        assert client.results == []

        # (d) The same impossible topology on a real capture-derived decision
        # that the fixed capture pipeline actually produced (previously it could
        # generate a recommendation): recomputed identity, still zero results.
        from guance.infrastructure.model.marvel_v2.decision_request import (
            project_confirmed_decision,
        )
        decisions, _identity_fn = _capture_decisions()
        capture_follow = next(decision for decision in decisions
                              if decision.action_session.phase == "FOLLOW_PLAY")
        capture_payload = project_confirmed_decision(
            capture_follow, artifact_id=MODEL_VERSION, artifact_sha256=CHECKPOINT_SHA256)
        illegal_capture = copy.deepcopy(capture_payload)
        illegal_capture["marvel_state"]["current_winner_seat"] = \
            illegal_capture["marvel_state"]["active_seat"]
        illegal_capture["decision_identity_sha256"] = decision_identity_sha256(illegal_capture)
        request = ConfirmedDecisionRecommendationRequest(
            RequestIdentity("", illegal_capture["session_id"], illegal_capture["game_id"],
                            illegal_capture["state_version"]), illegal_capture)
        with pytest.raises(ModelBackendError) as exc:
            client.client.submit(request)
        assert exc.value.code == "MODEL_INPUT_UNSUPPORTED"
        assert client.results == []
    finally:
        client.close()


def _control_ids(session_id="", game_id="", state_version=0):
    return {"request_id": uuid.uuid4().hex, "session_id": session_id, "game_id": game_id,
            "state_version": state_version, "epoch": 0, "generation": 0}


class _RawMarvelWorker:
    """Real model worker process (production launch spec) driven by raw JSONL.

    The generic worker protocol is exercised directly so the provider-side
    re-parse can be reached without the client's own convenience pre-check.
    """

    def __init__(self, monkeypatch):
        _configure_environment(monkeypatch)
        probe = InferenceClient(BUNDLE / "manifest.json", on_result=lambda _result: None,
                                on_status=lambda _status: None,
                                timeout_seconds=600.0, threads=2)
        try:
            self.command, self.env, self.flags = probe._worker_process_spec()
        finally:
            probe.close()
        self.process = None
        self.messages = queue.Queue()

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

        threading.Thread(target=read, name="guance-raw-worker-reader", daemon=True).start()
        return self

    def send(self, kind, ids, **body):
        assert self.process is not None and self.process.stdin is not None
        self.process.stdin.write(dumps(envelope(kind, ids, **body)))
        self.process.stdin.flush()

    def receive(self, timeout=600.0):
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


def test_real_iter2000_rejects_r1_r2_r3_trick_topology(monkeypatch):
    """Repair-3 R1/R2/R3 zero-result negatives through the real chain.

    (a) The production InferenceClient (real bundle loaded) refuses each
        identity-recomputed counterexample before the wire.
    (b) The same self-consistent payloads are written raw to a real candidate
        worker process (production launch spec, Phase 2 frozen runtime, real
        iter2000); the worker/provider parser must reject them before any model
        forward with a bounded error and no result.
    (c) No fallback / no state mutation: the same worker still produces a real
        iter2000 recommendation for a valid confirmed decision afterwards.
    """
    from guance.infrastructure.model.marvel_v2.decision_request import (
        decision_identity_sha256,
    )

    counterexamples = {
        "R1": _trick_counterexample_r1(),
        "R2": _trick_counterexample_r2(),
        "R3": _trick_counterexample_r3(),
    }
    for label, payload in counterexamples.items():
        assert payload["decision_identity_sha256"] == decision_identity_sha256(payload), label

    client = _RealMarvelClient(monkeypatch)
    try:
        client.start()
        assert client.loaded_metadata.get("provider") == "guandan_marvel_v2"
        for label, payload in counterexamples.items():
            request = ConfirmedDecisionRecommendationRequest(
                RequestIdentity("", payload["session_id"], payload["game_id"],
                                payload["state_version"]), payload)
            with pytest.raises(ModelBackendError) as exc:
                client.client.submit(request)
            assert exc.value.code == "MODEL_INPUT_UNSUPPORTED", label
            assert client.results == [], label
    finally:
        client.close()

    fixtures = _synthetic_fixtures()
    valid_payload, valid_context = fixtures[0]
    with _RawMarvelWorker(monkeypatch) as worker:
        worker.send("load", _control_ids(), source=str(BUNDLE / "manifest.json"))
        while True:
            reply = worker.receive()
            if reply.get("type") == "loaded":
                break
            assert reply.get("type") == "status" and reply.get("model_status") == "loading"
        assert worker.process.poll() is None
        for label, payload in counterexamples.items():
            ids = _control_ids(payload["session_id"], payload["game_id"],
                               payload["state_version"])
            worker.send("recommend", ids, request_kind="confirmed_decision",
                        decision=payload)
            reply = worker.receive()
            assert reply.get("type") == "error", (label, reply)
            assert reply.get("error_code") == "MODEL_INPUT_UNSUPPORTED", (label, reply)
            assert "result" not in reply, label
            assert worker.process.poll() is None, label
        ids = _control_ids(valid_payload["session_id"], valid_payload["game_id"],
                           valid_payload["state_version"])
        worker.send("recommend", ids, request_kind="confirmed_decision",
                    decision=valid_payload)
        reply = worker.receive(timeout=420.0)
        assert reply.get("type") == "result", reply
        result = decode_canonical_advice(reply["result"])
        record = _validate_result(valid_context, result)
        assert record["valid"] is True
