"""Application/runtime acceptance tests using only fakes.

No real Windows proxy, certificate store, network, mitmdump, or game process is
used here. Failures are release blockers and should be reported to the lead with
exact production locations.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from guance.application.controller import GuanceController
from guance.application.runtime import RuntimeOptions, RuntimeService
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
        "root_dir": Path("D:/project/guance"),
    }
    for path in values.values():
        path.mkdir(parents=True, exist_ok=True)
    return values


def build_controller(tmp_path: Path) -> tuple[GuanceController, GuanceViewModel, FakeRuntime, FakeModel]:
    vm = GuanceViewModel()
    controller = GuanceController(vm, paths=controller_paths(tmp_path))
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
