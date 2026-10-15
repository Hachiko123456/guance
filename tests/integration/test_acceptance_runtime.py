"""Application/runtime acceptance tests using only fakes.

No real Windows proxy, certificate store, network, mitmdump, or game process is
used here. Failures are release blockers and should be reported to the lead with
exact production locations.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
import zipfile
from typing import Any

from guance.application.controller import DebugFrameRecorder, GuanceController
from guance.config import app_paths
from guance.application.runtime import RuntimeOptions, RuntimeService
from guance.contracts import EngineUpdate, SourceFrame
from guance.infrastructure.capture.command import build_mitm_command
from guance.infrastructure.capture.diagnostics import BoundedDiagnostics
from guance.infrastructure.windows.proxy import InMemoryProxyBackend
from guance.ports.capture import (
    CaptureMode,
    CertificateStatus,
    ProxySnapshot,
    RuntimePaths,
    UpstreamProxy,
)
from guance.presentation.viewmodel import GuanceViewModel


class FakeModel:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self) -> bool:
        self.calls.append("start")
        return True

    def invalidate(self) -> None:
        self.calls.append("invalidate")

    def close(self) -> None:
        self.calls.append("close")

    def submit(self, observation: Any) -> None:
        self.calls.append("submit")


class FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.inspect_data = {"state": "idle", "proxy": {"enabled": False}}

    def prepare_certificate(self, consent: bool) -> dict[str, Any]:
        self.calls.append(("prepare_certificate", consent))
        return {"ok": bool(consent), "state": "trusted" if consent else "consent_required"}

    def start(self) -> dict[str, Any]:
        self.calls.append(("start", None))
        return {"ok": True, "mode": "direct"}

    def stop(self) -> dict[str, Any]:
        self.calls.append(("stop", None))
        return {"ok": True, "message": "stopped"}

    def repair(self) -> dict[str, Any]:
        self.calls.append(("repair", None))
        return {"ok": True, "message": "repaired"}

    def remove_certificate(self, consent: bool) -> dict[str, Any]:
        self.calls.append(("remove_certificate", consent))
        return {"ok": bool(consent), "message": "removed" if consent else "consent_required"}

    def inspect(self) -> dict[str, Any]:
        self.calls.append(("inspect", None))
        return self.inspect_data

    def close(self) -> None:
        self.calls.append(("close", None))


class FakeCertificate:
    def __init__(self, state: str = "trusted") -> None:
        self.state = state
        self.calls: list[tuple[str, bool]] = []

    def inspect(self) -> CertificateStatus:
        return CertificateStatus(self.state, fingerprint="TEST")

    def prepare(self, consent: bool, owner_hwnd: int = 0) -> dict[str, Any]:
        self.calls.append(("prepare", consent))
        return {"ok": consent, "state": "trusted" if consent else "consent_required"}

    def remove(self, consent: bool, owner_hwnd: int = 0) -> dict[str, Any]:
        self.calls.append(("remove", consent))
        return {"ok": consent, "state": "missing" if consent else "consent_required"}


class FakeCapture:
    def __init__(self, events: list[str], ready: bool = True) -> None:
        self.events = events
        self.ready = ready
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self, config, on_frame, on_status) -> None:
        self.events.append("capture.start")
        self._running = True

    def wait_ready(self, timeout: float) -> bool:
        self.events.append("capture.wait_ready")
        return self.ready

    def stop(self, timeout: float) -> None:
        self.events.append("capture.stop")
        self._running = False

    def close(self) -> None:
        self.events.append("capture.close")


def runtime_paths(tmp_path: Path) -> RuntimePaths:
    return RuntimePaths(
        data_dir=tmp_path / "data",
        runtime_dir=tmp_path / "runtime",
        config_dir=tmp_path / "config",
        logs_dir=tmp_path / "logs",
        cert_dir=tmp_path / "cert",
    )


def controller_paths(tmp_path: Path) -> dict[str, Path]:
    values = {
        "data_dir": tmp_path / "data",
        "runtime_dir": tmp_path / "runtime",
        "config_dir": tmp_path / "config",
        "logs_dir": tmp_path / "logs",
        "cert_dir": tmp_path / "cert",
        "exports_dir": tmp_path / "exports",
        "models_dir": tmp_path / "models",
        # Keep debug-capture fixtures inside pytest's isolated workspace.  The
        # production controller still resolves its root from the real project
        # or frozen data directory; tests must never create artifacts there.
        "root_dir": tmp_path / "project",
    }
    for path in values.values():
        path.mkdir(parents=True, exist_ok=True)
    return values


def build_controller(tmp_path: Path) -> tuple[GuanceController, GuanceViewModel, FakeRuntime, FakeModel]:
    vm = GuanceViewModel()
    controller = GuanceController(vm, paths=controller_paths(tmp_path))
    controller._capture_port_available = lambda: True
    runtime = FakeRuntime()
    model = FakeModel()
    controller._runtime = runtime
    controller._model.close()
    controller._model = model
    return controller, vm, runtime, model


def test_core_002_stale_advice_is_rejected() -> None:
    vm = GuanceViewModel()
    vm.apply_observation({"session_id": "s", "game_id": "g", "state_version": 2,
                          "level": 5, "my_seat": 1, "current_seat": 1,
                          "hand": [0x12], "remaining": [1, 2, 3, 4]})
    vm.apply_advice({"session_id": "s", "game_id": "g", "state_version": 1,
                     "status": "ready", "candidates": [{"kind": "single", "cards": [0x12]}]})
    assert vm.adviceVisible is False
    assert vm.adviceCandidates == []


def test_controller_ui_signal_consumers_and_operations(tmp_path: Path) -> None:
    controller, vm, runtime, model = build_controller(tmp_path)
    controller._ensure_certificate_material = lambda: None

    controller.authorize(False)
    assert runtime.calls == []

    controller._authorize_impl(True)
    assert ("prepare_certificate", True) in runtime.calls
    assert ("start", None) in runtime.calls
    assert "start" in model.calls

    controller._start_impl()
    assert runtime.calls.count(("start", None)) == 2

    controller._stop_impl()
    assert ("stop", None) in runtime.calls
    assert "invalidate" in model.calls and "close" in model.calls

    controller.repair()
    assert ("repair", None) in runtime.calls

    controller.remove_certificate(True)
    assert ("remove_certificate", True) in runtime.calls

    controller.log.write(
        "test_secret", message="Bearer top-secret", cookie="session=private",
        authorization="Bearer abc", private_key="-----BEGIN PRIVATE KEY-----secret",
    )
    vm.request_export_diagnostics()
    exports = list((tmp_path / "exports").glob("*.zip"))
    assert any(p.name.startswith("diagnostics-") for p in exports)
    import zipfile
    with zipfile.ZipFile(exports[-1]) as archive:
        names = set(archive.namelist())
        assert names == {"manifest.json", "recent.jsonl"}
        content = "\n".join(archive.read(name).decode("utf-8") for name in names)
        assert "top-secret" not in content
        assert "session=private" not in content
        assert "BEGIN PRIVATE KEY" not in content
        assert ".mitm" not in content
        assert ".npz" not in content
    controller.close()


def test_ui_010_model_import_has_a_controller_consumer(tmp_path: Path) -> None:
    events: list[str] = []

    class SpyController(GuanceController):
        def import_model(self, path: str) -> None:
            events.append(path)

    vm = GuanceViewModel()
    controller = SpyController(vm, paths=controller_paths(tmp_path))
    weights = tmp_path / "model.npz"
    weights.write_bytes(b"not a real model")
    vm.request_model_import(weights.as_uri())
    assert [Path(events[0])] == [weights]
    controller.close()



def test_core_003_real_profile_fixture_is_legal_if_available() -> None:
    # The required private weights are intentionally not checked into Git. The
    # existing model acceptance tests own the full best.npz load/recommendation
    # coverage; this marker makes the matrix explicit without requiring a
    # network or a Windows runtime.
    from pathlib import Path
    import pytest
    from guance.contracts import GameObservation
    from guance.infrastructure.model.advisor import FableDanAdvisor

    root = Path(__file__).parents[2]
    weights = root / "data" / "profiles" / "tencent_daguandan" / "models" / "best.npz"
    if not weights.is_file():
        pytest.skip("private best.npz is not available")
    advisor = FableDanAdvisor(weights, top_n=3)
    assert advisor.load()["status"] == "loaded"
    record = {
        "session_id": "acceptance", "game_id": "fixture", "state_version": 1, "captured_at": 0.0,
        "level": 5, "my_seat": 0, "current_seat": 0, "first_seat": 0,
        "hand": [0x15, 0x25, 0x11, 0x21, 0x3D, 0x1B, 0x2B, 0x4A],
        "remaining": [8, 8, 8, 8], "free_lead": True, "trick": 0,
    }
    result = advisor.recommend(GameObservation.from_dict(record), request_id="acceptance")
    assert result.status == "ready"
    assert result.candidates
    assert set(result.candidates[0].cards).issubset(set(record["hand"]))


def test_core_004_controller_reports_model_failure_without_rule_fallback(tmp_path: Path) -> None:
    controller, vm, _runtime, _model = build_controller(tmp_path)
    controller._on_model_status({"model_status": "error", "message": "worker failed"})
    assert vm.modelState == "error"
    assert vm.phase == "error"
    assert vm.lastError == "worker failed"
    controller.close()

def test_core_005_direct_runtime_config_does_not_use_upstream(tmp_path: Path) -> None:
    service = RuntimeService(
        runtime_paths(tmp_path),
        options=RuntimeOptions(mode=CaptureMode.DIRECT, require_certificate=False),
        capture=FakeCapture([]),
        proxy=InMemoryProxyBackend(ProxySnapshot(enabled=False)),
        certificate=FakeCertificate(),
    )
    config = service._build_config()
    assert config.mode is CaptureMode.DIRECT
    assert config.upstream is None


def test_core_006_upstream_runtime_config_is_explicit_and_redacted(tmp_path: Path) -> None:
    service = RuntimeService(
        runtime_paths(tmp_path),
        options=RuntimeOptions(
            mode=CaptureMode.UPSTREAM_MIXED,
            upstream=UpstreamProxy("127.0.0.1", 7897, kind="mixed"),
            require_certificate=False,
        ),
        capture=FakeCapture([]),
        proxy=InMemoryProxyBackend(ProxySnapshot(enabled=False)),
        certificate=FakeCertificate(),
    )
    config = service._build_config()
    command = build_mitm_command(config)
    assert config.mode is CaptureMode.UPSTREAM_MIXED
    assert config.upstream is not None
    assert "upstream:http://127.0.0.1:7897" in command
    assert all("@" not in token for token in command)


def test_core_007_certificate_consent_false_is_noop(tmp_path: Path) -> None:
    certificate = FakeCertificate(state="untrusted")
    service = RuntimeService(
        runtime_paths(tmp_path),
        options=RuntimeOptions(require_certificate=True),
        capture=FakeCapture([]),
        proxy=InMemoryProxyBackend(ProxySnapshot(enabled=False)),
        certificate=certificate,
    )
    result = service.prepare_certificate(False)
    assert result["ok"] is False
    assert certificate.calls == [] or certificate.calls == [("prepare", False)]


def test_core_008_diagnostics_are_redacted_and_bounded(tmp_path: Path) -> None:
    diagnostics = BoundedDiagnostics(max_records=2)
    diagnostics.add("warning", "test", "Bearer super-secret", {
        "cookie": "session=secret", "authorization": "Bearer abc", "safe": "ok"
    })
    result = diagnostics.snapshot()
    assert len(result) == 1
    record = result[0]
    assert "super-secret" not in json.dumps(record)
    assert "secret" not in json.dumps(record)
    assert record["data"]["safe"] == "ok"


def test_core_009_runtime_stops_safely_before_helper_termination(tmp_path: Path) -> None:
    events: list[str] = []
    capture = FakeCapture(events)
    proxy = InMemoryProxyBackend(ProxySnapshot(enabled=False))
    service = RuntimeService(
        runtime_paths(tmp_path),
        options=RuntimeOptions(require_certificate=False),
        capture=capture,
        proxy=proxy,
        certificate=FakeCertificate(),
    )
    assert service.start()["ok"] is True
    assert service.stop()["ok"] is True
    assert events.index("capture.wait_ready") < events.index("capture.stop")
    assert service.state == "idle"


def test_application_paths_keep_capture_ca_and_runtime_confdir_aligned(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GUANCE_DATA_DIR", str(tmp_path / "Guance"))
    paths = app_paths()
    assert paths["cert_dir"] == paths["config_dir"] / "mitmproxy"



def test_debug_frame_recorder_persists_before_parser(tmp_path: Path) -> None:
    recorder = DebugFrameRecorder(tmp_path / "captures")
    path = recorder.start()
    frame = SourceFrame(
        host="hlxyxws.huanle.qq.com",
        payload=b"debug-frame",
        received_at=123.5,
        connection_id="conn-1",
        sequence=7,
        direction="server",
    )
    recorder.append(frame)
    recorder.close()

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["host"] == frame.host
    assert records[0]["sequence"] == frame.sequence
    assert records[0]["direction"] == frame.direction
    assert records[0]["payload_bytes"] == len(frame.payload)
    assert records[0]["payload_b64"] == "ZGVidWctZnJhbWU="


def test_debug_frame_recorder_exposes_waiting_status_and_live_game_metadata(tmp_path: Path) -> None:
    recorder = DebugFrameRecorder(tmp_path / "captures")
    path = recorder.start()

    status_path = path.parent / "capture_status.json"
    initial = json.loads(status_path.read_text(encoding="utf-8"))
    assert initial["state"] == "waiting_for_frames"
    assert initial["frames"] == 0

    recorder.append(SourceFrame(
        host="hlxyxws.huanle.qq.com",
        payload=b"first-frame",
        received_at=1.0,
        connection_id="conn-1",
        sequence=7,
        direction="server",
    ))
    recorder.begin_game("game-0001", 7)

    live_status = json.loads(status_path.read_text(encoding="utf-8"))
    assert live_status["state"] in {"capturing", "capturing_game"}
    assert live_status["frames"] == 1
    assert (path.parent / "games" / "game-0001" / "metadata.json").is_file()

    recorder.close()
    closed = json.loads(status_path.read_text(encoding="utf-8"))
    assert closed["schema"] == "guance-capture-closed-sidecar-v1"
    assert closed["state"] == "closed"
    assert closed["producer"] == "guance.DebugFrameRecorder"
    assert closed["producer_version"] == "1"
    assert closed["protocol_identity"] == "TencentDaguandanObservedProtocol.v20261014"
    assert closed["capture_id"] == path.parent.name
    assert closed["source_filename"] == "frames.jsonl"
    assert closed["source_bytes"] == path.stat().st_size
    assert closed["source_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert closed["frames"] == 1
    assert closed["directions"] == {"server": 1}
    assert closed["connection_epochs"] == [{
        "ordinal": 1, "start_sequence": 7, "end_sequence": 7,
        "frames": 1, "directions": {"server": 1},
    }]
    assert closed["final_sequence"] == 7
    assert closed["close_reason"] == "recorder_closed_with_incomplete_game"
    assert closed["closed_at"]
    assert not status_path.with_suffix(".tmp").exists()


def test_debug_frame_recorder_closed_sidecar_is_bound_after_source_close(tmp_path: Path) -> None:
    recorder = DebugFrameRecorder(tmp_path / "captures")
    path = recorder.start()
    recorder.append(SourceFrame(
        host="hlxyxws.huanle.qq.com", payload=b"first", received_at=1.0,
        connection_id="conn-1", sequence=1, direction="client",
    ))
    recorder.begin_game("game-0001", 1)
    recorder.append(SourceFrame(
        host="hlxyxws.huanle.qq.com", payload=b"second", received_at=2.0,
        connection_id="conn-1", sequence=2, direction="server",
    ))
    recorder.finish_game("game-0001", 2)
    recorder.close()
    sidecar = json.loads((path.parent / "capture_status.json").read_text(encoding="utf-8"))
    assert sidecar["state"] == "closed"
    assert sidecar["source_bytes"] == path.stat().st_size
    assert sidecar["source_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert sidecar["directions"] == {"client": 1, "server": 1}
    assert sidecar["connection_epochs"] == [{
        "ordinal": 1, "start_sequence": 1, "end_sequence": 2,
        "frames": 2, "directions": {"client": 1, "server": 1},
    }]
    assert sidecar["final_sequence"] == 2
    assert sidecar["close_reason"] == "recorder_closed"
    assert not (path.parent / "capture_status.tmp").exists()
    assert sidecar["games"] == [{
        "game_id": "game-0001", "status": "settled", "start_frame_index": 0,
        "end_frame_index": 1, "start_sequence": 1, "end_sequence": 2,
    }]
    sidecar_bytes = (path.parent / "capture_status.json").read_bytes()
    recorder.close()
    assert (path.parent / "capture_status.json").read_bytes() == sidecar_bytes
    text = json.dumps(sidecar, sort_keys=True).lower()
    assert "payload" not in text and "cookie" not in text and "authorization" not in text


def test_debug_frame_recorder_never_writes_closed_sidecar_after_source_close_failure(tmp_path: Path) -> None:
    recorder = DebugFrameRecorder(tmp_path / "captures")
    path = recorder.start()
    recorder.append(SourceFrame(
        host="hlxyxws.huanle.qq.com", payload=b"frame", received_at=1.0,
        connection_id="conn-1", sequence=1, direction="server",
    ))
    underlying = recorder._stream

    class FailingStream:
        def flush(self):
            raise OSError("synthetic close failure")

        def close(self):
            if underlying is not None:
                underlying.close()

    recorder._stream = FailingStream()
    recorder.close()
    assert underlying is None or underlying.closed
    sidecar = json.loads((path.parent / "capture_status.json").read_text(encoding="utf-8"))
    assert sidecar["state"] == "close_failed"
    assert sidecar["close_reason"] == "source_close_failed"
    assert sidecar["source_sha256"] == ""
    assert sidecar["closed_at"] == ""


def test_controller_preserves_first_engine_error_over_repeated_paused_updates(tmp_path: Path) -> None:
    controller, _vm, _runtime, _model = build_controller(tmp_path)

    controller._handle_update(EngineUpdate(
        kind="error", message="played card absent from own hand", error_code="state_invalid"
    ))
    controller._handle_update(EngineUpdate(
        kind="error", message="played card absent from own hand", error_code="engine_paused"
    ))
    controller._handle_update(EngineUpdate(
        kind="error", message="played card absent from own hand", error_code="engine_paused"
    ))

    assert controller._primary_engine_error == {
        "code": "state_invalid",
        "message": "played card absent from own hand",
        "game_id": "",
    }
    assert controller._engine_error_repeats == 2


def test_floating_layout_is_persisted_and_restored(tmp_path: Path) -> None:
    controller, vm, _runtime, _model = build_controller(tmp_path)
    layout = {"saved": True, "mode": "left", "x": 17, "y": 23, "width": 520, "height": 720}
    controller.save_floating_layout(layout)
    preferences = json.loads((tmp_path / "config" / "preferences.json").read_text(encoding="utf-8"))
    assert preferences["floating_layout"] == layout
    controller.close()

    vm2 = GuanceViewModel()
    controller2 = GuanceController(vm2, paths=controller_paths(tmp_path))
    assert vm2.floatingLayout == layout
    controller2.close()


def test_controller_writes_debug_frame_before_session_feed(tmp_path: Path) -> None:
    controller, _vm, _runtime, _model = build_controller(tmp_path)
    controller.paths["root_dir"] = tmp_path / "project"
    controller.preferences["debug_capture_enabled"] = True
    controller._open_debug_recorder()
    observed: list[bool] = []
    controller.session.feed = lambda _frame: observed.append(
        bool(controller._debug_recorder and controller._debug_recorder.path and
             controller._debug_recorder.path.read_text(encoding="utf-8"))
    ) or []

    controller._on_frame(SourceFrame(
        host="game.example",
        payload=b"before-parse",
        received_at=1.0,
        connection_id="c",
        sequence=1,
    ))
    controller._close_debug_recorder()

    assert observed == [True]


def test_diagnostics_export_contains_current_error_state_and_redacted_metadata(tmp_path: Path) -> None:
    controller, vm, runtime, _model = build_controller(tmp_path)
    controller.preferences["debug_capture_enabled"] = True
    controller._open_debug_recorder()
    controller._debug_recorder.append(SourceFrame(
        host="hlxyxws.huanle.qq.com",
        payload=b"raw-opponent-hand-must-not-be-exported",
        received_at=123.5,
        connection_id="conn-1",
        sequence=7,
        direction="server",
    ))
    vm.apply_status({
        "phase": "error",
        "error_message": "played card absent from own hand",
        "diagnostics": {"state_version": 2, "sync_error": "state_invalid"},
    })
    vm.append_log({"level": "ERROR", "code": "STATE_INVALID", "message": "state_version=2"})
    controller.session._paused_reason = "played card absent from own hand"

    controller.export_diagnostics()

    archives = sorted((tmp_path / "exports").glob("diagnostics-*.zip"))
    assert len(archives) == 1
    with zipfile.ZipFile(archives[0]) as archive:
        names = set(archive.namelist())
        assert names == {"manifest.json", "recent.jsonl"}
        manifest = json.loads(archive.read("manifest.json"))
        text = "\n".join(archive.read(name).decode("utf-8") for name in names)

    status = manifest["status"]
    assert status["view_model"]["last_error"] == "played card absent from own hand"
    assert status["view_model"]["phase"] == "error"
    assert status["view_model"]["diagnostics"][-1]["label"] == "sync_error"
    assert status["session"]["paused"] is True
    assert status["session"]["paused_reason"] == "played card absent from own hand"
    assert status["debug_capture"]["frames"] == 1
    assert status["debug_capture"]["directions"] == {"server": 1}
    assert status["debug_capture"]["session_directory"]
    assert "raw-opponent-hand-must-not-be-exported" not in text
    assert "payload_b64" not in text
    assert "DIAGNOSTICS_EXPORTED" in "\n".join(
        str(item) for item in vm.logs
    )
    assert archives[0].name in "\n".join(str(item) for item in vm.logs)


def test_diagnostics_export_failure_is_observable(tmp_path: Path) -> None:
    controller, vm, _runtime, _model = build_controller(tmp_path)

    def fail_export(*_args, **_kwargs):
        raise OSError("disk full")

    controller.log.export = fail_export
    controller.export_diagnostics()

    assert not list((tmp_path / "exports").glob("*.zip"))
    assert any(
        item.get("code") == "DIAGNOSTICS_EXPORT_FAILED"
        and "OSError" in item.get("message", "")
        for item in vm.logs
    )


def test_phase6_controller_preserves_experimental_unavailable_metadata_without_ready(tmp_path: Path) -> None:
    controller, vm, _runtime, _model = build_controller(tmp_path)
    try:
        controller._on_model_status({
            "model_status": "unavailable",
            "error_code": "GUANDAN_STATE_UNAVAILABLE",
            "message": "current public state unsupported",
            "metadata": {
                "status": "unavailable", "provider": "guandan_marvel_v2",
                "backend": "guandan", "runtime": "torch-cpu",
                "model_version": "MARVEL-V2-v1.3-iter000100",
                "model_sha256": "4" * 64, "deployment_fingerprint": "3" * 64,
                "score_kind": "marvel_v2_policy_path_score",
                "classification": "evaluation_only", "experimental": True,
                "production_ready": False, "strength_claim": False,
                "public_self_supported": False,
                "capabilities": {"next_token_policy": True},
            },
        })
        assert vm.modelState == "unavailable"
        assert vm.modelMetadata["provider"] == "guandan_marvel_v2"
        assert vm.modelMetadata["experimental"] is True
        assert vm.modelMetadata["production_ready"] is False
        assert vm.adviceCandidates == []
        assert vm.phase != "error"
    finally:
        controller.close()


def test_frozen_no_capture_package_does_not_fallback_to_system_helper(
        tmp_path: Path, monkeypatch) -> None:
    controller, _vm, _runtime, _model = build_controller(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    options = controller._mode_options()

    expected_runtime = controller.paths["runtime_dir"] / "mitmproxy"
    assert options.helper_path == expected_runtime / "mitmdump.exe"
    assert options.addon_path == expected_runtime / "addon.py"
    assert not options.helper_path.is_file()
    assert str(options.helper_path) != r"C:\Program Files\mitmproxy\bin\mitmdump.exe"
