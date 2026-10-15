"""Cross-layer model selection faults. Synthetic clients, no network or weights."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from guance.application import controller as controller_module
from guance.application.controller import GuanceController
from guance.application.model_service import ModelService
from guance.contracts import AdviceCandidate, CanonicalAdviceResult, EngineUpdate, GameObservation
from guance.config import save_preferences
from guance.presentation.viewmodel import GuanceViewModel
from tests.integration.test_acceptance_runtime import controller_paths, FakeRuntime


class ControlledClient:
    def __init__(self, source, *, on_status, on_result, **kwargs):
        self.source = Path(source)
        self.on_status, self.on_result = on_status, on_result
        self.submitted = []
        self.started = self.closed = self.invalidated = 0

    def ready(self):
        self.on_status({'model_status': 'ready', 'metadata': {
            'provider': 'test', 'backend': 'synthetic', 'runtime': 'test',
            'model_version': self.source.stem, 'model_sha256': 'a' * 64,
            'capabilities': {'recommend': True}}})

    def start(self):
        self.started += 1
        self.ready()

    def submit(self, view):
        self.submitted.append(view)
        return 'request'

    def reply(self, view):
        self.on_result(CanonicalAdviceResult(*view.key, 'request', provider='test',
            model_version=self.source.stem, model_fingerprint='a' * 64, score_kind='test_score',
            candidates=(AdviceCandidate((0x11,), 'play', 'single', 0.1, score_kind='test_score'),)))

    def invalidate(self):
        self.invalidated += 1

    def close(self):
        self.closed += 1


@pytest.fixture
def harness(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv('GUANCE_TRAINING_DATA', '0')
    monkeypatch.delenv('GUANCE_MODEL_PATH', raising=False)
    monkeypatch.delenv('GUANCE_MODEL_BUNDLE', raising=False)
    paths = controller_paths(tmp_path)
    initial = {'model_path': str(tmp_path / 'A.npz'), 'debug_capture_enabled': False}
    save_preferences(paths, initial)
    clients = []
    def factory(source, **kwargs):
        client = ControlledClient(source, **kwargs)
        clients.append(client)
        return client
    vm = GuanceViewModel()
    controller = GuanceController(vm, paths=paths, model_client_factory=factory)
    controller._runtime = FakeRuntime()
    controller._capture_port_available = lambda: True
    controller._model.start()
    observation = GameObservation('test-session', 'test-game', 7, 123.0,
        2, 0, 0, 0, (0x11, 0x13), (2, 27, 27, 27), free_lead=True)
    vm.apply_status({'phase': 'ready', 'running': True, 'connected': True})
    controller._handle_update(EngineUpdate('observation', observation=observation))
    clients[0].reply(clients[0].submitted[-1])
    assert vm.adviceCandidates
    yield controller, vm, clients, observation, app
    controller.close()
    app.processEvents()


def test_preferences_disk_full_never_resumes_old_model(harness, monkeypatch, tmp_path):
    controller, vm, clients, observation, app = harness
    old = clients[0]
    old_view = old.submitted[-1]
    before = len(old.submitted)
    original = (controller.paths['config_dir'] / 'preferences.json').read_bytes()
    def disk_full(*args, **kwargs):
        raise OSError(28, 'synthetic disk full')
    monkeypatch.setattr(controller_module, 'save_preferences', disk_full)
    controller.import_model(str(tmp_path / 'B.json'))
    controller._handle_update(EngineUpdate('observation', observation=observation))
    old.ready()
    old.reply(old_view)
    app.processEvents()
    assert len(old.submitted) == before, 'old client accepted observation after failed selection'
    assert old.closed == 1
    assert controller._model.status.get('model_status') in {'error', 'unavailable'}
    assert vm.modelState in {'error', 'unavailable'}
    assert not vm.adviceCandidates
    assert (controller.paths['config_dir'] / 'preferences.json').read_bytes() == original


def assert_blocked(controller, vm, old, observation, app, baseline):
    controller._handle_update(EngineUpdate('observation', observation=observation))
    old.ready()
    old.reply(old.submitted[0])
    old.on_status({'model_status': 'error', 'error_code': 'MODEL_LOAD_FAILED'})
    app.processEvents()
    assert len(old.submitted) == baseline
    assert controller.active_source is None
    assert controller._model.status.get('model_status') in {'error', 'unavailable', 'closed'}
    assert vm.modelState != 'ready'
    assert not vm.adviceCandidates


@pytest.mark.parametrize('fault', ['mkdir', 'serialize', 'write', 'replace'])
def test_atomic_preference_faults_preserve_old_file_and_user_temp(harness, monkeypatch, tmp_path, fault):
    from guance import config
    controller, vm, clients, observation, app = harness
    directory = controller.paths['config_dir']
    original = (directory / 'preferences.json').read_bytes()
    unrelated = directory / 'preferences.tmp'
    unrelated.write_text('user-owned temporary content')
    before = len(clients[0].submitted)
    original_mkdir, original_replace = Path.mkdir, Path.replace
    def fail(*args, **kwargs):
        raise OSError(28, 'synthetic persistence fault')
    if fault == 'mkdir':
        def mkdir(p, *args, **kwargs):
            return fail() if p == directory else original_mkdir(p, *args, **kwargs)
        monkeypatch.setattr(Path, 'mkdir', mkdir)
    elif fault == 'serialize':
        monkeypatch.setattr(config.json, 'dumps', fail)
    elif fault == 'replace':
        def replace(p, target):
            return fail() if Path(target) == directory / 'preferences.json' else original_replace(p, target)
        monkeypatch.setattr(Path, 'replace', replace)
    else:
        original_temp = config.tempfile.NamedTemporaryFile
        class BrokenWrite:
            def __init__(self, *args, **kwargs):
                self.stream = original_temp(*args, **kwargs)
                self.name = self.stream.name
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.stream.close()
            def write(self, text):
                self.stream.write(text[:10])
                raise OSError(28, 'synthetic partial write')
        monkeypatch.setattr(config.tempfile, 'NamedTemporaryFile', BrokenWrite)
    controller.import_model(str(tmp_path / 'B.json'))
    assert_blocked(controller, vm, clients[0], observation, app, before)
    assert (directory / 'preferences.json').read_bytes() == original
    assert unrelated.read_text() == 'user-owned temporary content'
    assert not list(directory.glob('.preferences-*.tmp'))
    assert controller.preferences['model_path'].endswith('A.npz')
    assert str(controller.persisted_source).endswith('A.npz')
    assert str(controller.requested_source).endswith('B.json')


@pytest.mark.parametrize('fault', ['source', 'factory', 'manifest', 'start', 'invalidate', 'close'])
def test_selection_fault_matrix_and_explicit_retry(harness, monkeypatch, tmp_path, fault):
    from guance.ports.model import ModelBackendError
    controller, vm, clients, observation, app = harness
    old = clients[0]
    before = len(old.submitted)
    factory = controller._model_client_factory
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise RuntimeError('synthetic sensitive failure text')
        if fault == 'source':
            original = Path.resolve
            def resolve(p, *args, **kwargs):
                return fail() if p.name == 'B.json' else original(p, *args, **kwargs)
            patch.setattr(Path, 'resolve', resolve)
        elif fault in {'invalidate', 'close'}:
            patch.setattr(old, fault, fail)
        else:
            def bad_factory(source, **kwargs):
                if fault == 'factory':
                    return fail()
                client = factory(source, **kwargs)
                def start():
                    if fault == 'manifest':
                        client.on_status({'model_status': 'error', 'error_code': 'ARTIFACT_HASH_MISMATCH'})
                        client.ready()  # Same client cannot undo an earlier load failure.
                    else:
                        raise ModelBackendError('MODEL_LOAD_FAILED')
                client.start = start
                return client
            patch.setattr(controller, '_model_client_factory', bad_factory)
        controller.import_model(str(tmp_path / 'B.json'))
        assert_blocked(controller, vm, old, observation, app, before)
        assert 'synthetic sensitive failure text' not in vm.modelSummary
    controller.import_model(str(tmp_path / 'C.json'))
    assert controller._model.status.get('model_status') == 'ready'
    assert vm.modelState == 'ready'
    assert controller.active_source == (tmp_path / 'C.json').resolve()
    assert str(controller.persisted_source) == str(controller.active_source)
    assert controller.requested_source == controller.active_source
    assert controller.preferences['model_path'] == str(controller.active_source)
    assert vm.modelMetadata['fingerprint'] == 'a' * 64
    controller._handle_update(EngineUpdate('observation', observation=observation))
    clients[-1].reply(clients[-1].submitted[-1])
    assert vm.adviceCandidates


def test_cancel_does_not_revoke_current_model(harness):
    controller, vm, clients, observation, app = harness
    generation = controller.selection_generation
    controller.import_model('')
    assert controller.selection_generation == generation
    assert clients[0].closed == 0
    assert vm.adviceCandidates


def test_failed_selection_latch_survives_stop_start_and_service_rebuild(harness, monkeypatch, tmp_path):
    controller, vm, clients, observation, app = harness
    with monkeypatch.context() as patch:
        patch.setattr(controller_module, 'save_preferences', lambda *a: (_ for _ in ()).throw(OSError(28, 'test')))
        controller.import_model(str(tmp_path / 'B.json'))
    old = clients[0]
    before = len(old.submitted)
    controller._stop_impl()
    controller._start_impl()
    controller._create_model_service()
    controller._model.start()
    assert len(clients) == 1
    assert_blocked(controller, vm, old, observation, app, before)
    controller.import_model(str(tmp_path / 'C.json'))
    assert vm.modelState == 'ready'
    assert len(clients) == 2


def test_normal_stop_start_uses_session_selection_not_environment(harness, monkeypatch, tmp_path):
    controller, vm, clients, observation, app = harness
    controller.import_model(str(tmp_path / 'B.json'))
    controller._stop_impl()
    monkeypatch.setenv('GUANCE_MODEL_PATH', str(tmp_path / 'unexpected-default.npz'))
    controller._start_impl()
    assert clients[-1].source.name == 'B.json'
    assert vm.modelState == 'ready'


def test_queued_old_ui_result_and_status_are_rejected_after_switch(harness, tmp_path):
    import threading
    controller, vm, clients, observation, app = harness
    old = clients[0]
    # Deliver callbacks from a worker so the real VM queues them to Qt's thread.
    worker = threading.Thread(target=lambda: (old.reply(old.submitted[-1]), old.ready()))
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    controller.import_model(str(tmp_path / 'B.json'))
    app.processEvents()
    assert vm.modelMetadata['model_version'] == 'B'
    assert not vm.adviceCandidates
    old.on_status({'model_status': 'error', 'error_code': 'MODEL_LOAD_FAILED'})
    old.ready()
    assert vm.modelState == 'ready'
    assert vm.modelMetadata['model_version'] == 'B'


def test_observation_and_newest_selection_while_preferences_are_blocked(harness, monkeypatch, tmp_path):
    import threading
    controller, vm, clients, observation, app = harness
    entered, release = threading.Event(), threading.Event()
    original = controller_module.save_preferences
    def save(paths, preferences):
        if preferences['model_path'].endswith('B.json'):
            entered.set()
            assert release.wait(timeout=3)
        return original(paths, preferences)
    monkeypatch.setattr(controller_module, 'save_preferences', save)
    worker_b = threading.Thread(target=controller.import_model, args=(str(tmp_path / 'B.json'),))
    worker_b.start()
    assert entered.wait(timeout=2)
    old = clients[0]
    before = len(old.submitted)
    controller._handle_update(EngineUpdate('observation', observation=observation))
    assert len(old.submitted) == before
    # Superseding choice takes ownership immediately even while B owns disk I/O.
    requested = threading.Event()
    original_new = controller._new_model_service
    def new(generation):
        service = original_new(generation)
        requested.set()
        return service
    monkeypatch.setattr(controller, '_new_model_service', new)
    worker_c = threading.Thread(target=controller.import_model, args=(str(tmp_path / 'C.json'),))
    worker_c.start()
    assert requested.wait(timeout=2)
    release.set()
    worker_b.join(timeout=3)
    worker_c.join(timeout=3)
    assert not worker_b.is_alive() and not worker_c.is_alive()
    app.processEvents()
    assert [c.source.stem for c in clients] == ['A', 'C']
    assert vm.modelMetadata['model_version'] == 'C'
    assert controller.active_source == (tmp_path / 'C.json').resolve()
    persisted = json.loads((controller.paths['config_dir'] / 'preferences.json').read_text())
    assert persisted['model_path'] == str(controller.active_source)


def test_close_during_persistence_cannot_resurrect_service(harness, monkeypatch, tmp_path):
    import threading
    controller, vm, clients, observation, app = harness
    entered, release = threading.Event(), threading.Event()
    original = controller_module.save_preferences
    def save(*args):
        entered.set()
        assert release.wait(timeout=3)
        return original(*args)
    monkeypatch.setattr(controller_module, 'save_preferences', save)
    worker = threading.Thread(target=controller.import_model, args=(str(tmp_path / 'B.json'),))
    worker.start()
    assert entered.wait(timeout=2)
    controller.close()
    release.set()
    worker.join(timeout=3)
    assert not worker.is_alive()
    app.processEvents()
    assert controller.active_source is None
    assert len(clients) == 1
    assert controller._model.status.get('model_status') == 'closed'
    controller.import_model(str(tmp_path / 'C.json'))
    assert len(clients) == 1


def test_rapid_a_b_c_late_callbacks_never_restore_prior_identity(harness, tmp_path):
    controller, vm, clients, observation, app = harness
    old_service = controller._model
    controller.import_model(str(tmp_path / 'B.json'))
    controller._handle_update(EngineUpdate('observation', observation=observation))
    b = clients[-1]
    controller.import_model(str(tmp_path / 'C.json'))
    old_service._on_status({'model_status': 'ready'})
    for prior in clients[:-1]:
        prior.ready()
        prior.reply(prior.submitted[-1])
        prior.on_status({'model_status': 'error', 'error_code': 'MODEL_LOAD_FAILED'})
    app.processEvents()
    assert vm.modelState == 'ready'
    assert vm.modelMetadata['model_version'] == 'C'
    assert not vm.adviceCandidates


def test_synchronous_start_result_and_reentrant_close(harness, tmp_path, monkeypatch):
    controller, vm, clients, observation, app = harness
    factory = controller._model_client_factory
    def create(source, **kwargs):
        client = factory(source, **kwargs)
        start = client.start
        def close_while_starting():
            start()
            controller.close()
            client.ready()
        client.start = close_while_starting
        return client
    monkeypatch.setattr(controller, '_model_client_factory', create)
    controller.import_model(str(tmp_path / 'B.json'))
    app.processEvents()
    assert controller.active_source is None
    assert clients[-1].closed == 1
    assert not vm.adviceCandidates


def test_persisted_failed_selection_blocks_new_controller_until_explicit_retry(harness, monkeypatch, tmp_path):
    controller, vm, clients, observation, app = harness
    original_factory = controller._model_client_factory
    monkeypatch.setattr(controller, '_model_client_factory',
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('synthetic load failure')))
    selected = tmp_path / 'B.json'
    controller.import_model(str(selected))
    persisted = json.loads((controller.paths['config_dir'] / 'preferences.json').read_text())
    assert persisted['model_selection_failed'] is True
    assert persisted['requested_model_path'] == str(selected.resolve())
    assert persisted['model_path'] == str(controller.preferences['model_path'])
    controller.close()

    replacement_clients = []
    def replacement_factory(source, **kwargs):
        client = ControlledClient(source, **kwargs)
        replacement_clients.append(client)
        return client
    replacement_vm = GuanceViewModel()
    replacement = GuanceController(replacement_vm, paths=controller.paths,
                                   model_client_factory=replacement_factory)
    try:
        replacement._model.start()
        assert replacement_clients == []
        assert replacement.active_source is None
        assert replacement._selection_failed is True
        replacement.import_model(str(tmp_path / 'C.json'))
        assert len(replacement_clients) == 1
        assert replacement.active_source == (tmp_path / 'C.json').resolve()
        cleared = json.loads((controller.paths['config_dir'] / 'preferences.json').read_text())
        assert cleared['model_selection_failed'] is False
        assert cleared['requested_model_path'] == str(replacement.active_source)
    finally:
        replacement.close()
        app.processEvents()


def test_current_observation_is_buffered_and_submitted_only_to_successful_new_client(harness, monkeypatch, tmp_path):
    import threading
    controller, vm, clients, observation, app = harness
    old = clients[0]
    old_before = len(old.submitted)
    entered, release = threading.Event(), threading.Event()
    original = controller_module.save_preferences
    def blocked(paths, preferences):
        entered.set()
        assert release.wait(timeout=3)
        return original(paths, preferences)
    monkeypatch.setattr(controller_module, 'save_preferences', blocked)
    worker = threading.Thread(target=controller.import_model, args=(str(tmp_path / 'B.json'),))
    worker.start()
    assert entered.wait(timeout=2)
    # A fresh confirmed observation during persistence replaces the one captured
    # at transaction start, but the old client remains ineligible.
    later = GameObservation('test-session', 'test-game', 8, 124.0,
        2, 0, 0, 0, (0x11, 0x13), (2, 27, 27, 27), free_lead=True)
    controller._handle_update(EngineUpdate('observation', observation=later))
    assert len(old.submitted) == old_before
    release.set()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert len(clients) == 2
    new = clients[-1]
    assert [view.state_version for view in new.submitted] == [8]
    new.reply(new.submitted[-1])
    app.processEvents()
    assert vm.adviceCandidates
    assert controller._active_advice_key == later.key
