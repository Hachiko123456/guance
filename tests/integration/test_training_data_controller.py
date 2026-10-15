"""Fake-only controller subscriber tests for TD-05, TD-06 and TD-12."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from guance.application import controller as controller_module
from guance.contracts import EngineUpdate
from tests.integration.test_acceptance_runtime import build_controller
from tests.training_data.test_recorder import sequence


class StubRecorder:
    instances: list["StubRecorder"] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.updates: list[EngineUpdate] = []
        self.invalidations: list[str] = []
        self.close_timeouts: list[float] = []
        self.closed = False
        self.raise_on_update = False
        self.__class__.instances.append(self)

    def observe_update(self, update: EngineUpdate) -> None:
        if self.raise_on_update:
            raise OSError("synthetic writer failure")
        self.updates.append(update)

    def invalidate_current(self, reason: str) -> None:
        self.invalidations.append(reason)

    def close(self, timeout: float = 0.2) -> dict[str, Any]:
        self.close_timeouts.append(timeout)
        self.closed = True
        return {"complete": True, "writer_alive": False}

    def stats(self) -> dict[str, Any]:
        return {"accepting": not self.closed, "complete": self.closed}


def _state_update() -> EngineUpdate:
    return sequence()[0]


def test_training_subscriber_does_not_pause_or_replace_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    controller, viewmodel, _runtime, model = build_controller(tmp_path)
    recorder = StubRecorder()
    controller._training_recorder = recorder
    before_calls = list(model.calls)
    before_model = controller._model
    before_phase = viewmodel.phase

    try:
        controller._training_update(_state_update())

        assert recorder.updates
        assert controller._model is before_model
        assert model.calls == before_calls
        assert controller.session.paused is False
        assert viewmodel.phase == before_phase
        assert controller._training_failed is False
    finally:
        controller.close()


def test_training_error_invalidates_only_recorder_and_writer_failure_is_isolated(
    tmp_path: Path,
) -> None:
    controller, viewmodel, _runtime, model = build_controller(tmp_path)
    recorder = StubRecorder()
    controller._training_recorder = recorder
    before_calls = list(model.calls)
    before_phase = viewmodel.phase
    before_model = controller._model

    try:
        controller._training_update(EngineUpdate("error", "private engine detail", error_code="state_invalid"))
        assert recorder.invalidations == ["state_error"]
        assert model.calls == before_calls
        assert controller._model is before_model
        assert controller.session.paused is False
        assert viewmodel.phase == before_phase

        recorder.raise_on_update = True
        controller._training_update(_state_update())
        assert controller._training_failed is True
        assert recorder.invalidations[-1] == "external_loss"
        assert model.calls == before_calls
        assert controller._model is before_model
        assert viewmodel.phase == before_phase
    finally:
        controller.close()


def test_stop_closes_recorder_after_runtime_and_model_cleanup_with_zero_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, _viewmodel, runtime, model = build_controller(tmp_path)
    recorder = StubRecorder()
    controller._training_recorder = recorder
    events: list[str] = []

    original_runtime_stop = runtime.stop
    original_model_close = model.close
    monkeypatch.setattr(runtime, "stop", lambda: (events.append("runtime.stop"), original_runtime_stop())[1])
    monkeypatch.setattr(model, "close", lambda: (events.append("model.close"), original_model_close())[1])
    monkeypatch.setattr(recorder, "close", lambda timeout=0.2: (events.append("recorder.close"), recorder.close_timeouts.append(timeout), setattr(recorder, "closed", True), {"complete": True})[-1])

    try:
        controller._stop_impl()

        assert events.index("runtime.stop") < events.index("model.close") < events.index("recorder.close")
        assert recorder.close_timeouts == [0.0]
        assert controller._training_recorder is None
    finally:
        controller.close()


def test_default_recorder_is_created_for_each_start_and_closed_on_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    StubRecorder.instances.clear()
    monkeypatch.setattr(controller_module, "TrainingDataRecorder", StubRecorder)
    monkeypatch.delenv("GUANCE_TRAINING_DATA", raising=False)
    controller, _viewmodel, _runtime, _model = build_controller(tmp_path)

    try:
        controller._start_impl()
        assert StubRecorder.instances, "default-on capture must create its recorder at start"
        recorder = StubRecorder.instances[-1]
        controller._stop_impl()
        assert recorder.closed
        assert recorder.close_timeouts == [0.0]
    finally:
        controller.close()

def test_concurrent_start_and_capture_ensure_share_one_recorder(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    created, release = threading.Event(), threading.Event()
    StubRecorder.instances.clear()
    controller, _viewmodel, _runtime, _model = build_controller(tmp_path)
    class BarrierRecorder(StubRecorder):
        def __init__(self, *args, **kwargs):
            created.set()
            assert release.wait(5)
            super().__init__(*args, **kwargs)
    monkeypatch.setattr(controller_module, "TrainingDataRecorder", BarrierRecorder)
    monkeypatch.delenv("GUANCE_TRAINING_DATA", raising=False)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(controller._ensure_training_recorder)
            assert created.wait(2)
            second = pool.submit(controller._ensure_training_recorder)
            release.set()
            first.result(timeout=3)
            second.result(timeout=3)
        assert len(StubRecorder.instances) == 1
        assert controller._training_recorder is StubRecorder.instances[0]
    finally:
        release.set()
        controller.close()
    assert StubRecorder.instances[0].closed
from guance.presentation import viewmodel as _viewmodel_module


@pytest.fixture(autouse=True)
def _no_gui_screen_dependency_in_controller_tests(monkeypatch: pytest.MonkeyPatch):
    """Use actual VM for status/Qt signals, without assuming a QGuiApplication.

    Another integration module intentionally owns a plain QCoreApplication.
    These tests exercise Controller's recorder boundary, not native screens.
    """
    class HeadlessApp:
        @staticmethod
        def instance():
            return None

    monkeypatch.setattr(_viewmodel_module, "QGuiApplication", HeadlessApp)
