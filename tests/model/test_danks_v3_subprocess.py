"""D06/D07 real-subprocess chain tests for the DanKS V3 provider.

These tests launch the REAL worker subprocess (the production client launch
spec: isolated Python, guance bootstrap, --model-worker), load the real
frozen weights from a materialized bundle, submit V2 requests with declared
facts, and verify the recommendation, identity binding, stale rejection and
restart behavior end to end.  All skip when the frozen checkpoint is absent.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from guance.contracts import (CanonicalPublicGameView,
                              CurrentWinningDeclarationFact, DeclaredFacts,
                              PublicRecommendationRequestV2, RequestIdentity)
from guance.infrastructure.model.client import InferenceClient
from guance.infrastructure.model.errors import ModelBackendError
from guance.infrastructure.model.danks_v3.provider import build_bundle
from guance.public_state_contract import PublicDeclaration

ROOT = Path(__file__).resolve().parents[2]
PHASE1 = ROOT / "tests/fixtures/danks_v3"
CHECKPOINT_PATH = Path(os.environ.get(
    "GUANCE_DANKS_CHECKPOINT", "E:/Downloads/top10_selector_v3_online.pt"))
_real_checkpoint = pytest.mark.skipif(
    not CHECKPOINT_PATH.is_file() or not (PHASE1 / "samples_32.json").is_file(),
    reason="frozen checkpoint or samples not on this host")


def _wild03() -> tuple[CanonicalPublicGameView, DeclaredFacts]:
    samples = json.loads((PHASE1 / "samples_32.json").read_text(encoding="utf-8"))
    sample = next(row for row in samples["samples"]
                  if row["sample_id"] == "danks-p1-wild-03")
    view = CanonicalPublicGameView.from_dict(sample["observation"])
    declaration = PublicDeclaration.from_dict(
        sample["current_winning_declaration_fact"]["declaration"])
    facts = DeclaredFacts(schema="Guandan.DeclaredFacts.v2",
        current_winning_declaration=CurrentWinningDeclarationFact(
            status="known", declaration=declaration),
        history_origin="synthetic_normal_entry",
        public_history_complete="known", history_complete_value=True,
        transport_contiguous="known", transport_value=True)
    return view, facts


@_real_checkpoint
def test_real_subprocess_recommendation_end_to_end(tmp_path: Path):
    """D06 main chain: client -> real worker subprocess -> danks_v3 provider
    -> real weights -> shared validation -> on_result, identity bound."""
    bundle = build_bundle(CHECKPOINT_PATH, tmp_path / "bundle")
    view, facts = _wild03()
    identity = RequestIdentity("e2e-req-1", view.session_id, view.game_id,
                               view.state_version)
    received: list = []
    done = threading.Event()
    statuses: list[dict] = []
    client = InferenceClient(str(bundle.root),
                             on_result=lambda r: (received.append(r), done.set()),
                             on_status=statuses.append, timeout_seconds=30.0,
                             threads=1, requested_device="cpu")
    request = PublicRecommendationRequestV2(identity, view, facts)
    try:
        returned = client.submit(request)
        assert returned
        assert done.wait(120.0), "no result within 120 s"
        result = received[-1]
        assert result.request_id == returned
        assert result.key == view.key
        assert result.status == "ready" and 1 <= len(result.candidates) <= 5
        assert result.score_kind == "danks_v3_selector_logit"
        assert result.provider == "danks_v3"
        assert {c.kind for c in result.candidates} <= {"pass", "bomb", "sflush",
                                                       "rocket"}
        # The client reports its own lifecycle statuses: loading -> ready.
        ready = [s for s in statuses if s.get("model_status") == "ready"]
        assert ready and ready[-1]["metadata"]["provider"] == "danks_v3"
        assert ready[-1]["metadata"]["actual_device"] == "cpu"
    finally:
        client.close()


@_real_checkpoint
def test_real_subprocess_stale_generation_is_dropped(tmp_path: Path):
    """D07: a superseded generation's late result is dropped by identity."""
    bundle = build_bundle(CHECKPOINT_PATH, tmp_path / "bundle")
    view, facts = _wild03()
    received: list = []
    statuses: list[dict] = []
    client = InferenceClient(str(bundle.root), on_result=received.append,
                             on_status=statuses.append, timeout_seconds=30.0,
                             threads=1, requested_device="cpu")
    try:
        first = PublicRecommendationRequestV2(
            RequestIdentity("stale-1", view.session_id, view.game_id,
                            view.state_version), view, facts)
        returned_first = client.submit(first)
        assert returned_first
        # A second submit for the same state supersedes the first generation.
        second = PublicRecommendationRequestV2(
            RequestIdentity("stale-2", view.session_id, view.game_id,
                            view.state_version), view, facts)
        returned_second = client.submit(second)
        assert returned_second and returned_second != returned_first
        deadline = time.monotonic() + 120.0
        while not received and time.monotonic() < deadline:
            time.sleep(0.05)
        assert received, "no result for the latest generation"
        assert {r.request_id for r in received} == {returned_second}
        # The superseded queued request is skipped before the wire (identity
        # check in the send loop), so it never produces a callback at all -
        # stronger than a late drop.  The only delivered result belongs to
        # the latest generation and matches the submitted view identity.
        assert received[-1].key == view.key
    finally:
        client.close()


