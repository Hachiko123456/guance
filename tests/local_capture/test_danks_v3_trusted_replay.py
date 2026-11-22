"""Trusted-replay projection + scheduling tests (V2 contract).

The production projection (``guance.application.trusted_replay``) runs over
the real capture ``20261014-143204`` through the exact production chain
(iter_debug_frames -> live.parse_aggregate -> SessionEngine ->
NormalExchangeLiveSubsetProvider confirmed decisions).  Golden view/fact
hashes for the probed rows come from the frozen baseline report (34 verified
states; report sha256 ``fc544fad…3f80``).  Tampered inputs must fail closed
to None.  The scheduling tests bind the real ``GuanceController`` scheduling
methods against a real ready DanKS ModelService status and the real queued
decisions: one submission per confirmed identity, deferred confirmations
submit once while it is still the observer's turn, expired or duplicate
confirmations never produce advice, and insufficient evidence clears the
advice with a recorded reason without poisoning the model.

Run explicitly: ``python -B -m pytest -q -m local_capture
tests/local_capture/test_danks_v3_trusted_replay.py``
"""
from __future__ import annotations

from dataclasses import replace as dataclass_replace
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import MethodType, SimpleNamespace

import pytest

pytestmark = pytest.mark.local_capture

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

CAPTURE = "20261014-143204"
CAPTURE_SHA = "29e91cf3ffc3169bb671b238fe7549944e60a9bc8b0a9a61d47192f6ba0e5e48"
RULES_COMMIT = "e5c82e7c27fb366800e9722c9e8e4720df7db01f"
RULES_REPO = Path("D:/project/guandan")
# Golden (observation, verified-facts) hashes for two probe rows, taken from
# the frozen baseline report; row numbers are report coordinates, never a
# production behavior switch.
GOLDEN_ROWS = {
    416: ("782c8352fffb4881e59e988ca4ff9998cebd0320f59ee42bacfcf990a47c5f8b",
          "326fb6c93e86369641c860c2bc2f5435cd07ee6146099eaa256d6f5df0312d0b"),
    1246: ("2763c7f5fdd99f7a238f60e804b7172dfaff896aff7db3f463e71f6dbbbc056d",
           "3d28f506df816959d2f60b55e30640d50fadc5d2f4c412b27f60092cb2bd98d5"),
}


