from __future__ import annotations

import base64
import io
import json
import threading
from pathlib import Path

import pytest

from guance.application.runtime import RuntimeOptions, RuntimeService
from guance.contracts import SourceFrame
from guance.infrastructure.capture.command import build_mitm_command
from guance.infrastructure.capture.diagnostics import BoundedDiagnostics, redact_text
from guance.infrastructure.capture.supervisor import SubprocessCaptureSupervisor
from guance.infrastructure.windows.certificate import WindowsCertificateManager
from guance.infrastructure.windows.proxy import InMemoryProxyBackend
from guance.ports.capture import (
    CaptureConfig,
    CaptureMode,
    CertificateStatus,
    ProxySnapshot,
    RuntimePaths,
    UpstreamProxy,
)


class FakeCapture:
    def __init__(self, events: list[str], *, ready: bool = True) -> None:
        self.events = events
        self.ready = ready
        self._running = False
        self.on_frame = None
        self.on_status = None

    @property
    def running(self) -> bool:
        return self._running

    def start(self, config, on_frame, on_status) -> None:
        self.events.append("capture.start")
        self._running = True
        self.on_frame = on_frame
        self.on_status = on_status

    def wait_ready(self, timeout: float) -> bool:
        self.events.append("capture.wait_ready")
        return self.ready

    def stop(self, timeout: float) -> None:
        self.events.append("capture.stop")
        self._running = False

    def close(self) -> None:
        self.events.append("capture.close")


class EventProxy(InMemoryProxyBackend):
    def __init__(self, current: ProxySnapshot, events: list[str]) -> None:
        super().__init__(current)
        self.events = events

    def apply_local(self, host: str, port: int) -> ProxySnapshot:
        self.events.append("proxy.apply_local")
        return super().apply_local(host, port)

    def restore(self, snapshot: ProxySnapshot, expected: ProxySnapshot | None = None) -> bool:
        self.events.append("proxy.restore")
        return super().restore(snapshot, expected)


class FakeCertificate:
    def __init__(self) -> None:
        self.trusted = True
        self.prepare_calls: list[bool] = []
        self.remove_calls: list[bool] = []

    def inspect(self) -> CertificateStatus:
        return CertificateStatus("trusted" if self.trusted else "untrusted", fingerprint="ABCD")

    def prepare(self, consent: bool, owner_hwnd: int = 0) -> dict:
        self.prepare_calls.append(consent)
        if consent:
            self.trusted = True
        return {"ok": consent, "state": "trusted" if consent else "consent_required"}

    def remove(self, consent: bool, owner_hwnd: int = 0) -> dict:
        self.remove_calls.append(consent)
        if consent:
            self.trusted = False
        return {"ok": consent, "state": "missing" if consent else "consent_required"}


def paths(tmp_path: Path) -> RuntimePaths:
    return RuntimePaths(
        data_dir=tmp_path / "data",
        runtime_dir=tmp_path / "runtime",
        config_dir=tmp_path / "config",
        logs_dir=tmp_path / "logs",
        cert_dir=tmp_path / "cert",
    )


def test_explicit_modes_never_embed_proxy_credentials() -> None:
    config = CaptureConfig(
        helper_path=Path("C:/Program Files/mitmproxy/mitmdump.exe"),
        addon_path=Path("addon.py"),
        confdir=Path("conf"),
        mode=CaptureMode.UPSTREAM_MIXED,
        upstream=UpstreamProxy("127.0.0.1", 7890, kind="mixed"),
    )
    command = build_mitm_command(config)
    assert "--mode" in command
    assert "upstream:http://127.0.0.1:7890" in command
    assert all("@" not in item for item in command)
    with pytest.raises(ValueError):
        UpstreamProxy("user:password@proxy.example", 8080)


def test_diagnostics_are_bounded_and_redacted() -> None:
    diagnostics = BoundedDiagnostics(max_records=1)
    diagnostics.add(
        "warning",
        "helper",
        "Bearer top-secret " + "x" * 1000,
        {"cookie": "session=private", "nested": {"authorization": "Bearer abc"}},
    )
    record = diagnostics.snapshot()[0]
    assert "top-secret" not in record["message"]
    assert record["data"]["cookie"] == "<redacted>"
    assert record["data"]["nested"]["authorization"] == "<redacted>"
    assert len(record["message"]) <= 512
    assert "private" not in redact_text("Cookie: private")


