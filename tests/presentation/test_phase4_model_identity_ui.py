"""Phase 4: model identity surfaced to the UI for provider switching (goal 7).

The UI must expose the full bounded model identity so a user can tell which
provider/artifact is active after a switch: provider, model id/version/iteration,
artifact/bundle/deployment fingerprint, runtime contract/version, classification,
experimental/production_ready/strength_claim, capabilities, score_kind and
state/error. This module is the fixed counterexample for the missing
``iteration`` and ``runtime_contract`` identity fields.
"""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from guance.presentation.viewmodel import GuanceViewModel


@pytest.fixture
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


# Real-shaped MARVEL V2 metadata plus the two identity fields the UI must expose.
MARVEL_METADATA = {
    "status": "ready", "provider": "guandan_marvel_v2", "backend": "guandan",
    "runtime": "torch-cpu", "runtime_version": "2.13.0+cpu",
    "runtime_contract": "marvel_v2.cpu.v1",
    "model_version": "MARVEL-V2-v1.3-iter002000", "iteration": 2000,
    "model_sha256": "af0df1eaeb67c88e467ee78bf6b0494c49d000f5733d655ee81b40351d4ca9c8",
    "bundle_fingerprint": "44c69bd301c9883e5dc1b25268b26037a61c949d79d9d529cef7beca6fdf2f22",
    "deployment_fingerprint": "246775cf" + "0" * 56,
    "score_kind": "marvel_v2_policy_path_score", "classification": "evaluation_only",
    "formal_evaluation_status": "not_run", "champion_status": "not_established",
    "experimental": True, "production_ready": False, "strength_claim": False,
    "public_self_supported": False,
    "capabilities": {"recommend": True, "next_token_policy": True,
                     "complete_action": True, "confirmed_decision": True},
}


def test_marvel_identity_fields_are_projected(qapp: QApplication) -> None:
    vm = GuanceViewModel()
    vm.apply_status({"phase": "ready", "model_state": "ready",
                     "model_metadata": MARVEL_METADATA})
    metadata = vm.modelMetadata
    assert metadata["provider"] == "guandan_marvel_v2"
    assert metadata["model_version"] == "MARVEL-V2-v1.3-iter002000"
    # The two identity fields under test.
    assert metadata["iteration"] == 2000
    assert metadata["runtime_contract"] == "marvel_v2.cpu.v1"
    # The rest of the required identity must stay present.
    assert metadata["bundle_fingerprint"].startswith("44c69bd3")
    assert metadata["deployment_fingerprint"].startswith("246775cf")
    assert metadata["capabilities"]["confirmed_decision"] is True
    assert metadata["score_kind"] == "marvel_v2_policy_path_score"
    assert metadata["classification"] == "evaluation_only"
    assert metadata["experimental"] is True
    assert metadata["production_ready"] is False
    assert metadata["strength_claim"] is False


def test_fabledan_identity_fields_are_projected(qapp: QApplication) -> None:
    vm = GuanceViewModel()
    vm.apply_status({"phase": "ready", "model_state": "ready", "model_metadata": {
        "status": "ready", "provider": "fabledan", "backend": "numpy",
        "runtime": "numpy", "runtime_contract": "fabledan.numpy.v1",
        "model_version": "legacy", "iteration": 0,
        "model_sha256": "b" * 64, "bundle_fingerprint": "b" * 64,
        "score_kind": "fabledan_q", "classification": "legacy",
        "experimental": False, "production_ready": False, "strength_claim": False,
        "capabilities": {"recommend": True, "top_n": True},
    }})
    metadata = vm.modelMetadata
    assert metadata["provider"] == "fabledan"
    assert metadata["runtime_contract"] == "fabledan.numpy.v1"
    assert metadata["iteration"] == 0
    assert metadata["score_kind"] == "fabledan_q"
