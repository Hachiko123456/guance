"""Phase 4: real asynchronous provider selection semantics.

The production ``InferenceClient`` returns from ``start()`` before the worker has
finished loading (it reports ``loading`` and later ``ready``). The selection
transaction must treat that as an *in-progress* selection, never as a failure:
a spurious ``model_selection_failed`` latch would block the next launch and
break "restart restores the last explicit selection" for real providers.

These are the fixed counterexamples for that latch. ``AsyncClient`` faithfully
models the real client's asynchronous contract; ``ReadyClient`` models a
synchronous client (already covered elsewhere) to prove the failure path is
still latched.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from guance.application.controller import GuanceController
from guance.infrastructure.model.catalog import ModelCatalog
from guance.presentation.viewmodel import GuanceViewModel
from tests.integration.test_acceptance_runtime import controller_paths
from tests.model.model_bundle_factory import write_bundle


class AsyncClient:
    """Asynchronous load contract: start() returns before ready()."""

    def __init__(self, source, *, on_status, on_result, **kwargs):
        self.source = Path(source)
        self.on_status, self.on_result = on_status, on_result
        self.submitted: list = []
        self.started = self.closed = self.invalidated = 0

    def _metadata(self):
        return {'provider': 'test', 'backend': 'synthetic', 'runtime': 'test',
                'model_version': self.source.name, 'model_sha256': 'a' * 64,
                'capabilities': {'recommend': True}}

    def start(self):
        self.started += 1
        self.on_status({'model_status': 'loading'})  # ready arrives later

    def ready(self):
        self.on_status({'model_status': 'ready', 'metadata': self._metadata()})

    def fail(self):
        self.on_status({'model_status': 'error', 'error_code': 'MODEL_LOAD_FAILED'})

    def submit(self, request, *, timeout_seconds=None):
        self.submitted.append(request)
        return 'request'

    def invalidate(self):
        self.invalidated += 1

    def close(self):
        self.closed += 1


@pytest.fixture
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _build(tmp_path, monkeypatch, catalog_root, catalog_dir):
    monkeypatch.setenv('GUANCE_TRAINING_DATA', '0')
    monkeypatch.delenv('GUANCE_MODEL_PATH', raising=False)
    monkeypatch.delenv('GUANCE_MODEL_BUNDLE', raising=False)
    paths = controller_paths(tmp_path)
    clients: list[AsyncClient] = []

    def factory(source, **kwargs):
        client = AsyncClient(source, **kwargs)
        clients.append(client)
        return client

    catalog = ModelCatalog(catalog_root, paths['models_dir'] / 'active_model.json',
                           root=paths['models_dir'])
    vm = GuanceViewModel()
    controller = GuanceController(vm, paths=paths, model_client_factory=factory, catalog=catalog)
    return controller, vm, clients, paths, catalog


def _preferences(paths) -> dict:
    return json.loads((paths['config_dir'] / 'preferences.json').read_text(encoding='utf-8'))


def test_async_ready_selection_is_not_latched_and_restores_on_restart(tmp_path, monkeypatch, qapp):
    catalog_root = tmp_path / 'models' / 'catalog'
    bundle = write_bundle(catalog_root / 'async-entry', model_id='async-entry')
    controller, vm, clients, paths, catalog = _build(tmp_path, monkeypatch, catalog_root, None)
    try:
        controller.select_catalog_model('async-entry')
        # The load is genuinely in progress (loading), not failed.
        persisted = _preferences(paths)
        assert persisted['model_selection_failed'] is False, persisted
        assert controller._selection_failed is False
        assert controller._model.status.get('model_status') == 'loading'
        clients[-1].ready()
        qapp.processEvents()
        assert vm.modelState == 'ready'
        assert controller.active_source == bundle
        assert _preferences(paths)['model_selection_failed'] is False
    finally:
        controller.close()
        qapp.processEvents()

    # A fresh controller must restore the explicit selection (no block).
    replacement, vm2, clients2, _, _ = _build(tmp_path, monkeypatch, catalog_root, None)
    try:
        assert replacement._selection_failed is False
        assert replacement.requested_source == bundle
        replacement._model.start()
        clients2[-1].ready()
        qapp.processEvents()
        assert replacement.active_source == bundle
        assert vm2.modelState == 'ready'
    finally:
        replacement.close()
        qapp.processEvents()


def test_async_load_failure_is_latched_and_blocks_restart(tmp_path, monkeypatch, qapp):
    catalog_root = tmp_path / 'models' / 'catalog'
    write_bundle(catalog_root / 'async-entry', model_id='async-entry')
    controller, vm, clients, paths, _ = _build(tmp_path, monkeypatch, catalog_root, None)
    try:
        controller.select_catalog_model('async-entry')
        clients[-1].fail()
        qapp.processEvents()
        assert vm.modelState in {'error', 'unavailable'}
        assert controller._selection_failed is True
        assert _preferences(paths)['model_selection_failed'] is True
    finally:
        controller.close()
        qapp.processEvents()

    replacement, vm2, clients2, _, _ = _build(tmp_path, monkeypatch, catalog_root, None)
    try:
        # The durable latch still requires an explicit retry.
        assert replacement._selection_failed is True
        assert clients2 == []
        assert replacement.active_source is None
        replacement.select_catalog_model('async-entry')
        replacement._model.start()
        clients2[-1].ready()
        qapp.processEvents()
        assert replacement.active_source == catalog_root / 'async-entry'
    finally:
        replacement.close()
        qapp.processEvents()


def test_post_ready_inference_error_stays_in_session(tmp_path, monkeypatch, qapp):
    """A mid-session inference error is fail-closed in-session but does not latch."""
    catalog_root = tmp_path / 'models' / 'catalog'
    write_bundle(catalog_root / 'async-entry', model_id='async-entry')
    controller, vm, clients, paths, _ = _build(tmp_path, monkeypatch, catalog_root, None)
    try:
        controller.select_catalog_model('async-entry')
        clients[-1].ready()
        qapp.processEvents()
        assert vm.modelState == 'ready'
        clients[-1].on_status({'model_status': 'error', 'error_code': 'MODEL_INFERENCE_FAILED'})
        qapp.processEvents()
        assert vm.modelState in {'error', 'unavailable'}
        assert controller.active_source is None
        # Not a selection failure: the next launch retries the same selection.
        assert _preferences(paths)['model_selection_failed'] is False
    finally:
        controller.close()
        qapp.processEvents()

    replacement, vm2, clients2, _, _ = _build(tmp_path, monkeypatch, catalog_root, None)
    try:
        assert replacement._selection_failed is False
        assert replacement.requested_source == catalog_root / 'async-entry'
        replacement._model.start()
        clients2[-1].ready()
        qapp.processEvents()
        assert replacement.active_source == catalog_root / 'async-entry'
    finally:
        replacement.close()
        qapp.processEvents()


def test_synchronous_load_failure_still_latches(tmp_path, monkeypatch, qapp):
    """A client that fails synchronously inside start() keeps the fail-closed latch."""
    catalog_root = tmp_path / 'models' / 'catalog'
    write_bundle(catalog_root / 'sync-entry', model_id='sync-entry')
    monkeypatch.setenv('GUANCE_TRAINING_DATA', '0')
    monkeypatch.delenv('GUANCE_MODEL_PATH', raising=False)
    monkeypatch.delenv('GUANCE_MODEL_BUNDLE', raising=False)
    paths = controller_paths(tmp_path)

    class SyncFail(AsyncClient):
        def start(self):
            self.started += 1
            self.fail()

    catalog = ModelCatalog(catalog_root, paths['models_dir'] / 'active_model.json',
                           root=paths['models_dir'])
    controller = GuanceController(GuanceViewModel(), paths=paths, catalog=catalog,
                                  model_client_factory=lambda source, **kw: SyncFail(source, **kw))
    try:
        controller.select_catalog_model('sync-entry')
        assert controller._selection_failed is True
        assert controller.active_source is None
        assert _preferences(paths)['model_selection_failed'] is True
    finally:
        controller.close()
        qapp.processEvents()
