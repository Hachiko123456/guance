"""D10 DanKS-specific local_capture replay tests (explicitly marked).

Scope (phase 1 frozen): real captures prove frame-continuous public chains
whose live plays carry NO verified declaration, so every real-capture
decision point is model-INELIGIBLE for DanKS until an upstream declaration
source exists - the positive real-input gate stays BLOCKED.  The positive
replay here therefore runs the synthetic normal-entry chains (provenance
explicitly tagged) through the real provider with the real weights, while
the negative controls run against real capture input and the frozen
missing-PASS fixtures.

Run explicitly: ``python -B -m pytest -q -m local_capture tests/local_capture/test_danks_v3_replay.py``
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest

from guance.contracts import CanonicalPublicGameView, SourceFrame
from guance.domain.session import SessionEngine
from guance.infrastructure.model.danks_v3.history import live_capture

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

pytestmark = pytest.mark.local_capture

MANIFEST = ROOT / "tests" / "local_capture_manifest.json"
CAPTURES = ROOT / "debug" / "captures"
PHASE1 = ROOT / "tests/fixtures/danks_v3"


def _clean_capture() -> dict:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return next(item for item in manifest
                if item["capture_id"] == "20260930-012733")


def _replay_final_state(capture_id: Path, session_id: str) -> SessionEngine:
    from guance.infrastructure.capture.offline_replay import iter_debug_frames
    from guance.infrastructure.capture.live_subset import NormalExchangeLiveSubsetProvider
    from guance.infrastructure.model.manifest import sha256_file
    from guance.infrastructure.protocol import live
    assert sha256_file(capture_id) == _clean_capture()["source_sha256"]
    provider = NormalExchangeLiveSubsetProvider(None)
    provider.bind_capture(capture_id.parent.name)
    engine = SessionEngine(session_id, aggregate_parser=live.parse_aggregate,
                           accepted_host=live.GAME_HOST,
                           event_adapter=provider.adapt_event,
                           event_observer=provider.observe_applied,
                           reset_observer=provider.reset_session)
    for replay in iter_debug_frames(capture_id):
        updates = engine.feed(replay.frame)
        assert not any(update.kind == "error" for update in updates)
    assert not engine.paused
    return engine


def test_real_capture_danks_ineligible_no_recommendation():
    """The real capture reaches the observer's follow turns with every play
    declaration unsupported: the DanKS eligibility gate must refuse a
    recommendation for that state (fail-closed), recorded as the real-input
    negative control for D10."""
    fixture = _clean_capture()
    frames = CAPTURES / fixture["capture_id"] / "frames.jsonl"
    engine = _replay_final_state(frames, "danks-d10-real-capture")
    state = engine.state
    assert state is not None and state.history
    declarations = [action.declaration.status.value for action in state._v2_history]
    assert declarations
    assert any(status == "unsupported" for status in declarations)
    provenance = live_capture()
    assert provenance.eligible is False
    assert provenance.public_history_complete == "unsupported"
    # The eligibility decision the provider would make for this state:
    from guance.infrastructure.model.danks_v3.adapter import eligibility
    verdict = eligibility(None, None, provenance)
    assert verdict["eligible"] is False
    assert verdict["code"] == "HISTORY_PROVENANCE_UNVERIFIED"


@pytest.mark.parametrize("fixture_name", [
    "history_negative_delete_pass_gap",
    "history_negative_delete_pass_renumbered",
])
def test_missing_pass_variants_rejected_before_any_advice(fixture_name: str):
    """The frozen delete-PASS mutations are rejected by the state machine
    before an observation exists - no advice can be produced or displayed."""
    fixture = json.loads((PHASE1 / "fixtures" / f"{fixture_name}.json").read_text(
        encoding="utf-8"))
    from dataclasses import replace as _replace
    from guance.domain.events import ActionEvent, DealEvent, DeclaredActionCommitted, StartEvent
    from guance.public_state_contract import PublicDeclaration
    events = []
    for item in fixture["events"]:
        if item["type"] == "deal":
            events.append(DealEvent(item["my_seat"], item["level"],
                                    tuple(item["hand"])))
        elif item["type"] == "start":
            events.append(StartEvent(item["first_seat"]))
        else:
            action = ActionEvent(**{**item["action"],
                                    "cards": tuple(item["action"]["cards"])})
            if item["type"] == "declared_action":
                events.append(DeclaredActionCommitted(
                    action, PublicDeclaration.from_dict(item["declaration"])))
            else:
                events.append(action)
    by_payload = {str(index).encode(): event for index, event in enumerate(events)}

    def parse(payload, meta):
        event = by_payload[payload]
        if isinstance(event, DeclaredActionCommitted):
            return (_replace(event, action=_replace(event.action, meta=meta)),)
        return (_replace(event, meta=meta),)

    engine = SessionEngine("danks-d10-negative", aggregate_parser=parse,
                           accepted_host="synthetic-host")
    errors = []
    for position, sequence in enumerate(fixture["transport_sequences"]):
        updates = engine.feed(SourceFrame("synthetic-host", str(position).encode(),
            float(sequence), "synthetic-connection", sequence, "server"))
        errors.extend({"code": update.error_code, "message": update.message}
                      for update in updates if update.kind == "error")
        if errors:
            break
        if position == 1:
            engine.confirm_my_seat_relative("right")
    assert errors and not engine.latest_observation
    expected = fixture["expected"]
    if "error_code" in expected:
        assert errors[0]["code"] == expected["error_code"]


def test_positive_replay_runs_on_synthetic_normal_entry_real_weights():
    """Positive DanKS replay: the synthetic normal-entry chain (provenance
    explicitly tagged) through the REAL provider and REAL weights reaches the
    delivery boundary.  The real-input positive stays BLOCKED (see the module
    docstring and the phase 1 spec section 5) and is recorded as such."""
    checkpoint = Path(__import__("os").environ.get(
        "GUANCE_DANKS_CHECKPOINT", "E:/Downloads/top10_selector_v3_online.pt"))
    if not checkpoint.is_file():
        pytest.skip("frozen checkpoint not on this host")
    sys.path.insert(0, str(ROOT / "tests" / "model"))
    from test_danks_v3_declaration_ipc import (  # noqa: E402
        build_chain, club_substituted_declaration, observer_hand,
    )
    from guance.contracts import CurrentWinningDeclarationFact, DeclaredFacts
    from guance.infrastructure.model.danks_v3.provider import DanksV3Advisor, build_bundle

    engine = build_chain(club_substituted_declaration(), "danks-d10-positive")
    view = CanonicalPublicGameView.from_observation(engine.latest_observation)
    winning = engine.latest_public_state_v2.current_winning
    declaration = winning.value.declaration.value
    facts = DeclaredFacts(schema="Guandan.DeclaredFacts.v2",
        current_winning_declaration=CurrentWinningDeclarationFact(
            status="known", declaration=declaration),
        history_origin="synthetic_normal_entry",
        public_history_complete="known", history_complete_value=True,
        transport_contiguous="known", transport_value=True)
    bundle = build_bundle(checkpoint, ROOT / "build" / "danks-v3-d10-bundle")
    advisor = DanksV3Advisor(bundle, requested_device="cpu")
    advisor.load()
    result = advisor.recommend(view, "d10-positive-request", declared_facts=facts)
    assert result.status == "ready" and 1 <= len(result.candidates) <= 5
    assert {c.kind for c in result.candidates} <= {
        "pass", "single", "pair", "triple", "full", "straight", "serial_pair",
        "serial_triple", "bomb", "sflush", "rocket"}
    hand = set(view.hand)
    for candidate in result.candidates:
        assert set(candidate.cards) <= hand
    # Scope record: the chain provenance is synthetic normal entry.
    provenance = live_capture()
    assert provenance.eligible is False  # real-capture provenance stays blocked
    hand_hash = hashlib.sha256(json.dumps(
        list(observer_hand()), sort_keys=True).encode()).hexdigest()
    assert len(hand_hash) == 64