def test_runtime_restores_proxy_before_stopping_helper(tmp_path: Path) -> None:
    events: list[str] = []
    capture = FakeCapture(events)
    proxy = EventProxy(ProxySnapshot(enabled=False, bypass="<local>"), events)
    original = proxy.current
    certificate = FakeCertificate()
    statuses: list[dict] = []
    service = RuntimeService(
        paths(tmp_path),
        on_status=statuses.append,
        options=RuntimeOptions(require_certificate=True, addon_path=tmp_path / "addon.py"),
        capture=capture,
        proxy=proxy,
        certificate=certificate,
    )

    started = service.start()
    assert started["ok"] is True
    assert service.state == "running"
    stopped = service.stop()
    assert stopped["ok"] is True
    assert proxy.current == original
    assert events.index("proxy.apply_local") > events.index("capture.wait_ready")
    assert events.index("proxy.restore") < events.index("capture.stop")
    assert statuses[-1]["state"] == "stopped"


def test_runtime_does_not_switch_proxy_when_listener_is_not_ready(tmp_path: Path) -> None:
    events: list[str] = []
    capture = FakeCapture(events, ready=False)
    proxy = InMemoryProxyBackend(ProxySnapshot(enabled=False))
    service = RuntimeService(
        paths(tmp_path),
        options=RuntimeOptions(require_certificate=False),
        capture=capture,
        proxy=proxy,
        certificate=FakeCertificate(),
    )

    result = service.start()
    assert result["ok"] is False
    assert result["error_code"] == "HELPER_NOT_READY"
    assert proxy.writes == 0
    assert "capture.stop" in events


def test_certificate_manager_requires_explicit_consent(tmp_path: Path) -> None:
    cert = tmp_path / "mitmproxy-ca-cert.cer"
    cert.write_bytes(b"fake certificate")
    trusted = False
    calls: list[tuple[Path, int]] = []

    def checker(_path: Path) -> bool:
        return trusted

    def provisioner(path: Path, hwnd: int) -> None:
        nonlocal trusted
        calls.append((path, hwnd))
        trusted = True

    manager = WindowsCertificateManager(
        tmp_path,
        trust_checker=checker,
        provisioner=provisioner,
        remover=lambda path, hwnd: path.unlink(),
        platform_name="test",
    )
    denied = manager.prepare(False, 12)
    assert denied["state"] == "consent_required"
    assert not calls
    prepared = manager.prepare(True, 12)
    assert prepared["ok"] is True
    assert calls == [(cert, 12)]


class FakeProcess:
    def __init__(self) -> None:
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        self.pid = 42
        self._active = True
        self._lock = threading.Lock()

    def poll(self):
        with self._lock:
            return None if self._active else 0

    def terminate(self) -> None:
        with self._lock:
            self._active = False

    def kill(self) -> None:
        self.terminate()

    def wait(self, timeout=None):
        self.terminate()
        return 0


def test_supervisor_decodes_only_bounded_frame_records(tmp_path: Path) -> None:
    helper = tmp_path / "mitmdump.exe"
    addon = tmp_path / "addon.py"
    helper.write_bytes(b"stub")
    addon.write_bytes(b"stub")
    process = FakeProcess()
    payload = b'{"event":"safe"}'
    process.stdout = io.BytesIO(
        (
            "GUANCE_FRAME "
            + json.dumps(
                {
                    "host": "game.example",
                    "payload_b64": base64.b64encode(payload).decode("ascii"),
                    "received_at": 1.0,
                    "connection_id": "c1",
                    "sequence": 2,
                }
            )
            + "\nGUANCE_DIAG {\"cookie\":\"secret\"}\n"
        ).encode()
    )
    frames: list[SourceFrame] = []
    statuses: list[dict] = []
    supervisor = SubprocessCaptureSupervisor(
        process_factory=lambda *args, **kwargs: process,
        readiness_probe=lambda config, timeout: True,
    )
    config = CaptureConfig(helper, addon, tmp_path / "conf", max_frame_bytes=1024)
    supervisor.start(config, frames.append, statuses.append)
    assert supervisor.wait_ready(0.5)
    for _ in range(20):
        if frames:
            break
        threading.Event().wait(0.01)
    supervisor.stop(0.5)
    assert frames and frames[0].payload == payload
    assert any(status.get("state") == "helper_diag" for status in statuses)
    assert all(status.get("cookie") != "secret" for status in statuses)