def _document_hash(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return sha256(raw).hexdigest()


@pytest.fixture(scope="module")
def rules_dir(tmp_path_factory):
    if not RULES_REPO.is_dir():
        pytest.skip("frozen rules repository not on this host")
    from guance.infrastructure.model.marvel_v2.rules_oracle import (
        _SOURCE_FILES, VerifiedGuandanRulesOracle)
    destination = tmp_path_factory.mktemp("trusted-replay-rules")
    for name, expected in _SOURCE_FILES.items():
        data = subprocess.check_output(
            ["git", "--no-optional-locks", "-C", str(RULES_REPO),
             "show", f"{RULES_COMMIT}:{name}"])
        data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        assert (len(data), sha256(data).hexdigest()) == expected, name
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return destination


@pytest.fixture(scope="module")
def projections(rules_dir):
    """Row -> (view, facts) for every verified decision the projection accepts."""
    from guance.application.trusted_replay import project_trusted_replay_facts
    from guance.domain.session import SessionEngine
    from guance.infrastructure.capture.live_subset import NormalExchangeLiveSubsetProvider
    from guance.infrastructure.capture.offline_replay import iter_debug_frames
    from guance.infrastructure.protocol import live

    path = ROOT / "debug" / "captures" / CAPTURE / "frames.jsonl"
    assert sha256(path.read_bytes()).hexdigest() == CAPTURE_SHA
    provider = NormalExchangeLiveSubsetProvider(rules_dir)
    provider.bind_capture(CAPTURE)
    engine = SessionEngine("real-verified-" + CAPTURE,
                           aggregate_parser=live.parse_aggregate,
                           accepted_host=live.GAME_HOST,
                           event_adapter=provider.adapt_event,
                           event_observer=provider.observe_applied,
                           reset_observer=provider.reset_session)
    projected: dict[int, tuple] = {}
    for frame in iter_debug_frames(path):
        engine.feed(frame.frame)
        # The controller submits during update handling (before the drain):
        # peek this frame's confirmed decisions, project, then release them.
        for decision in provider.peek_decisions():
            if decision.source_sequence != frame.frame.sequence or engine.paused:
                continue
            observation = engine.latest_observation
            if observation is None:
                continue
            verified = project_trusted_replay_facts(observation, provider)
            if verified is not None:
                view, facts = verified
                assert frame.row not in projected
                projected[frame.row] = (view, facts)
        provider.drain_decisions()
    return projected


def test_projection_covers_the_34_verified_states(projections):
    assert len(projections) == 34
    for _row, (view, facts) in projections.items():
        assert facts.history_origin == "live_capture"
        assert facts.public_history_complete == "known"
        assert facts.history_complete_value is True
        assert facts.transport_contiguous == "known"
        assert facts.transport_value is True
        if view.free_lead:
            assert facts.current_winning_declaration.status == "absent"
        else:
            assert facts.current_winning_declaration.status == "known"
        # The normalized view carries the declaration-canonical card order
        # (multiset-verified before normalization), so the facts bind.
        facts.validate_for_view(view)


def test_projection_matches_the_frozen_report_goldens(projections):
    for row, (observation_sha, facts_sha) in GOLDEN_ROWS.items():
        view, facts = projections[row]
        assert _document_hash(view.to_dict()) == observation_sha, row
        assert _document_hash(facts.to_dict()) == facts_sha, row


def test_projection_fails_closed_without_a_matching_decision(projections,
                                                             rules_dir):
    from guance.application.trusted_replay import project_trusted_replay_facts
    from guance.infrastructure.capture.live_subset import NormalExchangeLiveSubsetProvider
    view, _facts = projections[416]
    empty_provider = NormalExchangeLiveSubsetProvider(rules_dir)
    assert project_trusted_replay_facts(_observation_for(view), empty_provider) \
        is None


def test_projection_fails_closed_on_tampered_observation(projections):
    from guance.application.trusted_replay import project_trusted_replay_facts
    from guance.infrastructure.capture.live_subset import NormalExchangeLiveSubsetProvider
    view, _facts = projections[416]
    provider = NormalExchangeLiveSubsetProvider(None)
    observation = _observation_for(view)
    tampered_hand = tuple(observation.hand[:-1]
                          + ((observation.hand[0] ^ 0x40),))
    assert project_trusted_replay_facts(
        dataclass_replace(observation, hand=tampered_hand), provider) is None
    tampered_remaining = tuple(observation.remaining[:-1]
                               + (observation.remaining[-1] + 1,))
    assert project_trusted_replay_facts(
        dataclass_replace(observation, remaining=tampered_remaining),
        provider) is None
    assert project_trusted_replay_facts(
        dataclass_replace(observation, state_version=observation.state_version + 1),
        provider) is None


def _observation_for(view):
    """Rebuild the live GameObservation shape a controller update carries."""
    from guance.contracts import GameObservation
    return GameObservation(view.session_id, view.game_id, view.state_version,
                           0.0, view.level, view.my_seat, view.first_seat,
                           view.current_seat, tuple(view.hand),
                           tuple(view.remaining), tuple(view.public_history),
                           view.lead_action, view.free_lead, view.trick)


# --- V2 scheduling ---------------------------------------------------------

@pytest.fixture(scope="module")
def make_queued(rules_dir):
    """Factory: replay until the first verified decision point; return the
    live observation, the provider with that decision still queued, and the
    verified projection."""
    from guance.application.trusted_replay import project_trusted_replay_facts
    from guance.domain.session import SessionEngine
    from guance.infrastructure.capture.live_subset import NormalExchangeLiveSubsetProvider
    from guance.infrastructure.capture.offline_replay import iter_debug_frames
    from guance.infrastructure.protocol import live

    def _make():
        path = ROOT / "debug" / "captures" / CAPTURE / "frames.jsonl"
        provider = NormalExchangeLiveSubsetProvider(rules_dir)
        provider.bind_capture(CAPTURE)
        engine = SessionEngine("real-verified-" + CAPTURE,
                               aggregate_parser=live.parse_aggregate,
                               accepted_host=live.GAME_HOST,
                               event_adapter=provider.adapt_event,
                               event_observer=provider.observe_applied,
                               reset_observer=provider.reset_session)
        for frame in iter_debug_frames(path):
            engine.feed(frame.frame)
            observation = engine.latest_observation
            if (provider.peek_decisions() and observation is not None
                    and not engine.paused
                    and project_trusted_replay_facts(observation, provider)
                    is not None):
                view, facts = project_trusted_replay_facts(observation, provider)
                decision = provider.peek_decisions()[0]
                return observation, provider, view, facts, decision
            provider.drain_decisions()
        raise AssertionError("no verified decision point found")
    return _make


def _ready_service(provider_name: str):
    import threading
    from guance.application.model_service import ModelService
    from guance.contracts import ModelLifecycleState, ModelMetadata, ModelStatusSnapshot
    service = ModelService.__new__(ModelService)
    service._lock = threading.Lock()
    service._client = None
    service._status = ModelStatusSnapshot(
        state=ModelLifecycleState.READY,
        metadata=ModelMetadata(provider=provider_name, model_version="v",
                               status="loaded"))
    return service


class _ControllerStub:
    """The controller attribute surface the scheduling methods touch."""

    def __init__(self, service, provider, observation, danks_active=True):
        self.session = SimpleNamespace(paused=False)
        self._model = service
        self._live_subset = provider
        self._table_game_key = None
        self._table_seat_confirmed = True
        self._selection_in_progress = False
        self._selection_failed = False
        self._no_selection = False
        self._latest_model_observation = observation
        self._active_advice_key = observation.key
        self._advice_submitted = set()
        self._advice_pending = {}
        self._declared_facts_for = lambda obs: "FALLBACK-FACTS"
        self._declared_facts_consumer_active = lambda: danks_active
        self.viewmodel_calls = []
        self._status = lambda _status: None
        self.viewmodel = SimpleNamespace(
            clear_advice=lambda reason="": self.viewmodel_calls.append(
                ("clear", reason)),
            apply_observation=lambda data: None)
        self.log_calls = []
        self.log = SimpleNamespace(
            write=lambda event, **fields: self.log_calls.append((event, fields)))
        self.submits = []
        service.submit = (lambda observation, **kwargs:
                          self.submits.append((observation, kwargs)))
        # Bind the real reason-recording helper (imports lazily to avoid a
        # controller import at module scope of the local_capture marker).
        from guance.application.controller import GuanceController
        self._record_advice_deferred = MethodType(
            GuanceController._record_advice_deferred, self)
        self._turn_still_eligible = MethodType(
            GuanceController._turn_still_eligible, self)
        self._submit_danks_advice = MethodType(
            GuanceController._submit_danks_advice, self)


def test_controller_submits_verified_facts_for_danks(make_queued):
    """A ready DanKS family model submits the normalized view with the
    verified facts exactly once per identity; a duplicate observation update
    never re-submits; non-DanKS families keep the verbatim submission."""
    from guance.application.controller import GuanceController
    from guance.contracts import GameObservation

    observation, queued_provider, view, facts, _decision = make_queued()
    budget = 6.0 if view.free_lead else 2.0

    stub = _ControllerStub(_ready_service("danks_v3"), queued_provider,
                           observation)
    MethodType(GuanceController._submit_public_advice, stub)(observation)
    assert len(stub.submits) == 1
    submitted_view, kwargs = stub.submits[0]
    assert not isinstance(submitted_view, GameObservation)
    assert submitted_view.key == view.key
    assert kwargs["declared_facts"].history_origin == "live_capture"
    assert kwargs["declared_facts"].history_declarations
    assert kwargs["budget_seconds"] == budget
    assert submitted_view is not observation
    # Duplicate observation update for the same identity: no second submit.
    MethodType(GuanceController._submit_public_advice, stub)(observation)
    assert len(stub.submits) == 1

    # Non-DanKS family: verbatim submission behavior unchanged.
    stub = _ControllerStub(_ready_service("guandan_marvel_v2"),
                           queued_provider, observation, danks_active=False)
    MethodType(GuanceController._submit_public_advice, stub)(observation)
    assert stub.submits == [(observation, {"declared_facts": "FALLBACK-FACTS",
                                           "budget_seconds": budget})]


def test_insufficient_evidence_defers_and_never_submits(make_queued):
    """No verified decision: the advice is cleared with a recorded reason, the
    turn is registered for deferred confirmation, nothing is submitted, and
    repeated updates for the same identity stay silent."""
    from guance.application.controller import GuanceController
    from guance.infrastructure.capture.live_subset import NormalExchangeLiveSubsetProvider

    observation, _queued_provider, _view, _facts, _decision = make_queued()
    stub = _ControllerStub(_ready_service("danks_v3"),
                           NormalExchangeLiveSubsetProvider(None), observation)
    MethodType(GuanceController._submit_public_advice, stub)(observation)
    assert stub.submits == []
    assert observation.key in stub._advice_pending
    assert stub.viewmodel_calls and stub.viewmodel_calls[0][0] == "clear"
    assert stub.log_calls
    assert stub.log_calls[0][0] == "advice_deferred_insufficient_evidence"
    stub.viewmodel_calls.clear()
    stub.log_calls.clear()
    MethodType(GuanceController._submit_public_advice, stub)(observation)
    assert stub.submits == [] and stub.viewmodel_calls == [] and not stub.log_calls


def test_deferred_confirmation_submits_once_when_still_my_turn(make_queued):
    """A trusted confirmation arriving on a later frame submits exactly once
    while it is still the observer's turn; duplicates never re-submit."""
    from guance.application.controller import GuanceController

    observation, _queued_provider, view, _facts, decision = make_queued()
    from guance.infrastructure.capture.live_subset import NormalExchangeLiveSubsetProvider
    later_provider = NormalExchangeLiveSubsetProvider(None)
    stub = _ControllerStub(_ready_service("danks_v3"), later_provider,
                           observation)
    submit = MethodType(GuanceController._submit_public_advice, stub)
    flush = MethodType(GuanceController._flush_deferred_advice, stub)
    submit(observation)  # turn arrives without a verified decision: deferred
    assert stub.submits == [] and observation.key in stub._advice_pending
    later_provider._decisions.append(decision)  # confirmation arrives later
    flush()  # the confirmation arrives while still my turn
    assert len(stub.submits) == 1
    submitted_view, kwargs = stub.submits[0]
    assert submitted_view.key == view.key
    assert kwargs["declared_facts"].history_origin == "live_capture"
    assert observation.key not in stub._advice_pending
    assert observation.key in stub._advice_submitted
    flush()  # duplicate confirmation: no second advice
    assert len(stub.submits) == 1


def test_expired_confirmation_never_produces_advice(make_queued):
    """Once the state moved past the pending turn (or it is no longer the
    observer's seat), a late confirmation is dropped without advice."""
    from guance.application.controller import GuanceController
    from guance.infrastructure.capture.live_subset import NormalExchangeLiveSubsetProvider

    observation, _queued_provider, _view, _facts, _decision = make_queued()
    provider = NormalExchangeLiveSubsetProvider(None)
    stub = _ControllerStub(_ready_service("danks_v3"), provider, observation)
    MethodType(GuanceController._submit_public_advice, stub)(observation)
    assert stub.submits == [] and observation.key in stub._advice_pending

    # The state advanced to the next state version: the pending turn expires.
    moved = _ControllerStub(_ready_service("danks_v3"), provider, observation)
    moved._latest_model_observation = dataclass_replace(
        observation, state_version=observation.state_version + 1)
    moved._active_advice_key = moved._latest_model_observation.key
    moved._advice_pending = dict(stub._advice_pending)
    MethodType(GuanceController._flush_deferred_advice, moved)()
    assert moved.submits == [] and moved._advice_pending == {}

    # No longer my seat: expires as well.
    passed = _ControllerStub(_ready_service("danks_v3"), provider, observation)
    passed._latest_model_observation = dataclass_replace(
        observation, current_seat=(observation.my_seat + 1) % 4)
    passed._active_advice_key = observation.key
    passed._advice_pending = dict(stub._advice_pending)
    MethodType(GuanceController._flush_deferred_advice, passed)()
    assert passed.submits == [] and passed._advice_pending == {}
