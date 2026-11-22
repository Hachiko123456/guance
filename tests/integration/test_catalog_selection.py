"""P1-A5..A8/A11 plus P0-repair transactions: catalog selection through the controller."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import threading
import tempfile as tempfile_module

import pytest
from PySide6.QtWidgets import QApplication

from guance.application import controller as controller_module
from guance.application.controller import GuanceController
from guance.config import save_preferences
from guance.infrastructure.model.catalog import ModelCatalog
from guance.infrastructure.model.selection import (
    commit_file_for, load_active_selection, save_active_selection,
)
from guance.ports.model import ActiveSelectionDTO
from guance.presentation.viewmodel import GuanceViewModel
from tests.integration.test_acceptance_runtime import controller_paths
from tests.model.model_bundle_factory import write_bundle
from tests.model.windows_junction import make_junction


class RecordingClient:
    def __init__(self, source, *, on_status, on_result, **kwargs):
        self.source = Path(source)
        self.on_status, self.on_result = on_status, on_result
        self.submitted, self.invalidated, self.closed = [], 0, 0

    def ready(self):
        self.on_status({'model_status': 'ready', 'metadata': {
            'provider': 'test', 'backend': 'synthetic', 'runtime': 'test',
            'model_version': self.source.name, 'model_sha256': 'a' * 64,
            'capabilities': {'recommend': True}}})

    def start(self):
        self.ready()

    def submit(self, request, *, timeout_seconds=None):
        self.submitted.append(request.view)
        return 'request'

    def invalidate(self):
        self.invalidated += 1

    def close(self):
        self.closed += 1


class FailingClient(RecordingClient):
    def start(self):
        self.on_status({'model_status': 'error', 'error_code': 'MODEL_LOAD_FAILED'})


@pytest.fixture
def app():
    application = QApplication.instance() or QApplication([])
    yield application
    application.processEvents()


def build(tmp_path, monkeypatch, *, catalog_root=None, client_class=RecordingClient,
          preferences=None, env=None, initial_packaged_model=None):
    monkeypatch.setenv('GUANCE_TRAINING_DATA', '0')
    monkeypatch.delenv('GUANCE_MODEL_PATH', raising=False)
    monkeypatch.delenv('GUANCE_MODEL_BUNDLE', raising=False)
    if env:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
    paths = controller_paths(tmp_path)
    if catalog_root is None:
        catalog_root = paths["models_dir"] / "catalog"
    if preferences is not None:
        from guance.config import save_preferences
        save_preferences(paths, preferences)
    clients = []
    def factory(source, **kwargs):
        client = client_class(source, **kwargs)
        clients.append(client)
        return client
    catalog = ModelCatalog(catalog_root, paths["models_dir"] / "active_model.json",
                           root=paths["models_dir"])
    vm = GuanceViewModel()
    controller = GuanceController(
        vm, paths=paths, model_client_factory=factory, catalog=catalog,
        initial_packaged_model=initial_packaged_model,
    )
    return controller, vm, clients, paths, catalog


def test_catalog_selection_persists_and_new_controller_rereads(tmp_path, monkeypatch, app):
    catalog_root = tmp_path / "models" / "catalog-root"
    write_bundle(catalog_root / "fabledan-default", model_id="fabledan-default")
    controller, vm, clients, paths, catalog = build(tmp_path, monkeypatch, catalog_root=catalog_root)
    try:
        assert vm.activeModelId == ""
        controller.select_catalog_model("fabledan-default")
        controller._model.start()
        clients[-1].ready()
        app.processEvents()
        assert clients[-1].source == catalog_root / "fabledan-default"
        assert vm.activeModelId == "fabledan-default"
        assert "fabledan-default" in vm.activeModelSummary
        document = load_active_selection(catalog.active_file)
        assert document is not None and document.kind == "catalog"
        assert document.model_id == "fabledan-default" and document.bundle == "fabledan-default"
        assert controller.active_source == catalog_root / "fabledan-default"
    finally:
        controller.close()
        app.processEvents()

    replacement, vm2, _, _, _ = build(tmp_path, monkeypatch, catalog_root=catalog_root)
    try:
        assert replacement.requested_source == catalog_root / "fabledan-default"
        assert replacement._active_catalog_id == "fabledan-default"
        assert vm2.activeModelId == "fabledan-default"
    finally:
        replacement.close()
        app.processEvents()


def test_missing_active_is_explicit_unavailable_without_auto_selection(tmp_path, monkeypatch, app):
    catalog_root = tmp_path / "models" / "catalog-root"
    write_bundle(catalog_root / "discovered-only", model_id="discovered-only")
    controller, vm, clients, _, _ = build(tmp_path, monkeypatch, catalog_root=catalog_root)
    try:
        assert controller._no_selection is True
        assert controller._selection_failed is False
        assert controller.requested_source == ""
        assert clients == []
        assert vm.catalogEntries and vm.catalogEntries[0]["model_id"] == "discovered-only"
        controller._model.start()
        app.processEvents()
        assert vm.modelState == "unavailable"
        assert "尚未选择模型" in vm.modelSummary
        assert vm.phase != "error"
    finally:
        controller.close()
        app.processEvents()


def test_capture_package_uses_its_initial_model_on_an_empty_profile(tmp_path, monkeypatch, app):
    bundled = tmp_path / "package" / "danks-v3-top10-v1"
    controller, vm, clients, _, catalog = build(
        tmp_path, monkeypatch, initial_packaged_model=bundled)
    try:
        assert not catalog.active_file.exists()
        assert controller.requested_source == bundled
        assert clients[0].source == bundled
        assert vm.debugCaptureEnabled is False
        controller._model.start()
        app.processEvents()
        assert vm.modelState == "ready"
        assert not catalog.active_file.exists()
    finally:
        controller.close()
        app.processEvents()


def test_legacy_preference_is_migrated_explicitly(tmp_path, monkeypatch, app):
    legacy = str(tmp_path / "legacy-best.npz")
    controller, vm, clients, paths, catalog = build(
        tmp_path, monkeypatch, preferences={'model_path': legacy},
        initial_packaged_model=tmp_path / "package" / "danks-v3-top10-v1")
    try:
        document = load_active_selection(catalog.active_file)
        assert document is not None and document.kind == "legacy" and document.path == legacy
        assert controller.requested_source == Path(legacy)
    finally:
        controller.close()
        app.processEvents()


def test_environment_override_wins_and_is_not_persisted(tmp_path, monkeypatch, app):
    legacy = str(tmp_path / "legacy-best.npz")
    override = str(tmp_path / "env-model.npz")
    controller, vm, clients, paths, catalog = build(
        tmp_path, monkeypatch, preferences={'model_path': legacy},
        env={'GUANCE_MODEL_PATH': override})
    try:
        assert controller.requested_source == Path(override)
        assert load_active_selection(catalog.active_file) is None
    finally:
        controller.close()
        app.processEvents()


def test_tampered_active_document_fails_closed_without_fallback(tmp_path, monkeypatch, app):
    catalog_root = tmp_path / "models" / "catalog-root"
    write_bundle(catalog_root / "valid-bundle", model_id="valid-bundle")
    legacy = str(tmp_path / "legacy-best.npz")
    paths = controller_paths(tmp_path)
    active = paths["models_dir"] / "active_model.json"
    active.write_text('{"schema_version":1,"kind":"catalog","model_id":"valid-bundle"}',
                      encoding="utf-8")
    controller, vm, clients, _, _ = build(
        tmp_path, monkeypatch, catalog_root=catalog_root, preferences={'model_path': legacy},
        initial_packaged_model=tmp_path / "package" / "danks-v3-top10-v1")
    try:
        assert controller._selection_failed is True
        assert controller._selection_failure_code == "ACTIVE_SELECTION_INVALID"
        assert controller._no_selection is False
        assert clients == []
        assert controller.active_source is None
        app.processEvents()
        assert vm.modelState == "error"
        assert vm.modelState != "ready"
        assert vm.adviceCandidates == []
    finally:
        controller.close()
        app.processEvents()


def test_active_selection_pointing_to_missing_bundle_fails_closed(tmp_path, monkeypatch, app):
    catalog_root = tmp_path / "models" / "catalog-root"
    write_bundle(catalog_root / "valid-bundle", model_id="valid-bundle")
    legacy = str(tmp_path / "legacy-best.npz")
    paths = controller_paths(tmp_path)
    active = paths["models_dir"] / "active_model.json"
    active.write_text(json.dumps({"schema_version": 1, "kind": "catalog",
                                  "model_id": "valid-bundle", "bundle": "gone-bundle",
                                  "selected_at": ""}), encoding="utf-8")
    controller, vm, clients, _, _ = build(
        tmp_path, monkeypatch, catalog_root=catalog_root, preferences={'model_path': legacy})
    try:
        assert controller._selection_failed is True
        assert controller._selection_failure_code == "ACTIVE_SELECTION_INVALID"
        assert clients == []
        assert controller.active_source is None
        app.processEvents()
        assert vm.modelState == "error"
    finally:
        controller.close()
        app.processEvents()


def test_catalog_selection_of_broken_bundle_never_falls_back(tmp_path, monkeypatch, app):
    catalog_root = tmp_path / "models" / "catalog-root"
    write_bundle(catalog_root / "broken", model_id="broken")
    write_bundle(catalog_root / "healthy", model_id="healthy")
    controller, vm, clients, paths, catalog = build(
        tmp_path, monkeypatch, catalog_root=catalog_root, client_class=FailingClient)
    try:
        controller.select_catalog_model("broken")
        controller._model.start()
        app.processEvents()
        assert clients and clients[-1].source == catalog_root / "broken"
        assert all(client.source.name != "healthy" for client in clients)
        assert vm.modelState in {"error", "unavailable"}
        assert controller.active_source is None
        persisted = json.loads((paths["config_dir"] / "preferences.json").read_text(encoding="utf-8"))
        assert persisted["model_selection_failed"] is True
        assert str(catalog_root / "broken") in persisted["requested_model_path"]
    finally:
        controller.close()
        app.processEvents()


def test_unknown_catalog_model_revokes_and_reports(tmp_path, monkeypatch, app):
    catalog_root = tmp_path / "models" / "catalog-root"
    write_bundle(catalog_root / "known", model_id="known")
    controller, vm, clients, paths, catalog = build(tmp_path, monkeypatch, catalog_root=catalog_root)
    try:
        controller.select_catalog_model("known")
        controller._model.start()
        app.processEvents()
        assert len(clients) == 1
        controller.select_catalog_model("missing-model")
        app.processEvents()
        assert len(clients) == 1
        assert clients[0].closed == 1
        assert vm.modelState in {"error", "unavailable"}
        assert controller.active_source is None
        persisted = json.loads((paths["config_dir"] / "preferences.json").read_text(encoding="utf-8"))
        assert persisted["model_selection_failed"] is True
        assert persisted["requested_model_path"] == "missing-model"
        assert load_active_selection(catalog.active_file) is not None
    finally:
        controller.close()
        app.processEvents()


def test_catalog_problems_surface_in_viewmodel_and_do_not_hide_valid(tmp_path, monkeypatch, app):
    catalog_root = tmp_path / "models" / "catalog-root"
    write_bundle(catalog_root / "good", model_id="good")
    bad = write_bundle(catalog_root / "bad", model_id="bad")
    (bad / "artifact.npz").write_bytes(b"t" * len(b"synthetic-artifact"))
    controller, vm, clients, _, _ = build(tmp_path, monkeypatch, catalog_root=catalog_root)
    try:
        assert [entry["model_id"] for entry in vm.catalogEntries] == ["good"]
        assert any(problem["code"] == "ARTIFACT_HASH_MISMATCH" for problem in vm.catalogProblems)
    finally:
        controller.close()
        app.processEvents()


def test_direct_import_records_legacy_active_document(tmp_path, monkeypatch, app):
    npz = tmp_path / "direct.npz"
    npz.write_bytes(b"synthetic")
    controller, vm, clients, paths, catalog = build(tmp_path, monkeypatch)
    try:
        controller.import_model(str(npz))
        document = load_active_selection(catalog.active_file)
        assert document is not None and document.kind == "legacy"
        assert document.path == str(npz.resolve())
        controller._model.start()
        app.processEvents()
        assert vm.activeModelId == ""
        assert "direct.npz" in vm.activeModelSummary
    finally:
        controller.close()
        app.processEvents()


def test_catalog_selection_superseded_by_direct_import_updates_document(tmp_path, monkeypatch, app):
    catalog_root = tmp_path / "models" / "catalog-root"
    write_bundle(catalog_root / "catalog-entry", model_id="catalog-entry")
    npz = tmp_path / "direct.npz"
    npz.write_bytes(b"synthetic")
    controller, vm, clients, paths, catalog = build(tmp_path, monkeypatch, catalog_root=catalog_root)
    try:
        controller.select_catalog_model("catalog-entry")
        controller.import_model(str(npz))
        document = load_active_selection(catalog.active_file)
        assert document.kind == "legacy" and document.path == str(npz.resolve())
        controller.import_model('')
        assert load_active_selection(catalog.active_file).kind == "legacy"
    finally:
        controller.close()
        app.processEvents()


# --- P0 repair: durable transaction semantics ---------------------------------


def _preferences_fault(monkeypatch, mode: str, paths: dict):
    """Inject a real preferences-mirror failure; returns a release() callback."""
    config_dir = paths["config_dir"]
    preferences_file = config_dir / "preferences.json"
    if mode == "mkdir":
        original = Path.mkdir
        def mkdir(self, *args, **kwargs):
            if Path(self) == config_dir:
                raise OSError(28, "synthetic mkdir failure")
            return original(self, *args, **kwargs)
        monkeypatch.setattr(Path, "mkdir", mkdir)
        return lambda: monkeypatch.setattr(Path, "mkdir", original)
    if mode == "replace":
        original = Path.replace
        def replace(self, target):
            if Path(target) == preferences_file:
                raise OSError(28, "synthetic replace failure")
            return original(self, target)
        monkeypatch.setattr(Path, "replace", replace)
        return lambda: monkeypatch.setattr(Path, "replace", original)
    original_temp = tempfile_module.NamedTemporaryFile

    class BrokenStream:
        name = str(config_dir / ".preferences-broken.tmp")

        def write(self, text):
            raise OSError(28, "synthetic disk full" if mode == "disk_full"
                          else "synthetic write failure")

        def flush(self):
            raise OSError(28, "synthetic flush failure")

    class ConditionalTemp:
        def __init__(self, *args, **kwargs):
            self._broken = Path(str(kwargs.get("dir", ""))) == config_dir
            self._stream = None if self._broken else original_temp(*args, **kwargs)

        def __enter__(self):
            return BrokenStream() if self._broken else self._stream.__enter__()

        def __exit__(self, *exc):
            return False if self._broken else self._stream.__exit__(*exc)

    monkeypatch.setattr(tempfile_module, "NamedTemporaryFile", ConditionalTemp)
    return lambda: monkeypatch.setattr(tempfile_module, "NamedTemporaryFile", original_temp)


@pytest.mark.parametrize("fault", ["mkdir", "write", "replace", "disk_full"])
def test_preferences_failure_rolls_back_active_and_restart_does_not_activate(
        tmp_path, monkeypatch, app, fault):
    catalog_root = tmp_path / "models" / "catalog-root"
    write_bundle(catalog_root / "first", model_id="first")
    write_bundle(catalog_root / "second", model_id="second")
    controller, vm, clients, paths, catalog = build(tmp_path, monkeypatch, catalog_root=catalog_root)
    try:
        controller.select_catalog_model("first")
        controller._model.start()
        clients[-1].ready()
        app.processEvents()
        assert controller.active_source == catalog_root / "first"
        doc_before = catalog.active_file.read_bytes()
        marker_before = commit_file_for(catalog.active_file).read_bytes()

        staged: dict = {}
        original_stage = catalog.stage_active
        def recording_stage(selection):
            snapshot = original_stage(selection)
            staged["model_id"] = selection.model_id
            return snapshot
        monkeypatch.setattr(catalog, "stage_active", recording_stage)

        release = _preferences_fault(monkeypatch, fault, paths)
        controller.select_catalog_model("second")
        app.processEvents()
        assert staged.get("model_id") == "second", "active stage must succeed before the mirror fails"
        assert controller.active_source is None
        assert vm.modelState in {"error", "unavailable"}
        assert all(client.source.name != "second" for client in clients)
        assert catalog.active_file.read_bytes() == doc_before
        assert commit_file_for(catalog.active_file).read_bytes() == marker_before
        assert load_active_selection(catalog.active_file).model_id == "first"
        persisted = json.loads((paths["config_dir"] / "preferences.json").read_text(encoding="utf-8"))
        assert persisted["model_path"].endswith("first")
        release()
    finally:
        controller.close()
        app.processEvents()

    replacement, vm2, clients2, _, _ = build(tmp_path, monkeypatch, catalog_root=catalog_root)
    try:
        replacement._model.start()
        app.processEvents()
        assert replacement.requested_source == catalog_root / "first"
        assert replacement._active_catalog_id == "first"
        assert all(client.source.name != "second" for client in clients2)
        assert clients2 and clients2[-1].source == catalog_root / "first"
        assert vm2.modelState == "ready"
    finally:
        replacement.close()
        app.processEvents()


def test_staged_document_without_commit_is_not_activated_on_restart(tmp_path, monkeypatch, app):
    catalog_root = tmp_path / "models" / "catalog-root"
    write_bundle(catalog_root / "first", model_id="first")
    write_bundle(catalog_root / "second", model_id="second")
    controller, vm, clients, paths, catalog = build(tmp_path, monkeypatch, catalog_root=catalog_root)
    try:
        controller.select_catalog_model("first")
        controller._model.start()
        clients[-1].ready()
        app.processEvents()
        assert controller.active_source == catalog_root / "first"
    finally:
        controller.close()
        app.processEvents()

    staged_catalog = ModelCatalog(catalog_root, paths["models_dir"] / "active_model.json")
    staged_catalog.stage_active(ActiveSelectionDTO(kind="catalog", model_id="second",
                                                   bundle="second", selected_at=""))
    with pytest.raises(Exception) as exc:
        load_active_selection(paths["models_dir"] / "active_model.json")
    assert getattr(exc.value, "code", "") == "ACTIVE_SELECTION_INVALID"

    replacement, vm2, clients2, _, _ = build(tmp_path, monkeypatch, catalog_root=catalog_root)
    try:
        assert replacement._selection_failed is True
        assert replacement._selection_failure_code == "ACTIVE_SELECTION_INVALID"
        assert clients2 == []
        assert replacement.active_source is None
        app.processEvents()
        assert vm2.modelState == "error"
    finally:
        replacement.close()
        app.processEvents()


def test_commit_marker_failure_restores_previous_selection(tmp_path, monkeypatch, app):
    catalog_root = tmp_path / "models" / "catalog-root"
    write_bundle(catalog_root / "first", model_id="first")
    write_bundle(catalog_root / "second", model_id="second")
    controller, vm, clients, paths, catalog = build(tmp_path, monkeypatch, catalog_root=catalog_root)
    try:
        controller.select_catalog_model("first")
        controller._model.start()
        clients[-1].ready()
        app.processEvents()
        doc_before = catalog.active_file.read_bytes()
        marker_before = commit_file_for(catalog.active_file).read_bytes()

        def failing_commit():
            raise OSError(28, "synthetic marker commit failure")
        monkeypatch.setattr(catalog, "commit_active", failing_commit)
        controller.select_catalog_model("second")
        app.processEvents()
        assert controller.active_source is None
        assert all(client.source.name != "second" for client in clients)
        assert catalog.active_file.read_bytes() == doc_before
        assert commit_file_for(catalog.active_file).read_bytes() == marker_before
        assert load_active_selection(catalog.active_file).model_id == "first"
    finally:
        controller.close()
        app.processEvents()

    replacement, vm2, clients2, _, _ = build(tmp_path, monkeypatch, catalog_root=catalog_root)
    try:
        # The failed attempt is latched (explicit retry only); neither the failed
        # nor the previously committed selection is silently activated.
        assert replacement._selection_failed is True
        assert clients2 == []
        assert replacement.active_source is None
        replacement.select_catalog_model("first")
        replacement._model.start()
        clients2[-1].ready()
        app.processEvents()
        assert replacement.active_source == catalog_root / "first"
        assert vm2.modelState == "ready"
    finally:
        replacement.close()
        app.processEvents()


def test_commit_and_restore_double_failure_stays_fail_closed(tmp_path, monkeypatch, app):
    catalog_root = tmp_path / "models" / "catalog-root"
    write_bundle(catalog_root / "first", model_id="first")
    write_bundle(catalog_root / "second", model_id="second")
    controller, vm, clients, paths, catalog = build(tmp_path, monkeypatch, catalog_root=catalog_root)
    try:
        controller.select_catalog_model("first")
        controller._model.start()
        clients[-1].ready()
        app.processEvents()

        def failing_commit():
            raise OSError(28, "synthetic marker commit failure")
        monkeypatch.setattr(catalog, "commit_active", failing_commit)
        monkeypatch.setattr(catalog, "restore_active", lambda snapshot: False)
        controller.select_catalog_model("second")
        app.processEvents()
        assert controller.active_source is None
        assert all(client.source.name != "second" for client in clients)
        with pytest.raises(Exception) as exc:
            load_active_selection(catalog.active_file)
        assert getattr(exc.value, "code", "") == "ACTIVE_SELECTION_INVALID"
    finally:
        controller.close()
        app.processEvents()

    replacement, vm2, clients2, _, _ = build(tmp_path, monkeypatch, catalog_root=catalog_root)
    try:
        replacement._model.start()
        app.processEvents()
        assert replacement._selection_failed is True
        assert clients2 == []
        assert replacement.active_source is None
        assert vm2.modelState in {"error", "unavailable"}
        assert all(client.source.name != "second" for client in clients2)
    finally:
        replacement.close()
        app.processEvents()


def test_superseded_writer_keeps_consistent_committed_selection(tmp_path, monkeypatch, app):
    catalog_root = tmp_path / "models" / "catalog-root"
    for model in ("first", "second", "third"):
        write_bundle(catalog_root / model, model_id=model)
    controller, vm, clients, paths, catalog = build(tmp_path, monkeypatch, catalog_root=catalog_root)
    try:
        controller.select_catalog_model("first")
        controller._model.start()
        clients[-1].ready()
        app.processEvents()
        assert controller.active_source == catalog_root / "first"

        entered, release = threading.Event(), threading.Event()
        original = controller_module.save_preferences
        def blocking(paths_arg, preferences):
            if preferences["model_path"].endswith("second"):
                entered.set()
                assert release.wait(timeout=5)
            return original(paths_arg, preferences)
        monkeypatch.setattr(controller_module, "save_preferences", blocking)
        worker_b = threading.Thread(target=controller.select_catalog_model, args=("second",))
        worker_b.start()
        assert entered.wait(timeout=3)
        worker_c = threading.Thread(target=controller.select_catalog_model, args=("third",))
        worker_c.start()
        release.set()
        worker_b.join(timeout=5)
        worker_c.join(timeout=5)
        assert not worker_b.is_alive() and not worker_c.is_alive()
        app.processEvents()
        document = load_active_selection(catalog.active_file)
        assert document.model_id == "third"
        marker = json.loads(commit_file_for(catalog.active_file).read_text(encoding="utf-8"))
        assert marker["active_sha256"] == hashlib.sha256(catalog.active_file.read_bytes()).hexdigest()
        assert controller.requested_source == catalog_root / "third"
        assert all(client.source.name != "second" for client in clients)
        assert clients[-1].source == catalog_root / "third"
    finally:
        controller.close()
        app.processEvents()


# --- P0 repair: legacy migration must commit before adoption ------------------


def _migration_fault(monkeypatch, mode: str, models_dir: Path, *, active: Path):
    """Real startup migration write failure; returns a release() callback."""
    if mode == "mkdir":
        target_dir = active.parent
        original = Path.mkdir
        def mkdir(self, *args, **kwargs):
            if Path(self) == target_dir:
                raise OSError(28, "synthetic mkdir failure")
            return original(self, *args, **kwargs)
        monkeypatch.setattr(Path, "mkdir", mkdir)
        return lambda: monkeypatch.setattr(Path, "mkdir", original)
    if mode == "replace":
        original = Path.replace
        def replace(self, target):
            if Path(target) == active:
                raise OSError(28, "synthetic replace failure")
            return original(self, target)
        monkeypatch.setattr(Path, "replace", replace)
        return lambda: monkeypatch.setattr(Path, "replace", original)
    original_temp = tempfile_module.NamedTemporaryFile

    class BrokenStream:
        name = str(active.parent / ".active-broken.tmp")

        def write(self, data):
            raise OSError(28, "synthetic disk full" if mode == "disk_full"
                          else "synthetic write failure")

        def flush(self):
            raise OSError(28, "synthetic flush failure")

    class ConditionalTemp:
        def __init__(self, *args, **kwargs):
            self._broken = Path(str(kwargs.get("dir", ""))) == active.parent
            self._stream = None if self._broken else original_temp(*args, **kwargs)

        def __enter__(self):
            return BrokenStream() if self._broken else self._stream.__enter__()

        def __exit__(self, *exc):
            return False if self._broken else self._stream.__exit__(*exc)

    monkeypatch.setattr(tempfile_module, "NamedTemporaryFile", ConditionalTemp)
    return lambda: monkeypatch.setattr(tempfile_module, "NamedTemporaryFile", original_temp)


def _build_migration_controller(tmp_path, active_file: Path):
    legacy = str(tmp_path / "legacy-best.npz")
    paths = controller_paths(tmp_path)
    save_preferences(paths, {'model_path': legacy})
    clients: list = []
    vm = GuanceViewModel()
    catalog = ModelCatalog(paths["models_dir"] / "catalog", active_file,
                           root=paths["models_dir"])
    controller = GuanceController(
        vm, paths=paths, catalog=catalog,
        model_client_factory=lambda source, **kwargs: (
            clients.append(Path(source)) or RecordingClient(source, **kwargs)))
    return controller, vm, clients, paths, catalog, legacy


def _assert_migration_unavailable(controller, vm, clients, active: Path) -> None:
    assert controller._no_selection is True
    assert controller._selection_failed is False
    assert controller.requested_source == ""
    assert clients == []
    assert not active.exists()
    assert not commit_file_for(active).exists()
    assert vm.modelState == "unavailable"
    assert "尚未选择模型" in vm.modelSummary


@pytest.mark.parametrize("fault", ["mkdir", "write", "replace", "disk_full"])
def test_migration_failure_is_explicit_unavailable_and_recovers_on_restart(
        tmp_path, monkeypatch, app, fault):
    monkeypatch.setenv('GUANCE_TRAINING_DATA', '0')
    monkeypatch.delenv('GUANCE_MODEL_PATH', raising=False)
    monkeypatch.delenv('GUANCE_MODEL_BUNDLE', raising=False)
    if fault == "mkdir":
        # A nested active path whose directory only the migration write would
        # create: the mkdir failure is real and scoped to the migration write.
        active = tmp_path / "models" / "pending-decision" / "active_model.json"
    else:
        active = tmp_path / "models" / "active_model.json"
    release = _migration_fault(monkeypatch, fault, tmp_path / "models", active=active)
    controller, vm, clients, paths, catalog, legacy = _build_migration_controller(tmp_path, active)
    try:
        app.processEvents()
        _assert_migration_unavailable(controller, vm, clients, active)
    finally:
        controller.close()
        app.processEvents()

    # Restart while storage is still broken: the same explicit unavailable, no load.
    controller2, vm2, clients2, paths2, catalog2, legacy2 = _build_migration_controller(tmp_path, active)
    try:
        app.processEvents()
        _assert_migration_unavailable(controller2, vm2, clients2, active)
    finally:
        controller2.close()
        app.processEvents()
    release()

    # After storage recovers, the explicit migration commits instead of staying broken.
    controller3, vm3, clients3, paths3, catalog3, legacy3 = _build_migration_controller(tmp_path, active)
    try:
        document = load_active_selection(active)
        assert document is not None and document.kind == "legacy"
        assert document.path == legacy3
        assert controller3.requested_source == Path(legacy3)
        assert controller3._no_selection is False
        assert clients3 == [Path(legacy3)]
        assert controller3.active_source is None
    finally:
        controller3.close()
        app.processEvents()


def test_forged_claims_never_reach_viewmodel(tmp_path, monkeypatch, app):
    catalog_root = tmp_path / "models" / "catalog-root"
    write_bundle(catalog_root / "good", model_id="good")
    write_bundle(catalog_root / "forged-marvel", model_id="forged-marvel",
                 provider="guandan_marvel_v2",
                 manifest_overrides={"classification": "production",
                                     "readiness": {"experimental": False,
                                                   "production_ready": True,
                                                   "strength_claim": True}})
    controller, vm, clients, _, _ = build(tmp_path, monkeypatch, catalog_root=catalog_root)
    try:
        assert [entry["model_id"] for entry in vm.catalogEntries] == ["good"]
        assert any(problem["code"] == "CLAIM_UNSUPPORTED" for problem in vm.catalogProblems)
        assert all(entry["production_ready"] is False for entry in vm.catalogEntries)
        assert all(entry["strength_claim"] is False for entry in vm.catalogEntries)
        assert all("production" != entry["classification"] for entry in vm.catalogEntries)
    finally:
        controller.close()
        app.processEvents()


# --- second repair: duplicate model_id and junction catalog root ---------------


def test_duplicate_model_id_added_after_selection_fails_closed_then_explicit_rerelect(
        tmp_path, monkeypatch, app):
    catalog_root = tmp_path / "models" / "catalog"
    write_bundle(catalog_root / "a", model_id="same")
    controller, vm, clients, paths, catalog = build(tmp_path, monkeypatch, catalog_root=catalog_root)
    try:
        controller.select_catalog_model("same")
        controller._model.start()
        clients[-1].ready()
        app.processEvents()
        assert controller.active_source == catalog_root / "a"
    finally:
        controller.close()
        app.processEvents()

    write_bundle(catalog_root / "b", model_id="same")
    duplicate, vm2, clients2, paths2, catalog2 = build(tmp_path, monkeypatch, catalog_root=catalog_root)
    try:
        assert duplicate._selection_failed is True
        assert duplicate._selection_failure_code == "ACTIVE_SELECTION_INVALID"
        assert clients2 == []
        assert duplicate.active_source is None
        app.processEvents()
        assert vm2.modelState == "error"
        persisted = json.loads((paths2["config_dir"] / "preferences.json").read_text(encoding="utf-8"))
        assert persisted["model_selection_failed"] is True
    finally:
        duplicate.close()
        app.processEvents()

    shutil.rmtree(catalog_root / "b")
    recovered, vm3, clients3, paths3, catalog3 = build(tmp_path, monkeypatch, catalog_root=catalog_root)
    try:
        # Removing the duplicate does not silently reactivate: the durable latch
        # requires an explicit user re-selection of the (now unique) entry.
        assert recovered._selection_failed is True
        assert clients3 == []
        assert recovered.active_source is None
        recovered.select_catalog_model("same")
        recovered._model.start()
        clients3[-1].ready()
        app.processEvents()
        assert recovered.active_source == catalog_root / "a"
        assert vm3.modelState == "ready"
        cleared = json.loads((paths3["config_dir"] / "preferences.json").read_text(encoding="utf-8"))
        assert cleared["model_selection_failed"] is False
    finally:
        recovered.close()
        app.processEvents()


def test_active_document_under_junction_catalog_fails_closed_on_startup(tmp_path, monkeypatch, app):
    models = tmp_path / "models"
    real_catalog = models / "catalog-real"
    write_bundle(real_catalog / "entry", model_id="entry")
    make_junction(models / "catalog", real_catalog)
    active = models / "active_model.json"
    save_active_selection(active, ActiveSelectionDTO(kind="catalog", model_id="entry",
                                                     bundle="entry", selected_at=""))
    controller, vm, clients, paths, catalog = build(tmp_path, monkeypatch,
                                                    catalog_root=models / "catalog")
    try:
        assert controller._selection_failed is True
        assert controller._selection_failure_code == "ACTIVE_SELECTION_INVALID"
        assert clients == []
        assert controller.active_source is None
        app.processEvents()
        assert vm.modelState == "error"
        assert any(problem["code"] == "ARTIFACT_PATH_INVALID" for problem in vm.catalogProblems)
        persisted = json.loads((paths["config_dir"] / "preferences.json").read_text(encoding="utf-8"))
        assert persisted["model_selection_failed"] is True
    finally:
        controller.close()
        app.processEvents()
