"""MBA-P0-001/P0-005, MBA-P1-005: lifecycle ownership and invalidation."""
from dataclasses import replace
import threading

import pytest

from guance.application.model_service import ModelService
from guance.contracts import (AdviceCandidate, CanonicalAdviceResult, ModelLifecycleState,
                               ModelStatusSnapshot)
from guance.infrastructure.model.errors import ModelBackendError


class FakeClient:
    def __init__(self, source, *, on_result, on_status, **kwargs):
        self.source, self.on_result, self.on_status = source, on_result, on_status
        self.started = 0
        self.closed = 0
        self.invalidated = 0
        self.submitted = []
    def start(self):
        self.started += 1
        self.on_status({"model_status": "ready", "metadata": {
            "provider": "test", "backend": "synthetic", "runtime": "test",
            "model_version": "v1", "capabilities": {"recommend": True, "top_n": True},
        }})
    def submit(self, request, *, timeout_seconds=None):
        self.submitted.append(request)
        return "request"
    def invalidate(self):
        self.invalidated += 1
    def close(self):
        self.closed += 1


def result(view, request_id="request"):
    return CanonicalAdviceResult(*view.key, request_id, provider="test", model_version="v1",
        score_kind="test_score", candidates=(AdviceCandidate((0x11,), "play", "single", 0.1,
                                                            score_kind="test_score"),))


def make_service(tmp_path):
    clients, results, statuses = [], [], []
    def factory(*args, **kwargs):
        instance = FakeClient(*args, **kwargs)
        clients.append(instance)
        return instance
    service = ModelService(tmp_path / "chosen.npz", client_factory=factory,
                           on_result=results.append, on_status=statuses.append)
    return service, clients, results, statuses


def test_model_service_start_submit_invalidate_and_close(tmp_path, observation, view):
    service, clients, results, statuses = make_service(tmp_path)
    client = clients[0]
    assert client.started == 0
    assert isinstance(service.status, ModelStatusSnapshot)
    assert service.status.state is ModelLifecycleState.UNINITIALIZED
    service.start()
    assert service.status.state is ModelLifecycleState.READY
    assert service.status.metadata.provider == "test"
    assert service.status.metadata.backend == "synthetic"
    assert service.status.metadata.capabilities.recommend is True
    assert service.status.metadata.capabilities.top_n is True
    service.submit(observation)
    assert [item.view for item in client.submitted] == [view]
    client.on_result(result(view))
    assert len(results) == 1
    service.invalidate()
    client.on_result(result(view))
    assert len(results) == 1
    assert client.invalidated == 1
    service.close()
    service.close()
    assert client.closed == 1
    assert service.status.state is ModelLifecycleState.CLOSED
    with pytest.raises(ModelBackendError):
        service.start()
    with pytest.raises(ModelBackendError):
        service.import_model(tmp_path / "another.npz")


def test_synchronous_callback_during_submit_is_delivered_after_request_identity_is_registered(tmp_path, view):
    clients, results, statuses = [], [], []

    class SyncClient(FakeClient):
        def submit(self, request, *, timeout_seconds=None):
            self.submitted.append(request)
            request_id = "sync-request"
            self.on_result(result(request.view, request_id))
            return request_id

    def factory(*args, **kwargs):
        instance = SyncClient(*args, **kwargs)
        clients.append(instance)
        return instance

    service = ModelService(tmp_path / "chosen.npz", client_factory=factory,
                           on_result=results.append, on_status=statuses.append)
    service.submit(view)
    assert len(results) == 1
    assert isinstance(results[0], CanonicalAdviceResult)
    assert results[0].request_id == "sync-request"
    service.close()


def test_import_model_closes_old_and_ignores_old_callbacks_even_same_key(tmp_path, view):
    service, clients, results, statuses = make_service(tmp_path)
    service.submit(view)
    old = clients[0]
    service.import_model(tmp_path / "new-bundle" / "manifest.json")
    assert old.closed == 1
    assert old.invalidated == 1
    assert clients[1].started == 1
    assert service.selected_source == tmp_path / "new-bundle" / "manifest.json"
    service.submit(view)
    old.on_result(result(view))
    old.on_status({"model_status": "error", "error_code": "MODEL_LOAD_FAILED"})
    assert not results
    assert service.status.state is ModelLifecycleState.READY
    assert service.status.metadata.provider == "test"
    clients[1].on_result(result(view))
    assert len(results) == 1
    service.close()


def test_failed_import_does_not_restore_previous_client(tmp_path, view):
    instances = []
    def factory(source, **kwargs):
        if source.name == "bad":
            raise RuntimeError("SECRET_SENTINEL")
        instance = FakeClient(source, **kwargs)
        instances.append(instance)
        return instance
    statuses = []
    service = ModelService(tmp_path / "good", client_factory=factory, on_status=statuses.append)
    service.start()
    service.import_model(tmp_path / "bad")
    assert instances[0].closed == 1
    assert service.status.state is ModelLifecycleState.ERROR
    assert service.selected_source.name == "bad"
    service.submit(view)
    assert not instances[0].submitted
    assert service.status.state is ModelLifecycleState.UNAVAILABLE
    assert "SECRET_SENTINEL" not in str(statuses)
    service.close()


