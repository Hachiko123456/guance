"""Phase 3 controller wiring: one request path per turn and fail-closed clearing.

Synthetic client/controller harness only; no weights or network. The real model
recommendation is covered by the iter2000 acceptance module.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from guance.application.controller import DebugCaptureError, GuanceController
from guance.config import save_preferences
from guance.contracts import (
    ConfirmedDecisionRecommendationRequest, EngineUpdate, GameObservation,
    PublicRecommendationRequest,
)
from guance.presentation.viewmodel import GuanceViewModel
from tests.integration.test_acceptance_runtime import FakeRuntime, controller_paths

MODEL_VERSION = "MARVEL-V2-v1.3-iter002000"
MODEL_SHA = "af0df1eaeb67c88e467ee78bf6b0494c49d000f5733d655ee81b40351d4ca9c8"


class _Client:
    def __init__(self, source, *, on_result, on_status, provider="guandan_marvel_v2", **kwargs):
        self.source = source
        self.on_result = on_result
        self.on_status = on_status
        self.provider = provider
        self.submitted = []
        self.confirmed = []
        self.started = self.closed = self.invalidated = 0
        self.fail_close = False

    def start(self):
        self.started += 1
        self.on_status({"model_status": "ready", "metadata": {
            "status": "ready", "provider": self.provider,
            "backend": "guandan" if self.provider == "guandan_marvel_v2" else "numpy",
            "runtime": "torch-cpu" if self.provider == "guandan_marvel_v2" else "numpy",
            "model_version": MODEL_VERSION, "model_sha256": MODEL_SHA,
            "capabilities": {"recommend": True, "next_token_policy": True,
                             "complete_action": True, "confirmed_decision": True},
            "wire_input_contract": "GuandanMARVELV2.ConfirmedDecision.v1",
        }})

    def submit(self, request, *, timeout_seconds=None):
        self.submitted.append(request)
        if isinstance(request, ConfirmedDecisionRecommendationRequest):
            self.confirmed.append(request)
        return "request-id"

    def invalidate(self):
        self.invalidated += 1

    def close(self):
        self.closed += 1
        if self.fail_close:
            raise RuntimeError("synthetic cleanup failure")


def _observation(state_version=7):
    return GameObservation("test-session", "test-game", state_version, 123.0, 2, 0, 0, 0,
                           (0x11, 0x13), (2, 27, 27, 27), free_lead=True)


def _baseline_payload():
    """A structurally valid confirmed-decision payload used only by the fake projector."""
    import copy
    import tests.integration.test_phase3_confirmed_recommendation as acceptance
    return copy.deepcopy(acceptance._synthetic_fixtures()[0][0])


def _decision(state_version=7):
    return SimpleNamespace(public_state=SimpleNamespace(identity=SimpleNamespace(
        session_id="test-session", game_id="test-game", state_version=state_version)))


def _harness(tmp_path, monkeypatch, *, provider, projector=None):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("GUANCE_TRAINING_DATA", "0")
    monkeypatch.delenv("GUANCE_MODEL_PATH", raising=False)
    monkeypatch.delenv("GUANCE_MODEL_BUNDLE", raising=False)
    paths = controller_paths(tmp_path)
    save_preferences(paths, {"model_path": str(tmp_path / "A.npz"),
                             "debug_capture_enabled": False})
    clients = []

    def factory(source, **kwargs):
        client = _Client(source, provider=provider, **kwargs)
        clients.append(client)
        return client

    calls = []
    base_payload = _baseline_payload()

    def default_projector(decision, artifact_id, artifact_sha256):
        calls.append((decision, artifact_id, artifact_sha256))
        identity = decision.public_state.identity
        payload = dict(base_payload)
        payload.update({"session_id": identity.session_id, "game_id": identity.game_id,
                        "state_version": identity.state_version})
        return payload

    vm = GuanceViewModel()
    controller = GuanceController(vm, paths=paths, model_client_factory=factory,
                                  confirmed_decision_projector=projector or default_projector)
    controller._runtime = FakeRuntime()
    controller._capture_port_available = lambda: True
    controller._model.start()
    vm.apply_status({"phase": "ready", "running": True, "connected": True})
    return controller, vm, clients[0], calls, app


def test_marvel_turn_uses_only_the_confirmed_decision_path(tmp_path, monkeypatch):
    controller, vm, client, calls, app = _harness(tmp_path, monkeypatch,
                                                  provider="guandan_marvel_v2")
    try:
        observation = _observation()
        controller._handle_update(EngineUpdate("observation", observation=observation))
        assert client.submitted == []
        assert controller._pending_confirmed_key == observation.key
        decision = _decision()
        controller._maybe_submit_confirmed(decision)
        assert len(client.confirmed) == 1, (
            controller._model.status.to_dict(), controller._model._selection_failed)
        request = client.confirmed[0]
        assert request.decision["session_id"] == "test-session"
        assert calls and calls[0][1] == MODEL_VERSION and calls[0][2] == MODEL_SHA
        assert controller._pending_confirmed_key is None
    finally:
        controller.close()
        app.processEvents()


def test_marvel_turn_without_decision_fails_closed(tmp_path, monkeypatch):
    controller, vm, client, calls, app = _harness(tmp_path, monkeypatch,
                                                  provider="guandan_marvel_v2")
    try:
        controller._handle_update(EngineUpdate("observation", observation=_observation()))
        assert controller._pending_confirmed_key is not None
        controller._flush_confirmed_pending()
        assert client.submitted == [] and client.confirmed == []
        assert client.invalidated >= 1
        assert not vm.adviceCandidates
        assert controller._active_advice_key is None
    finally:
        controller.close()
        app.processEvents()


def test_stale_or_unmatched_decision_is_not_submitted(tmp_path, monkeypatch):
    controller, vm, client, calls, app = _harness(tmp_path, monkeypatch,
                                                  provider="guandan_marvel_v2")
    try:
        controller._maybe_submit_confirmed(_decision())
        assert client.confirmed == []
        controller._handle_update(EngineUpdate("observation", observation=_observation(7)))
        controller._maybe_submit_confirmed(_decision(state_version=8))
        assert client.confirmed == []
    finally:
        controller.close()
        app.processEvents()


def _switch_harness(tmp_path, monkeypatch, *, initial_provider, live_subset=None,
                    aggregate_parser=None, accepted_host=None):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("GUANCE_TRAINING_DATA", "0")
    monkeypatch.delenv("GUANCE_MODEL_PATH", raising=False)
    monkeypatch.delenv("GUANCE_MODEL_BUNDLE", raising=False)
    paths = controller_paths(tmp_path)
    save_preferences(paths, {"model_path": str(tmp_path / "A.npz"),
                             "debug_capture_enabled": False})
    state = {"provider": initial_provider}
    clients = []

    def factory(source, **kwargs):
        client = _Client(source, provider=state["provider"], **kwargs)
        clients.append(client)
        return client

    vm = GuanceViewModel()
    controller = GuanceController(vm, paths=paths, model_client_factory=factory,
                                  confirmed_decision_projector=lambda *args: {},
                                  live_subset_provider=live_subset,
                                  aggregate_parser=aggregate_parser,
                                  accepted_host=accepted_host)
    controller._runtime = FakeRuntime()
    controller._capture_port_available = lambda: True
    controller._model.start()
    vm.apply_status({"phase": "ready", "running": True, "connected": True})
    return controller, vm, clients, state, app


def test_marvel_switch_never_replays_pending_as_public(tmp_path, monkeypatch):
    controller, vm, clients, state, app = _switch_harness(
        tmp_path, monkeypatch, initial_provider="guandan_marvel_v2")
    try:
        observation = _observation()
        controller._handle_update(EngineUpdate("observation", observation=observation))
        assert controller._pending_confirmed_key == observation.key
        before = len(clients[0].submitted)
        controller.import_model(str(tmp_path / "B.npz"))
        after = clients[1]
        assert after.submitted == []
        assert after.confirmed == []
        assert controller._pending_confirmed_key == observation.key
        assert controller._active_advice_key == observation.key
        assert len(clients[0].submitted) == before
    finally:
        controller.close()
        app.processEvents()


def test_fabledan_to_marvel_switch_does_not_replay_public(tmp_path, monkeypatch):
    controller, vm, clients, state, app = _switch_harness(
        tmp_path, monkeypatch, initial_provider="fabledan")
    try:
        observation = _observation()
        controller._handle_update(EngineUpdate("observation", observation=observation))
        assert len(clients[0].submitted) == 1  # FableDan public path
        state["provider"] = "guandan_marvel_v2"
        controller.import_model(str(tmp_path / "B.npz"))
        assert clients[1].submitted == []
        assert clients[1].confirmed == []
        assert controller._pending_confirmed_key == observation.key
    finally:
        controller.close()
        app.processEvents()


def test_marvel_to_fabledan_switch_replays_public_only(tmp_path, monkeypatch):
    controller, vm, clients, state, app = _switch_harness(
        tmp_path, monkeypatch, initial_provider="guandan_marvel_v2")
    try:
        observation = _observation()
        controller._handle_update(EngineUpdate("observation", observation=observation))
        assert controller._pending_confirmed_key == observation.key
        state["provider"] = "fabledan"
        controller.import_model(str(tmp_path / "B.npz"))
        assert len(clients[1].submitted) == 1  # public replay is correct for FableDan
        assert clients[1].confirmed == []
        assert controller._pending_confirmed_key is None
    finally:
        controller.close()
        app.processEvents()


def test_late_decision_for_old_selection_is_not_submitted(tmp_path, monkeypatch):
    controller, vm, clients, state, app = _switch_harness(
        tmp_path, monkeypatch, initial_provider="guandan_marvel_v2")
    try:
        observation = _observation()
        controller._handle_update(EngineUpdate("observation", observation=observation))
        # A stale decision from the previous selection identity fails projection
        # (identity is no longer the selected artifact) and must clear advice.
        stale = _decision()
        stale.artifact = "old-bundle"
        controller._decision_projector = lambda decision, artifact_id, sha: (
            (_ for _ in ()).throw(RuntimeError("stale identity"))
            if getattr(decision, "artifact", None) != artifact_id else {})
        controller._maybe_submit_confirmed(stale)
        assert clients[0].confirmed == []
        assert controller._pending_confirmed_key is None
        assert controller._active_advice_key is None
        assert clients[0].invalidated >= 1  # stale rejection invalidates
    finally:
        controller.close()
        app.processEvents()


def test_decision_drain_does_not_depend_on_debug_capture_recording(tmp_path, monkeypatch):
    """Recommendation submission must work with the evidence recorder disabled."""
    pending = []

    class _Provider:
        def adapt_event(self, event):
            return []

        def observe_applied(self, event, state, session_id):
            return None

        def reset_session(self):
            return None

        def set_model_artifact_identity_provider(self, provider):
            return None

        def bind_capture(self, capture_id):
            return None

        def unbind_capture(self):
            return None

        def bind_runtime_session(self, session_id):
            self.runtime_session = session_id

        def unbind_runtime_session(self):
            self.runtime_session = None

        def drain_decisions(self):
            values = tuple(pending)
            pending.clear()
            return values

    provider = _Provider()
    controller, vm, clients, state, app = _switch_harness(
        tmp_path, monkeypatch, initial_provider="guandan_marvel_v2",
        live_subset=provider)
    try:
        assert controller._canonical_converter is None
        base = _baseline_payload()
        controller._decision_projector = lambda decision, artifact_id, artifact_sha256: {
            **base, "session_id": decision.public_state.identity.session_id,
            "game_id": decision.public_state.identity.game_id,
            "state_version": decision.public_state.identity.state_version}
        observation = _observation()
        controller._handle_update(EngineUpdate("observation", observation=observation))
        assert controller._pending_confirmed_key == observation.key
        pending.append(_decision())
        controller._drain_live_decisions()
        assert len(clients[0].confirmed) == 1
        assert clients[0].submitted == [clients[0].confirmed[0]]
        assert controller._pending_confirmed_key is None
    finally:
        controller.close()
        app.processEvents()


def _assert_all_pending_cleared(controller):
    state = (controller._pending_confirmed_key, controller._pending_model_observation,
             controller._pending_model_generation, controller._active_advice_key,
             controller._latest_model_observation)
    assert state == (None, None, None, None, None), state


@pytest.mark.parametrize("case", [
    "stop", "close", "model_error", "model_unavailable", "model_timeout",
    "selection_failure", "cleanup_failure", "runtime_error", "capture_source_error",
    "state_advance", "terminal", "game_switch", "pause_error",
])
def test_lifecycle_clears_every_pending_identity(tmp_path, monkeypatch, case):
    controller, vm, clients, state, app = _switch_harness(
        tmp_path, monkeypatch, initial_provider="guandan_marvel_v2")
    try:
        controller._handle_update(EngineUpdate("observation", observation=_observation()))
        assert controller._pending_confirmed_key is not None
        if case == "stop":
            controller._stop_impl()
        elif case == "close":
            controller.close()
        elif case == "model_error":
            clients[0].on_status({"model_status": "error", "error_code": "MODEL_LOAD_FAILED"})
        elif case == "model_unavailable":
            clients[0].on_status({"model_status": "unavailable",
                                  "error_code": "MODEL_UNAVAILABLE"})
        elif case == "model_timeout":
            clients[0].on_status({"model_status": "timeout", "error_code": "MODEL_TIMEOUT"})
        elif case == "selection_failure":
            controller._model_client_factory = (
                lambda *a, **k: (_ for _ in ()).throw(RuntimeError("selection failed")))
            controller.import_model(str(tmp_path / "bad.npz"))
        elif case == "cleanup_failure":
            clients[0].fail_close = True
            controller.import_model(str(tmp_path / "B.npz"))
        elif case == "runtime_error":
            controller._on_runtime_status({"state": "error", "message": "capture failed"})
        elif case == "capture_source_error":
            _raise_capture_source_error(controller)
        elif case == "state_advance":
            controller._handle_update(EngineUpdate("state", data={"phase": "active"}))
        elif case == "game_switch":
            controller._handle_update(EngineUpdate("state", data={
                "phase": "active", "game_id": "other-game", "state_version": 9}))
        elif case == "terminal":
            controller._handle_update(EngineUpdate("state", data={"phase": "settled"}))
        else:
            controller._handle_update(EngineUpdate(
                "error", error_code="engine_paused", message="引擎已暂停"))
        _assert_all_pending_cleared(controller)
    finally:
        controller.close()
        app.processEvents()


def _raise_capture_source_error(controller):
    """Drive the production frame path into DebugCaptureError (source invalidation)."""
    from guance.contracts import SourceFrame
    from guance.infrastructure.protocol.live import GAME_HOST

    class _FailingRecorder:
        path = None
        session_dir = None
        stats = {"frames": 0}

        def append(self, frame):
            raise DebugCaptureError("debug source invalidated")

        def close(self):
            return None

    controller._debug_recorder = _FailingRecorder()
    controller._canonical_converter = None
    controller._on_frame(SourceFrame(
        host=GAME_HOST, payload=b"", received_at=1.0,
        connection_id="capture-source-error", sequence=1, direction="server"))


def test_capture_source_error_clears_every_pending_identity(tmp_path, monkeypatch):
    controller, vm, clients, state, app = _switch_harness(
        tmp_path, monkeypatch, initial_provider="guandan_marvel_v2")
    try:
        controller._handle_update(EngineUpdate("observation", observation=_observation()))
        assert controller._pending_confirmed_key is not None
        _raise_capture_source_error(controller)
        assert controller._debug_capture_failed is True
        assert controller._debug_recorder is None
        _assert_all_pending_cleared(controller)
    finally:
        controller.close()
        app.processEvents()


def test_model_switch_and_restart_do_not_revive_old_requests(tmp_path, monkeypatch):
    controller, vm, clients, state, app = _switch_harness(
        tmp_path, monkeypatch, initial_provider="guandan_marvel_v2")
    try:
        observation = _observation()
        controller._handle_update(EngineUpdate("observation", observation=observation))
        assert controller._pending_confirmed_key == observation.key
        controller.import_model(str(tmp_path / "B.npz"))
        new_client = clients[1]
        # The pending turn is re-bound to the new selection generation; the old
        # client must never deliver it and a stale key cannot match the new turn.
        assert controller._pending_confirmed_key == observation.key
        assert controller._pending_model_generation is None
        assert clients[0].submitted == [] and clients[0].confirmed == []
        controller._maybe_submit_confirmed(_decision(state_version=8))
        assert new_client.confirmed == []
        # Stop clears every identity and neither late decisions nor late model
        # callbacks may revive after a restart.
        controller._stop_impl()
        _assert_all_pending_cleared(controller)
        controller._maybe_submit_confirmed(_decision())
        controller._on_advice(
            {"session_id": "test-session", "game_id": "test-game", "state_version": 7},
            generation=controller.selection_generation)
        assert new_client.confirmed == []
        assert not vm.adviceCandidates
        # A real restart rebinds the producer; the old turn still cannot revive.
        controller._start_impl()
        _assert_all_pending_cleared(controller)
        controller._maybe_submit_confirmed(_decision())
        controller._on_advice(
            {"session_id": "test-session", "game_id": "test-game", "state_version": 7})
        assert all(client.confirmed == [] for client in clients)
        assert not vm.adviceCandidates
    finally:
        controller.close()
        app.processEvents()


def test_fabledan_turn_keeps_the_public_observation_path(tmp_path, monkeypatch):
    controller, vm, client, calls, app = _harness(tmp_path, monkeypatch, provider="fabledan")
    try:
        observation = _observation()
        controller._handle_update(EngineUpdate("observation", observation=observation))
        assert len(client.submitted) == 1
        assert isinstance(client.submitted[0], PublicRecommendationRequest)
        assert client.confirmed == []
        assert controller._pending_confirmed_key is None
        assert calls == []
    finally:
        controller.close()
        app.processEvents()


def test_real_provider_produces_confirmed_decisions_with_debug_disabled(tmp_path, monkeypatch):
    """Fixed offline controller wiring: real producer, evidence recorder disabled.

    Real SessionEngine + NormalExchangeLiveSubsetProvider + the controller's
    production frame/update/decision-drain path over the fixed offline capture
    and the Phase 2 frozen rules source; only the model client is a controlled
    fake. This is offline wiring evidence, never a live PASSED claim.
    """
    import base64
    import json as _json
    from pathlib import Path as _Path

    from guance.contracts import SourceFrame
    from guance.infrastructure.capture.live_subset import NormalExchangeLiveSubsetProvider

    runtime_source = _Path(
        r"D:\project\guance\build\acceptance\guance-model-platform-20261016"
        r"\phase2\runtime\marvel-e5c82e7c-d7acbc8d\source")
    capture = _Path(r"D:\project\guance\debug\captures\20261014-143204\frames.jsonl")
    if not capture.is_file() or not runtime_source.is_dir():
        pytest.skip("fixed offline capture or frozen runtime unavailable")
    provider = NormalExchangeLiveSubsetProvider(runtime_source)
    from guance.infrastructure.protocol import live

    controller, vm, clients, state, app = _switch_harness(
        tmp_path, monkeypatch, initial_provider="guandan_marvel_v2",
        live_subset=provider, aggregate_parser=live.parse_aggregate,
        accepted_host=live.GAME_HOST)
    try:
        assert controller.preferences.get("debug_capture_enabled") is False
        base = _baseline_payload()
        controller._decision_projector = lambda decision, artifact_id, artifact_sha256: {
            **base, "session_id": decision.public_state.identity.session_id,
            "game_id": decision.public_state.identity.game_id,
            "state_version": decision.public_state.identity.state_version}
        controller._open_debug_recorder()
        assert controller._debug_recorder is None
        assert controller._canonical_converter is None
        assert provider.decision_session_id == "runtime-session"
        debug_root = _Path(controller.paths["root_dir"]) / "debug"
        assert not debug_root.exists() or not [
            path for path in debug_root.rglob("*") if path.is_file()]

        drained = []
        real_drain = provider.drain_decisions

        def _counting_drain():
            values = real_drain()
            drained.extend(values)
            return values

        provider.drain_decisions = _counting_drain
        confirmed = public = 0
        for line in capture.open("rb"):
            record = _json.loads(line.decode("utf-8-sig"))
            payload_bytes = b""
            if record.get("direction") == "server":
                payload_bytes = base64.b64decode(record["payload_b64"], validate=True)
            frame = SourceFrame(host=str(record["host"]), payload=payload_bytes,
                                received_at=float(record["received_at"]),
                                connection_id=str(record.get("connection_id", "")),
                                sequence=int(record["sequence"]),
                                direction=str(record.get("direction")))
            # The production frame path: recorder (None) -> session.feed ->
            # controller updates -> decision drain.
            controller._on_frame(frame)
            confirmed = len(clients[0].confirmed)
            public = len(clients[0].submitted) - confirmed
            if confirmed > 0:
                break
        assert provider.model_adapter_eligible_count > 0
        assert len(drained) > 0
        assert confirmed > 0
        assert public == 0
        assert all(
            isinstance(request, ConfirmedDecisionRecommendationRequest)
            for request in clients[0].submitted)
        assert controller._canonical_converter is None
        assert controller._debug_recorder is None
        assert controller._pending_confirmed_key is None
        assert not debug_root.exists() or not [
            path for path in debug_root.rglob("*") if path.is_file()]
    finally:
        controller.close()
        app.processEvents()
