"""D06 provider tests: real-weight advisor, bundle gates, slot mapping.

The real-weight tests skip when the frozen checkpoint is absent; the
manifest/bundle negative tests are self-contained.  The end-to-end
subprocess chain lives in test_danks_v3_subprocess.py.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from guance.contracts import (AdviceCandidate, CanonicalAdviceResult,
                              CanonicalPublicGameView, CurrentWinningDeclarationFact,
                              DeclaredFacts, PublicAction)
from guance.infrastructure.model.danks_v3.provider import (
    DANKS_V3_FACTS_CONTRACT, DanksV3Advisor, DanksV3Provider, build_bundle,
)
from guance.infrastructure.model.errors import ModelBackendError
from guance.infrastructure.model.manifest import ModelBundle, ModelManifest
from guance.public_state_contract import PublicDeclaration

ROOT = Path(__file__).resolve().parents[2]
PHASE1 = ROOT / "tests/fixtures/danks_v3"
CHECKPOINT_PATH = Path(os.environ.get(
    "GUANCE_DANKS_CHECKPOINT", r"E:\Downloads\top10_selector_v3_online.pt"))
SAMPLES = PHASE1 / "samples_32.json"
_real_checkpoint = pytest.mark.skipif(
    not CHECKPOINT_PATH.is_file() or not SAMPLES.is_file(),
    reason="frozen checkpoint or samples not on this host")


def _facts(**overrides) -> DeclaredFacts:
    """Complete v2 provenance facts (default: synthetic_normal_entry + all known)."""
    data = {
        "schema": "Guandan.DeclaredFacts.v2",
        "current_winning_declaration": CurrentWinningDeclarationFact(status="absent"),
        "history_origin": "synthetic_normal_entry",
        "public_history_complete": "known", "history_complete_value": True,
        "transport_contiguous": "known", "transport_value": True,
    }
    data.update(overrides)
    return DeclaredFacts(**data)


def _manifest(**overrides) -> ModelManifest:
    data = {
        "schema_version": 2, "model_id": "danks-v3-top10-selector",
        "backend": "torch", "provider": "danks_v3",
        "artifact": "weights/a.pt", "artifact_format": "pt",
        "artifact_sha256": "0" * 64, "artifact_bytes": 1,
        "input_contract": "CanonicalPublicGameView.v1",
        "output_contract": "CanonicalAdviceResult.v1",
        "runtime_contract": DANKS_V3_FACTS_CONTRACT,
        "model_version": "top10_selector_v12_calibrated_tactics1",
        "training_version": "online-unknown",
        "rules_version": "danks.gd-rules.c618f537.v1",
        "action_version": "danks.action.v1",
        "environment_version": "danks.environment.v1",
        "encoding_version": "top10_selector_v12_calibrated_tactics1",
        "algorithm": "structured-topk-selector",
        "architecture": "EfficientTeamBeliefTop10Selector",
        "runtime": "torch", "classification": "experimental",
        "readiness": {"experimental": True, "production_ready": False,
                      "strength_claim": False},
        "capabilities": {"recommend": True, "top_n": True, "complete_action": False,
                         "next_token_policy": False, "confirmed_decision": False,
                         "beam_search": False, "belief": False, "search": False,
                         "public_plus_self": True},
    }
    data.update(overrides)
    return ModelManifest.from_dict(data)


def test_provider_validate_accepts_the_frozen_manifest():
    DanksV3Provider().validate(_manifest())


@pytest.mark.parametrize("overrides", [
    {"provider": "fabledan"},
    {"backend": "numpy"},
    {"runtime": "onnx"},
    {"input_contract": "ConfirmedDecision.v1"},
    {"artifact_format": "npz"},
    {"rules_version": "gd-rules-rogue"},
    {"encoding_version": "top10_selector_v99"},
    {"runtime_contract": "rogue.contract.v1"},
    {"classification": "production",
     "readiness": {"experimental": False, "production_ready": True,
                   "strength_claim": True}},
])
def test_provider_validate_rejects_out_of_contract_manifests(overrides):
    with pytest.raises(ModelBackendError):
        DanksV3Provider().validate(_manifest(**overrides))


def test_cuda_request_fails_closed(tmp_path: Path):
    from hashlib import sha256

    payload = b"synthetic artifact for device admission"
    manifest = _manifest(artifact_sha256=sha256(payload).hexdigest(), artifact_bytes=len(payload))
    bundle = ModelBundle(manifest, tmp_path)
    bundle.artifact_path.parent.mkdir()
    bundle.artifact_path.write_bytes(payload)
    advisor = DanksV3Advisor(bundle, requested_device="cuda")
    with pytest.raises(ModelBackendError) as excinfo:
        advisor.load()
    assert excinfo.value.code == "MODEL_UNAVAILABLE"
    assert advisor._loaded is None


def test_build_bundle_rejects_foreign_weights(tmp_path: Path):
    foreign = tmp_path / "rogue.pt"
    foreign.write_bytes(b"not the frozen checkpoint")
    with pytest.raises(ModelBackendError) as excinfo:
        build_bundle(foreign, tmp_path / "bundle")
    assert excinfo.value.code == "ARTIFACT_HASH_MISMATCH"


@_real_checkpoint
def test_build_bundle_and_load_real_weights(tmp_path: Path):
    bundle = build_bundle(CHECKPOINT_PATH, tmp_path / "bundle")
    assert bundle.manifest.artifact_sha256 == \
        "91356edfd46ccfd54d03d5ce8bf181d3b31326d5650897a5a3d0c29e1bcedb08"
    advisor = DanksV3Advisor(bundle, requested_device="cpu")
    metadata = advisor.load()
    assert metadata.status == "loaded" and metadata.actual_device == "cpu"
    assert metadata.model_sha256 == bundle.manifest.artifact_sha256


@_real_checkpoint
def test_recommend_follow_state_real_weights(tmp_path: Path):
    """wild-03 (level 13, confirmed declaration): the real weights must rank
    pass/bombs first (the level full house beats all non-bombs), within <=5
    entity-card-mapped candidates that pass the shared validator."""
    samples = json.loads(SAMPLES.read_text(encoding="utf-8"))
    sample = next(row for row in samples["samples"]
                  if row["sample_id"] == "danks-p1-wild-03")
    view = CanonicalPublicGameView.from_dict(sample["observation"])
    declaration = PublicDeclaration.from_dict(
        sample["current_winning_declaration_fact"]["declaration"])
    facts = _facts(current_winning_declaration=CurrentWinningDeclarationFact(
        status="known", declaration=declaration))
    bundle = build_bundle(CHECKPOINT_PATH, tmp_path / "bundle")
    advisor = DanksV3Advisor(bundle, requested_device="cpu")
    advisor.load()
    result = advisor.recommend(view, "test-req", declared_facts=facts)
    assert result.status == "ready" and 1 <= len(result.candidates) <= 5
    assert result.candidates[0].rank == 1
    scores = [c.score for c in result.candidates]
    assert scores == sorted(scores, reverse=True)
    hand = set(view.hand)
    for candidate in result.candidates:
        assert set(candidate.cards) <= hand
        assert candidate.score_kind == "danks_v3_selector_logit"
        assert candidate.label
    kinds = {c.kind for c in result.candidates}
    assert kinds <= {"pass", "bomb", "sflush", "rocket"}


@_real_checkpoint
def test_recommend_free_lead_real_weights(tmp_path: Path):
    samples = json.loads(SAMPLES.read_text(encoding="utf-8"))
    sample = next(row for row in samples["samples"]
                  if row["sample_id"] == "danks-p1-open-00")
    view = CanonicalPublicGameView.from_dict(sample["observation"])
    bundle = build_bundle(CHECKPOINT_PATH, tmp_path / "bundle")
    advisor = DanksV3Advisor(bundle, requested_device="cpu")
    advisor.load()
    result = advisor.recommend(view, "test-req", declared_facts=_facts())
    assert result.status == "ready" and 1 <= len(result.candidates) <= 5
    for candidate in result.candidates:
        assert candidate.kind != "pass"  # free lead never passes
        assert set(candidate.cards) <= set(view.hand)


@_real_checkpoint
def test_recommend_rejects_ambiguous_target_real_weights(tmp_path: Path):
    """The D03 five-card set without a confirmed declaration: three level-valid
    interpretations exist, so the provider must fail closed.  Uses the wild-03
    consistent view with the winning declaration set to unsupported so the
    provenance gate passes and the ambiguity gate fires."""
    samples = json.loads(SAMPLES.read_text(encoding="utf-8"))
    sample = next(row for row in samples["samples"]
                  if row["sample_id"] == "danks-p1-wild-03")
    view = CanonicalPublicGameView.from_dict(sample["observation"])
    facts = _facts(current_winning_declaration=CurrentWinningDeclarationFact(
        status="unsupported", reason="live_declaration_unverified"))
    bundle = build_bundle(CHECKPOINT_PATH, tmp_path / "bundle")
    advisor = DanksV3Advisor(bundle, requested_device="cpu")
    advisor.load()
    with pytest.raises(ModelBackendError) as excinfo:
        advisor.recommend(view, "test-req", declared_facts=facts)
    assert excinfo.value.code == "MODEL_INPUT_AMBIGUOUS"


@_real_checkpoint
def test_recommend_rejects_unverified_history_provenance(tmp_path: Path):
    """D04 gate: a live_capture origin without verified transport is
    ineligible even when the declaration itself resolves; missing facts and
    unknown origins fail closed identically."""
    samples = json.loads(SAMPLES.read_text(encoding="utf-8"))
    sample = next(row for row in samples["samples"]
                  if row["sample_id"] == "danks-p1-wild-03")
    view = CanonicalPublicGameView.from_dict(sample["observation"])
    declaration = PublicDeclaration.from_dict(
        sample["current_winning_declaration_fact"]["declaration"])
    bundle = build_bundle(CHECKPOINT_PATH, tmp_path / "bundle")
    advisor = DanksV3Advisor(bundle, requested_device="cpu")
    advisor.load()
    for origin, transport in (("live_capture", "unsupported"),
                              ("", "known"), ("rogue-origin", "known")):
        facts = DeclaredFacts(schema="Guandan.DeclaredFacts.v2",
            current_winning_declaration=CurrentWinningDeclarationFact(
                status="known", declaration=declaration),
            history_origin=origin, transport_contiguous=transport)
        with pytest.raises(ModelBackendError) as excinfo:
            advisor.recommend(view, "test-req", declared_facts=facts)
        assert excinfo.value.code == "MODEL_INPUT_UNSUPPORTED"
    with pytest.raises(ModelBackendError) as excinfo:
        advisor.recommend(view, "test-req")
    assert excinfo.value.code == "MODEL_INPUT_UNSUPPORTED"


@_real_checkpoint
def test_recommend_rejects_known_false_transport(tmp_path: Path):
    """D04 gate: known(False) = CONFIRMED discontinuous - must be rejected
    even with a trusted origin and a valid declaration."""
    samples = json.loads(SAMPLES.read_text(encoding="utf-8"))
    sample = next(row for row in samples["samples"]
                  if row["sample_id"] == "danks-p1-wild-03")
    view = CanonicalPublicGameView.from_dict(sample["observation"])
    declaration = PublicDeclaration.from_dict(
        sample["current_winning_declaration_fact"]["declaration"])
    bundle = build_bundle(CHECKPOINT_PATH, tmp_path / "bundle")
    advisor = DanksV3Advisor(bundle, requested_device="cpu")
    advisor.load()
    facts = DeclaredFacts(schema="Guandan.DeclaredFacts.v2",
        current_winning_declaration=CurrentWinningDeclarationFact(
            status="known", declaration=declaration),
        history_origin="synthetic_normal_entry",
        public_history_complete="known", history_complete_value=True,
        transport_contiguous="known", transport_value=False)
    with pytest.raises(ModelBackendError) as excinfo:
        advisor.recommend(view, "test-req", declared_facts=facts)
    assert excinfo.value.code == "MODEL_INPUT_UNSUPPORTED"


@_real_checkpoint
def test_recommend_rejects_mid_join_empty_history(tmp_path: Path):
    """A mid-join state (fewer cards than dealt but empty history) claiming
    synthetic_normal_entry must be rejected at the real worker entry."""
    samples = json.loads(SAMPLES.read_text(encoding="utf-8"))
    sample = next(row for row in samples["samples"]
                  if row["sample_id"] == "danks-p1-wild-03")
    raw = dict(sample["observation"])
    raw["public_history"] = []  # mid-join: strip all history
    raw["free_lead"] = True     # make the empty history self-consistent
    raw["lead_action"] = None
    raw["trick"] = 0
    # The observer must have played cards for this to be a mid-join.
    my_seat = raw["my_seat"]
    raw["remaining"] = list(raw["remaining"])
    raw["remaining"][my_seat] = 20  # 7 cards played but no history
    raw["hand"] = raw["hand"][:20]
    raw["remaining"][my_seat] = 20
    view = CanonicalPublicGameView.from_dict(raw)
    declaration = PublicDeclaration.from_dict(
        sample["current_winning_declaration_fact"]["declaration"])
    facts = _facts(current_winning_declaration=CurrentWinningDeclarationFact(
        status="known", declaration=declaration))
    bundle = build_bundle(CHECKPOINT_PATH, tmp_path / "bundle")
    advisor = DanksV3Advisor(bundle, requested_device="cpu")
    advisor.load()
    with pytest.raises(ModelBackendError) as excinfo:
        advisor.recommend(view, "test-req", declared_facts=facts)
    assert excinfo.value.code == "MODEL_INPUT_UNSUPPORTED"


@_real_checkpoint
def test_missing_pass_renumbering_rejected_real_worker(tmp_path: Path):
    """Delete a PASS and renumber the remaining actions: the actor-chain
    gate must reject the forged history at the real worker entry, while the
    full original chain still recommends.  Two deletion positions matter:
    mid-chain (caught by the rotation pair rule) and chain-end (caught by
    the open-trick-must-end-at-the-observer rule)."""
    # Minimal open trick 0: lead S3 by seat 0, PASS by seat 3, PASS by
    # seat 2; the observer (seat 1, the next actor in wire rotation) is
    # asked for a follow.  The observer's 27-card hand excludes the played
    # S3 (0x13); 0x113/0x114/0x118 are the second deck copy of S3/S4/S8.
    hand_codes = (0x14, 0x113, 0x114, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x1B,
                  0x1C, 0x1D, 0x23, 0x24, 0x25, 0x26, 0x27, 0x33,
                  0x34, 0x35, 0x36, 0x37, 0x43, 0x44, 0x45, 0x46,
                  0x47, 0x118)
    remaining = (26, 27, 27, 27)  # seat 0 played the lead
    lead = PublicAction(1, 0, "play", (0x13,), 1, 3, 0)
    pass_3 = PublicAction(2, 3, "pass", (), None, 2, 0)
    pass_2 = PublicAction(3, 2, "pass", (), None, 1, 0)
    view_full = CanonicalPublicGameView(
        "t", "g", 1, 1, 1, 0, 3, 2, 0, hand_codes, remaining,
        (lead, pass_3, pass_2), lead, False)
    # Forged mid-chain: delete seat 3's PASS, renumber 1..2.
    forged_mid = CanonicalPublicGameView(
        "t", "g", 1, 1, 1, 0, 3, 2, 0, hand_codes, remaining,
        (lead, PublicAction(2, 2, "pass", (), None, 1, 0)), lead, False)
    # Forged chain-end: delete seat 2's PASS, renumber 1..2.
    forged_end = CanonicalPublicGameView(
        "t", "g", 1, 1, 1, 0, 3, 2, 0, hand_codes, remaining,
        (lead, PublicAction(2, 3, "pass", (), None, 2, 0)), lead, False)
    declaration = PublicDeclaration("SINGLE", (3,), 3, None, 1, (0x13,),
                                    (), canonical_action_cards=(0x13,))
    declaration.validate_for_level(2)
    facts = _facts(current_winning_declaration=CurrentWinningDeclarationFact(
        status="known", declaration=declaration))
    bundle = build_bundle(CHECKPOINT_PATH, tmp_path / "bundle")
    advisor = DanksV3Advisor(bundle, requested_device="cpu")
    advisor.load()
    for view in (forged_mid, forged_end):
        with pytest.raises(ModelBackendError) as excinfo:
            advisor.recommend(view, "test-req", declared_facts=facts)
        assert excinfo.value.code == "MODEL_INPUT_UNSUPPORTED"
    # The full original chain still passes (produces a recommendation).
    result = advisor.recommend(view_full, "test-ok", declared_facts=facts)
    assert result.status == "ready"


@_real_checkpoint
def test_close_then_recommend_fails_closed(tmp_path: Path):
    bundle = build_bundle(CHECKPOINT_PATH, tmp_path / "bundle")
    advisor = DanksV3Advisor(bundle, requested_device="cpu")
    advisor.load()
    advisor.close()
    samples = json.loads(SAMPLES.read_text(encoding="utf-8"))
    sample = next(row for row in samples["samples"]
                  if row["sample_id"] == "danks-p1-open-00")
    view = CanonicalPublicGameView.from_dict(sample["observation"])
    facts = _facts()
    with pytest.raises(ModelBackendError) as excinfo:
        advisor.recommend(view, "req", declared_facts=facts)
    assert excinfo.value.code == "MODEL_CLOSED"