def test_status_snapshot_and_invalid_input_are_safe(tmp_path, view):
    service, clients, results, statuses = make_service(tmp_path)
    clients[0].on_status({"model_status": "ready", "metadata": {"provider": "test",
        "payload": "SECRET_SENTINEL"}, "message": "SECRET_SENTINEL"})
    snapshot = service.status
    assert snapshot.metadata.provider == "test"
    assert service.status.metadata.provider == "test"
    assert "SECRET_SENTINEL" not in str(statuses)
    service.submit(replace(view, ambiguities=("unclear",)))
    assert not clients[0].submitted
    assert clients[0].invalidated == 1
    assert service.status.state is ModelLifecycleState.ERROR
    service.close()


def test_missing_source_never_selects_a_default(tmp_path, view):
    def no_factory(*args, **kwargs):
        pytest.fail("No model may be implicitly selected")
    service = ModelService(client_factory=no_factory)
    service.start()
    service.submit(view)
    assert service.selected_source is None
    assert service.status.state is ModelLifecycleState.UNAVAILABLE
    service.close()


def test_service_rejects_stale_and_malformed_results(tmp_path, view):
    service, clients, results, statuses = make_service(tmp_path)
    service.submit(view)
    clients[0].on_result(result(replace(view, state_version=6)))
    assert not results
    invalid = result(view).to_dict()
    invalid["opponent_hands"] = "SECRET_SENTINEL"
    clients[0].on_result(invalid)
    assert not results
    assert service.status.error_code == "MODEL_OUTPUT_INVALID"
    assert "SECRET_SENTINEL" not in str(statuses)
    service.close()


def test_result_callback_does_not_hold_facade_state_mutex(tmp_path, view):
    service, clients, results, statuses = make_service(tmp_path)
    entered, release, submitted = threading.Event(), threading.Event(), threading.Event()
    def callback(result):
        entered.set()
        release.wait(timeout=3)
    service._on_result = callback
    service.submit(view)
    worker = threading.Thread(target=lambda: clients[0].on_result(result(view)), daemon=True)
    worker.start()
    try:
        assert entered.wait(timeout=2)
        def submit():
            service.submit(replace(view, state_version=8))
            submitted.set()
        caller = threading.Thread(target=submit, daemon=True)
        caller.start()
        completed = submitted.wait(timeout=1)
        release.set()
        caller.join(timeout=2)
        worker.join(timeout=2)
        assert completed, "Facade callback blocked a concurrent lifecycle operation"
    finally:
        release.set()
        service.close()


def test_reentrant_close_during_factory_callback_cannot_resurrect_client(tmp_path):
    created = []
    service = ModelService()
    def factory(source, **kwargs):
        client = FakeClient(source, **kwargs)
        created.append(client)
        service.close()
        return client
    service._factory = factory
    service.import_model(tmp_path / "selected.npz")
    assert service.status.state is ModelLifecycleState.CLOSED
    assert created[0].closed == 1
    assert created[0].started == 0
    assert service._client is None


def test_status_property_is_typed_and_not_callable(tmp_path):
    service, clients, results, statuses = make_service(tmp_path)
    service.start()
    snapshot = service.status
    assert isinstance(snapshot, ModelStatusSnapshot)
    assert snapshot.state is ModelLifecycleState.READY
    assert snapshot.metadata.provider == "test"
    assert not callable(snapshot)
    assert snapshot.to_dict()["model_status"] == "ready"
    service.close()


def test_missing_request_id_never_becomes_a_stale_result_bypass(tmp_path, view, monkeypatch):
    service, clients, results, _statuses = make_service(tmp_path)
    client = clients[0]
    monkeypatch.setattr(client, "submit", lambda _request, **kwargs: None)
    try:
        service.submit(view)
        client.on_result(result(view))
        service.submit(view)
        client.on_result(result(view))
        assert results == []
    finally:
        service.close()


def test_phase6_unavailable_status_keeps_bounded_identity_and_rejects_old_callbacks(tmp_path, view):
    service, clients, results, statuses = make_service(tmp_path)
    client = clients[0]
    service.start()
    client.on_status({
        "model_status": "unavailable", "error_code": "GUANDAN_STATE_UNAVAILABLE",
        "metadata": {
            "status": "unavailable", "provider": "guandan_marvel_v2",
            "backend": "guandan", "runtime": "torch-cpu",
            "runtime_version": "2.13.0+cpu", "model_version": "MARVEL-V2-v1.3-iter000100",
            "model_sha256": "4" * 64, "bundle_fingerprint": "1" * 64,
            "deployment_fingerprint": "3" * 64, "experimental": True,
            "production_ready": False, "strength_claim": False,
            "public_self_supported": False, "classification": "evaluation_only",
            "formal_evaluation_status": "not_run", "champion_status": "not_established",
            "capabilities": {"next_token_policy": True, "recommend": False},
            "private_path": r"D:\secret",
        },
    })
    assert service.status.state is ModelLifecycleState.UNAVAILABLE
    assert service.status.metadata.provider == "guandan_marvel_v2"
    assert service.status.metadata.experimental is True
    assert service.status.metadata.production_ready is False
    assert service.status.metadata.extras == {}
    service.invalidate()
    client.on_result(result(view))
    assert results == []