@_real_checkpoint
def test_real_subprocess_restart_reloads_and_recommends(tmp_path: Path):
    """D07: close() then a fresh client re-loads the same bundle and serves a
    new recommendation (no silent reuse of the dead process)."""
    bundle = build_bundle(CHECKPOINT_PATH, tmp_path / "bundle")
    view, facts = _wild03()

    def run_one() -> CanonicalPublicGameView:
        received: list = []
        client = InferenceClient(str(bundle.root), on_result=received.append,
                                 on_status=lambda s: None, timeout_seconds=30.0,
                                 threads=1, requested_device="cpu")
        try:
            request = PublicRecommendationRequestV2(
                RequestIdentity("restart", view.session_id, view.game_id,
                                view.state_version), view, facts)
            returned = client.submit(request)
            assert returned
            deadline = time.monotonic() + 120.0
            while not received and time.monotonic() < deadline:
                time.sleep(0.05)
            assert received and received[-1].request_id == returned
            return received[-1]
        finally:
            client.close()

    first = run_one()
    second = run_one()
    assert first.request_id != second.request_id
    assert second.status == "ready"


@_real_checkpoint
def test_real_subprocess_missing_pass_renumbering_rejected(tmp_path: Path):
    """Item 3 (2026-11-21): a history with a deleted mid-trick PASS, renumbered
    to keep the indices contiguous, must be rejected at the real worker
    subprocess entry instead of being silently recommended on."""
    bundle = build_bundle(CHECKPOINT_PATH, tmp_path / "bundle")
    samples = json.loads((PHASE1 / "samples_32.json").read_text(encoding="utf-8"))
    sample = next(row for row in samples["samples"]
                  if row["sample_id"] == "danks-p1-wild-03")
    raw = dict(sample["observation"])
    raw["public_history"] = [dict(a) for a in raw["public_history"]]
    current = [a for a in raw["public_history"] if a["trick"] == raw["trick"]]
    victim = next(a for a in current[1:] if a["kind"] == "pass")
    raw["public_history"] = [a for a in raw["public_history"]
                             if a["index"] != victim["index"]]
    for new_index, action in enumerate(raw["public_history"], start=1):
        action["index"] = new_index
    view = CanonicalPublicGameView.from_dict(raw)
    declaration = PublicDeclaration.from_dict(
        sample["current_winning_declaration_fact"]["declaration"])
    facts = DeclaredFacts(schema="Guandan.DeclaredFacts.v2",
        current_winning_declaration=CurrentWinningDeclarationFact(
            status="known", declaration=declaration),
        history_origin="synthetic_normal_entry",
        public_history_complete="known", history_complete_value=True,
        transport_contiguous="known", transport_value=True)
    identity = RequestIdentity("forged-pass-req", view.session_id, view.game_id,
                               view.state_version)
    statuses: list[dict] = []
    results = []
    completed = threading.Event()

    def on_result(result) -> None:
        results.append(result)
        completed.set()

    client = InferenceClient(str(bundle.root), on_result=on_result,
                             on_status=statuses.append, timeout_seconds=30.0,
                             threads=1, requested_device="cpu")
    try:
        returned = client.submit(PublicRecommendationRequestV2(identity, view, facts))
        assert returned
        assert completed.wait(45)
        rejected = results[-1]
        assert rejected.request_id == returned and rejected.status == "unavailable"
        assert rejected.error_code == "MODEL_INPUT_UNSUPPORTED" and not rejected.candidates
        assert rejected.to_dict()["provider"] == "danks_v3"

        completed.clear()
        valid_view, valid_facts = _wild03()
        valid_identity = RequestIdentity("valid-after-rejection", valid_view.session_id,
                                         valid_view.game_id, valid_view.state_version)
        accepted = client.submit(PublicRecommendationRequestV2(valid_identity, valid_view, valid_facts))
        assert completed.wait(45)
        assert results[-1].request_id == accepted and results[-1].status == "ready"
        assert results[-1].candidates
        assert all(status.get("model_status") != "error" for status in statuses)
    finally:
        client.close()
