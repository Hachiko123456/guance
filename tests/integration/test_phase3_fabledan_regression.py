"""Phase 3 FableDan regression: real public-view recommendation plus isolation.

Positive FableDan behavior must stay on the real worker/client path with the
real best.npz; confirmed-decision requests to the FableDan family must fail
closed with REQUEST_KIND_UNSUPPORTED; a fresh FableDan-only process must never
import or attempt to import Torch/MARVEL/native modules.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from guance.contracts import (  # noqa: E402
    CanonicalPublicGameView, ConfirmedDecisionRecommendationRequest, GameObservation,
    PublicRecommendationRequest, RequestIdentity, validate_advice_for_view,
)
from guance.infrastructure.model.client import InferenceClient  # noqa: E402

BEST_NPZ = ROOT / "data" / "profiles" / "tencent_daguandan" / "models" / "best.npz"
FIXTURE = ROOT / "tests" / "fixtures" / "observations.json"
BUNDLE = (ROOT / "build" / "acceptance" / "guance-model-platform-20261016" / "phase3"
          / "bundles" / "marvel-v2-iter002000-confirmed")

pytestmark = pytest.mark.skipif(not BEST_NPZ.is_file() or not FIXTURE.is_file(),
                                reason="real best.npz/fixture unavailable")


def _observation() -> GameObservation:
    record = json.loads(FIXTURE.read_text(encoding="utf-8"))[0]
    return GameObservation.from_dict(record)


def _wait(predicate, timeout=120.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_real_fabledan_recommendation_through_worker(monkeypatch):
    monkeypatch.delenv("GUANCE_MARVEL_V2_SOURCE", raising=False)
    monkeypatch.delenv("GUANCE_MARVEL_V2_CHECKPOINT", raising=False)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    results, statuses = [], []
    client = InferenceClient(BEST_NPZ, on_result=results.append,
                             on_status=lambda status: statuses.append(status.to_dict()),
                             timeout_seconds=30.0, threads=2)
    try:
        client.start()
        assert _wait(lambda: any(item.get("model_status") in {"ready", "unavailable", "error"}
                                 for item in statuses), 180)
        metadata = statuses[-1].get("metadata") or {}
        assert statuses[-1]["model_status"] == "ready", statuses[-1]
        assert metadata.get("provider") == "fabledan"
        assert metadata.get("score_kind") == "fabledan_q"
        assert metadata.get("wire_input_contract") == "CanonicalPublicGameView.v1"
        view = CanonicalPublicGameView.from_observation(_observation())
        request_id = client.submit(PublicRecommendationRequest(
            RequestIdentity("", view.session_id, view.game_id, view.state_version), view))
        assert isinstance(request_id, str) and request_id
        assert _wait(lambda: bool(results), 120), statuses[-3:]
        result = results[0]
        assert result.provider == "fabledan"
        assert result.score_kind == "fabledan_q"
        assert result.status == "ready"
        validate_advice_for_view(view, result)
    finally:
        client.close()


def test_confirmed_request_to_fabledan_is_rejected(monkeypatch):
    import tests.integration.test_phase3_confirmed_recommendation as acceptance

    monkeypatch.delenv("GUANCE_MARVEL_V2_SOURCE", raising=False)
    monkeypatch.delenv("GUANCE_MARVEL_V2_CHECKPOINT", raising=False)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    payload, _context = acceptance._synthetic_fixtures()[0]
    statuses, results = [], []
    client = InferenceClient(BEST_NPZ, on_result=results.append,
                             on_status=lambda status: statuses.append(status.to_dict()),
                             timeout_seconds=60.0, threads=2)
    try:
        client.start()
        assert _wait(lambda: statuses and statuses[-1].get("model_status") == "ready", 180)
        client.submit(ConfirmedDecisionRecommendationRequest(
            RequestIdentity("", payload["session_id"], payload["game_id"],
                            payload["state_version"]), payload))
        assert _wait(lambda: any(item.get("model_status") == "error" for item in statuses), 120)
        errors = [item for item in statuses if item.get("model_status") == "error"]
        assert errors[-1].get("error_code") == "REQUEST_KIND_UNSUPPORTED", errors[-1]
        assert results == []
    finally:
        client.close()


def test_fresh_fabledan_only_process_does_not_import_heavy_model_runtime():
    script = r'''
import sys, importlib.abc
blocked = {"torch", "guandan_marvel", "nanopokerlib"}
attempts = []
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in blocked:
            attempts.append(fullname)
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, Block())
from guance.infrastructure.model.registry import default_registry
from guance.infrastructure.model.manifest import ModelManifest
from guance.infrastructure.model.providers.fabledan import FableDanProvider
from guance.contracts import ADVICE_CONTRACT, PUBLIC_GAME_CONTRACT, PUBLIC_RULESET
manifest = ModelManifest.from_dict({
    "schema_version": 1, "backend": "numpy", "provider": "fabledan",
    "artifact": "best.npz", "artifact_format": "npz", "runtime": "numpy",
    "input_contract": PUBLIC_GAME_CONTRACT, "output_contract": ADVICE_CONTRACT,
    "model_version": "legacy", "training_version": "legacy-unknown",
    "rules_version": PUBLIC_RULESET, "action_version": "public-action.v1",
    "environment_version": "public-play.v1", "encoding_version": "fabledan.v1",
    "algorithm": "fabledan-q", "architecture": "FableDanNet",
    "artifact_sha256": "0" * 64,
    "capabilities": {"recommend": True},
})
default_registry()
FableDanProvider().validate(manifest)
for name in ("guance.infrastructure.model.marvel_v2.runtime",
             "guance.infrastructure.model.marvel_v2.deployment_bundle",
             "guance.infrastructure.model.marvel_v2.decision_request",
             "guance.infrastructure.model.marvel_v2.recommendation"):
    if name in sys.modules:
        raise AssertionError("heavy module imported: " + name)
if attempts:
    raise AssertionError("blocked import attempted: " + repr(attempts))
print("FABLEDAN_ONLY_OK")
'''
    env = {"PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1",
           "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}
    completed = subprocess.run([sys.executable, "-B", "-c", script], cwd=ROOT, env=env,
                               capture_output=True, text=True, timeout=60, check=False)
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert "FABLEDAN_ONLY_OK" in completed.stdout
