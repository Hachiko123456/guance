"""Phase 4 mandatory real provider switching smoke.

Real FableDan (``best.npz``) and real MARVEL V2 iter2000 / iter100 bundles, driven
through the real ``default_client_factory`` JSONL worker, the Phase 2 frozen
runtime source and the real Guandan checkpoints. This module proves:

* FableDan -> MARVEL iter2000 -> FableDan / MARVEL iter100 -> FableDan switching
  with real recommendations after each switch;
* MARVEL A (iter100) <-> B (iter2000) same-contract switching with the correct
  per-bundle identity (iteration, bundle/deployment fingerprint);
* the controller selection transaction plus restart restore with real
  asynchronous provider loads.

No hidden hands, raw payloads, tensors or private paths are written; temp outputs
stay inside pytest's isolated workspace.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from guance.application.controller import GuanceController
from guance.application.model_service import ModelService
from guance.contracts import GameObservation
from guance.infrastructure.model.catalog import ModelCatalog
from guance.infrastructure.model.client import default_client_factory
from guance.presentation.viewmodel import GuanceViewModel
from tests.model.model_bundle_factory import base_v2_manifest

ROOT = Path(__file__).resolve().parents[2]
BEST_NPZ = ROOT / "data" / "profiles" / "tencent_daguandan" / "models" / "best.npz"
PHASE2 = ROOT / "build" / "acceptance" / "guance-model-platform-20261016" / "phase2"
RUNTIME_ROOT = PHASE2 / "runtime" / "marvel-e5c82e7c-d7acbc8d"
RUNTIME_SOURCE = RUNTIME_ROOT / "source"
ITER100_DEPLOY = PHASE2 / "bundles" / "marvel-v2-iter000100" / "deployment.json"
ITER2000_DEPLOY = PHASE2 / "bundles" / "marvel-v2-iter002000" / "deployment.json"
CHECKPOINTS = Path(r"D:\project\guandan\runs\comparison_delivery\dataset")
ITER100_CP = CHECKPOINTS / "m09_champion_iter000100.pt"
ITER2000_CP = CHECKPOINTS / "m09_candidate_iter002000.pt"

MODEL_VERSION_ITER100 = "MARVEL-V2-v1.3-iter000100"
MODEL_VERSION_ITER2000 = "MARVEL-V2-v1.3-iter002000"
ITER2000_SHA = "af0df1eaeb67c88e467ee78bf6b0494c49d000f5733d655ee81b40351d4ca9c8"

pytestmark = pytest.mark.skipif(
    not BEST_NPZ.is_file() or not (RUNTIME_ROOT / "runtime.json").is_file()
    or not (RUNTIME_SOURCE / "guandan_marvel" / "__init__.py").is_file()
    or not ITER100_CP.is_file() or not ITER2000_CP.is_file()
    or not ITER2000_DEPLOY.is_file() or not ITER100_DEPLOY.is_file(),
    reason="real FableDan/MARVEL switching materials unavailable")


def _wait(predicate, timeout=420.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_fabledan_bundle(root: Path) -> Path:
    directory = root / "fabledan-real"
    directory.mkdir(parents=True, exist_ok=True)
    artifact = directory / "artifact.npz"
    shutil.copyfile(BEST_NPZ, artifact)
    manifest = base_v2_manifest("fabledan-real", "fabledan")
    manifest.update({"artifact": "artifact.npz", "artifact_bytes": artifact.stat().st_size,
                     "artifact_sha256": _sha256_file(artifact)})
    (directory / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    (directory / "SHA256SUMS").write_text(
        f"{manifest['artifact_sha256']}  artifact.npz\n", encoding="utf-8")
    return directory


def _write_marvel_bundle(root: Path, model_id: str, deployment_src: Path,
                         model_version: str) -> Path:
    directory = root / model_id
    directory.mkdir(parents=True, exist_ok=True)
    raw = deployment_src.read_bytes()
    (directory / "deployment.json").write_bytes(raw)
    manifest = base_v2_manifest(model_id, "guandan_marvel_v2")
    manifest.update({
        "artifact": "deployment.json",
        "artifact_format": "guance_marvel_v2_deployment_v1",
        "model_version": model_version, "training_version": "M09-comparison-delivery-20261016",
        "artifact_bytes": len(raw), "artifact_sha256": hashlib.sha256(raw).hexdigest(),
        "capabilities": {"recommend": True, "next_token_policy": True,
                         "complete_action": True, "confirmed_decision": True},
    })
    (directory / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    (directory / "SHA256SUMS").write_text(
        f"{manifest['artifact_sha256']}  deployment.json\n", encoding="utf-8")
    return directory


@pytest.fixture(scope="module")
def real_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("phase4-env")
    models = base / "models"
    catalog_root = models / "catalog"
    catalog_root.mkdir(parents=True)
    paths = {
        "data_dir": base / "data", "config_dir": base / "config", "logs_dir": base / "logs",
        "cert_dir": base / "cert", "exports_dir": base / "exports", "models_dir": models,
        "catalog_dir": catalog_root, "active_model_file": models / "active_model.json",
        "runtime_dir": base / "runtime", "root_dir": base,
    }
    for name in ("data_dir", "config_dir", "logs_dir", "cert_dir", "exports_dir"):
        paths[name].mkdir(parents=True, exist_ok=True)
    return {
        "paths": paths,
        "catalog_root": catalog_root,
        "fabledan": _write_fabledan_bundle(catalog_root),
        "marvel100": _write_marvel_bundle(catalog_root, "marvel-v2-iter000100", ITER100_DEPLOY,
                                          MODEL_VERSION_ITER100),
        "marvel2000": _write_marvel_bundle(catalog_root, "marvel-v2-iter002000", ITER2000_DEPLOY,
                                           MODEL_VERSION_ITER2000),
    }


def _marvel_env(monkeypatch, checkpoint: Path):
    monkeypatch.setenv("GUANCE_MARVEL_V2_SOURCE", str(RUNTIME_SOURCE))
    monkeypatch.setenv("GUANCE_MARVEL_V2_CHECKPOINT", str(checkpoint))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")


def _observation() -> GameObservation:
    record = json.loads((ROOT / "tests" / "fixtures" / "observations.json").read_text(
        encoding="utf-8"))[0]
    return GameObservation.from_dict(record)


class _Sink:
    def __init__(self):
        self.statuses: list[dict] = []
        self.results: list = []

    def on_status(self, status):
        self.statuses.append(status.to_dict())

    def on_result(self, result):
        self.results.append(result)

    def latest_state(self):
        return self.statuses[-1].get("model_status") if self.statuses else None

    def latest_metadata(self):
        for item in reversed(self.statuses):
            if item.get("metadata"):
                return item["metadata"]
        return {}


# ---------------------------------------------------------------------------
# Real service-level switch with real recommendations (mandatory smoke).
# ---------------------------------------------------------------------------

def test_real_bidirectional_switch_with_real_recommendations(real_env, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    from tests.integration.test_phase3_confirmed_recommendation import _synthetic_fixtures

    sink = _Sink()
    service = ModelService(on_result=sink.on_result, on_status=sink.on_status,
                           client_factory=default_client_factory, timeout_seconds=600.0, threads=2)
    try:
        # --- FableDan real load + real recommendation -------------------------
        service.import_model(real_env["fabledan"])
        assert _wait(lambda: sink.latest_state() in {"ready", "unavailable", "error"}), sink.statuses[-3:]
        assert sink.latest_state() == "ready", sink.statuses[-3:]
        fabledan_metadata = sink.latest_metadata()
        assert fabledan_metadata.get("provider") == "fabledan"
        assert fabledan_metadata.get("runtime_contract") == "fabledan.numpy.v1"
        service.submit(_observation())
        assert _wait(lambda: bool(sink.results)), sink.statuses[-3:]
        fabledan_result = sink.results[-1]
        assert fabledan_result.provider == "fabledan"
        assert fabledan_result.score_kind == "fabledan_q"
        assert fabledan_result.status == "ready"

        # --- MARVEL iter2000 real load + real confirmed recommendation --------
        _marvel_env(monkeypatch, ITER2000_CP)
        service.import_model(real_env["marvel2000"])
        assert _wait(lambda: sink.latest_state() in {"ready", "unavailable", "error"}), "marvel load"
        assert sink.latest_state() == "ready", sink.statuses[-3:]
        marvel_metadata = sink.latest_metadata()
        assert marvel_metadata.get("provider") == "guandan_marvel_v2"
        assert marvel_metadata.get("iteration") == 2000
        assert marvel_metadata.get("model_version") == MODEL_VERSION_ITER2000
        assert marvel_metadata.get("runtime_contract") == "marvel_v2.cpu.v1"
        assert marvel_metadata.get("bundle_fingerprint") == \
            hashlib.sha256(ITER2000_DEPLOY.read_bytes()).hexdigest()
        payload, _context = _synthetic_fixtures()[0]
        assert payload["model"]["artifact_id"] == MODEL_VERSION_ITER2000
        before = len(sink.results)
        service.submit_confirmed(payload, timeout_seconds=600.0)
        assert _wait(lambda: len(sink.results) > before), "marvel recommendation"
        marvel_result = sink.results[-1]
        assert marvel_result.provider == "guandan_marvel_v2"
        assert marvel_result.score_kind == "marvel_v2_policy_path_score"
        assert marvel_result.model_fingerprint == ITER2000_SHA
        assert marvel_result.status == "ready"
        assert marvel_result.candidates and float(marvel_result.candidates[0].score) <= 0.0

        # --- MARVEL A (iter100) same-contract switch --------------------------
        _marvel_env(monkeypatch, ITER100_CP)
        service.import_model(real_env["marvel100"])
        assert _wait(lambda: sink.latest_state() in {"ready", "unavailable", "error"}), "iter100 load"
        assert sink.latest_state() == "ready", sink.statuses[-3:]
        iter100_metadata = sink.latest_metadata()
        assert iter100_metadata.get("provider") == "guandan_marvel_v2"
        assert iter100_metadata.get("iteration") == 100
        assert iter100_metadata.get("model_version") == MODEL_VERSION_ITER100
        # Identity must differ from iter2000 (no reuse of the previous bundle).
        assert iter100_metadata.get("bundle_fingerprint") != \
            hashlib.sha256(ITER2000_DEPLOY.read_bytes()).hexdigest()
        # No new recommendation was produced merely by switching providers.
        assert sink.results[-1].provider == "guandan_marvel_v2"

        # --- back to FableDan: real recommendation, no MARVEL leakage ---------
        monkeypatch.delenv("GUANCE_MARVEL_V2_SOURCE", raising=False)
        monkeypatch.delenv("GUANCE_MARVEL_V2_CHECKPOINT", raising=False)
        service.import_model(real_env["fabledan"])
        assert _wait(lambda: sink.latest_state() in {"ready", "unavailable", "error"}), "fabledan back"
        assert sink.latest_state() == "ready", sink.statuses[-3:]
        assert sink.latest_metadata().get("provider") == "fabledan"
        before = len(sink.results)
        service.submit(_observation())
        assert _wait(lambda: len(sink.results) > before), "fabledan recommendation after switch back"
        assert sink.results[-1].provider == "fabledan"
        assert sink.results[-1].score_kind == "fabledan_q"
    finally:
        service.close()


# ---------------------------------------------------------------------------
# Controller selection transaction + UI identity + restart with real providers.
# ---------------------------------------------------------------------------

def test_real_corrupt_bundle_fails_closed_without_fallback(real_env, tmp_path, monkeypatch):
    """A tampered real MARVEL bundle must fail closed through the real worker."""
    corrupt_dir = tmp_path / "marvel-corrupt"
    shutil.copytree(real_env["marvel2000"], corrupt_dir)
    (corrupt_dir / "deployment.json").write_bytes(
        (corrupt_dir / "deployment.json").read_bytes() + b"x")
    _marvel_env(monkeypatch, ITER2000_CP)
    sink = _Sink()
    service = ModelService(on_result=sink.on_result, on_status=sink.on_status,
                           client_factory=default_client_factory, timeout_seconds=120.0, threads=2)
    try:
        service.import_model(corrupt_dir)
        assert _wait(lambda: sink.latest_state() in {"error", "unavailable", "timeout"}, 120), \
            sink.statuses[-3:]
        assert sink.latest_state() in {"error", "unavailable", "timeout"}
        assert service.active_source is None
        assert sink.results == []
        # Explicit retry with the intact bundle succeeds; no other provider was used.
        service.import_model(real_env["marvel2000"])
        assert _wait(lambda: sink.latest_state() == "ready", 420), sink.statuses[-3:]
        assert sink.latest_state() == "ready"
        assert sink.latest_metadata().get("provider") == "guandan_marvel_v2"
    finally:
        service.close()


def test_real_controller_selection_identity_and_restart(real_env, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("GUANCE_TRAINING_DATA", "0")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.delenv("GUANCE_MODEL_PATH", raising=False)
    monkeypatch.delenv("GUANCE_MODEL_BUNDLE", raising=False)
    monkeypatch.delenv("GUANCE_MARVEL_V2_SOURCE", raising=False)
    monkeypatch.delenv("GUANCE_MARVEL_V2_CHECKPOINT", raising=False)
    paths = real_env["paths"]
    catalog_root = real_env["catalog_root"]

    def build():
        catalog = ModelCatalog(catalog_root, paths["models_dir"] / "active_model.json",
                               root=paths["models_dir"])
        vm = GuanceViewModel()
        controller = GuanceController(vm, paths=paths, model_client_factory=default_client_factory,
                                      catalog=catalog)
        return controller, vm, catalog

    controller, vm, _catalog = build()
    try:
        # FableDan real selection through the unified bundle transaction.
        controller.select_catalog_model("fabledan-real")
        assert _wait(lambda: controller._model.status.get("model_status") in
                     {"ready", "unavailable", "error"}), "fabledan controller load"
        assert controller._model.status.get("model_status") == "ready"
        app.processEvents()
        assert vm.modelState == "ready"
        assert vm.modelMetadata.get("provider") == "fabledan"
        assert vm.modelMetadata.get("runtime_contract") == "fabledan.numpy.v1"
        assert vm.activeModelId == "fabledan-real"
        persisted = json.loads((paths["config_dir"] / "preferences.json").read_text(encoding="utf-8"))
        assert persisted["model_selection_failed"] is False
        assert controller.active_source == catalog_root / "fabledan-real"

        # Switch to real MARVEL iter2000.
        _marvel_env(monkeypatch, ITER2000_CP)
        controller.select_catalog_model("marvel-v2-iter002000")
        assert _wait(lambda: controller._model.status.get("model_status") in
                     {"ready", "unavailable", "error"}), "marvel controller load"
        assert controller._model.status.get("model_status") == "ready"
        app.processEvents()
        assert vm.modelState == "ready"
        assert vm.modelMetadata.get("provider") == "guandan_marvel_v2"
        assert vm.modelMetadata.get("iteration") == 2000
        assert vm.modelMetadata.get("runtime_contract") == "marvel_v2.cpu.v1"
        assert vm.activeModelId == "marvel-v2-iter002000"
        assert controller.active_source == catalog_root / "marvel-v2-iter002000"
    finally:
        controller.close()
        app.processEvents()

    # Restart: a fresh controller must restore the last explicit selection (the
    # real async load must not have persisted a spurious failure latch).
    replacement, vm2, _ = build()
    try:
        assert replacement._selection_failed is False
        assert replacement._active_catalog_id == "marvel-v2-iter002000"
        assert replacement.requested_source == catalog_root / "marvel-v2-iter002000"
        # No auto-load at construction (start=False) and no blocked latch: the
        # explicit selection is intact and ready for the next start.
        assert replacement._model.status.get("model_status") == "uninitialized"
        assert replacement.active_source is None
    finally:
        replacement.close()
        app.processEvents()
