from __future__ import annotations

import base64
import hashlib
import io
import json
import socket
import subprocess
import threading
import time
from dataclasses import replace
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from guance.application.runtime import RuntimeOptions, RuntimeService
from guance.application.runtime.lease import ProxyLease, ProxyLeaseStore
from guance.contracts import SourceFrame
from guance.infrastructure.capture.command import build_mitm_command
from guance.infrastructure.capture import addon as capture_addon
from guance.infrastructure.capture.diagnostics import BoundedDiagnostics, redact_text
from guance.infrastructure.capture.proxy_pac import build_scoped_routing
from guance.infrastructure.capture.supervisor import SubprocessCaptureSupervisor
from guance.infrastructure.windows import certificate as certificate_module
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


def _scoped_service(tmp_path, proxy, capture, on_status=None):
    return RuntimeService(
        paths(tmp_path), capture=capture, proxy=proxy, on_status=on_status,
        options=RuntimeOptions(scoped_proxy=True, allowed_hosts=("game.example",),
                               require_certificate=False),
        certificate=FakeCertificate(),
    )


@pytest.mark.parametrize("original", [
    ProxySnapshot(enabled=False, auto_detect=True),
    ProxySnapshot(enabled=True, server="127.0.0.1:7897", bypass="<local>;*.corp.example"),
])
def test_scoped_capture_only_installs_temporary_pac(tmp_path, original):
    class ScopedCapture(FakeCapture):
        def start(self, config, *args):
            self.config = config
            super().start(config, *args)

    proxy = InMemoryProxyBackend(original)
    capture = ScopedCapture([])
    service = _scoped_service(tmp_path, proxy, capture)
    try:
        assert service.start()["ok"]
        assert replace(proxy.current, auto_config_url="") == original
        assert bool(capture.config.upstream) == original.enabled
        url = urlsplit(proxy.current.auto_config_url)
        assert url.hostname == "127.0.0.1"
        http = HTTPConnection(url.hostname, url.port, timeout=2)
        try:
            http.request("GET", url.path)
            response = http.getresponse()
            assert response.status == 200
            payload = response.read()
            reference = build_scoped_routing(original, ("game.example",), "127.0.0.1", 18080)
            assert payload == reference.script
        finally:
            http.close()
        assert service.stop()["ok"]
        assert proxy.current == original
        assert not capture.running
        with pytest.raises(OSError):
            with socket.create_connection((url.hostname, url.port), 0.2):
                pytest.fail("PAC listener is still listening")
        assert not (tmp_path / "config" / "proxy-lease.json").exists()
    finally:
        service.close()


@pytest.mark.parametrize("setting", ["pac", "wpad", "unsupported"])
def test_scoped_capture_preserves_unsupported_network_settings(tmp_path, setting):
    class DiscoveredWpad(InMemoryProxyBackend):
        def wpad_available(self):
            return True

    if setting == "pac":
        original = ProxySnapshot(enabled=False, auto_config_url="https://corp.example/proxy.pac")
    elif setting == "wpad":
        original = ProxySnapshot(enabled=False, auto_detect=True)
    else:
        original = ProxySnapshot(enabled=True, server="socks=127.0.0.1:1090")
    proxy = DiscoveredWpad(original)
    capture = FakeCapture([])
    service = _scoped_service(tmp_path, proxy, capture)
    try:
        assert service.start()["error_code"] == "PROXY_ROUTING_UNSUPPORTED"
        assert proxy.current == original and proxy.writes == 0
        assert not capture.events
        assert not (tmp_path / "config" / "proxy-lease.json").exists()
    finally:
        service.close()


def test_scoped_capture_removes_its_pac_when_another_app_changes_manual_proxy(tmp_path):
    proxy = InMemoryProxyBackend(ProxySnapshot(enabled=True, server="127.0.0.1:7897"))
    statuses = []
    capture = FakeCapture([])
    service = _scoped_service(tmp_path, proxy, capture, statuses.append)
    try:
        assert service.start()["ok"]
        external = replace(proxy.current, server="127.0.0.1:8997", bypass="new.example")
        proxy.current = external
        deadline = time.monotonic() + 4
        while not any(status["state"] == "network_settings_changed" for status in statuses):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert proxy.current == replace(external, auto_config_url="")
        assert not capture.running
        assert not (tmp_path / "config" / "proxy-lease.json").exists()
    finally:
        service.close()


def test_scoped_stale_recovery_only_clears_owned_pac(tmp_path, monkeypatch):
    original = ProxySnapshot(enabled=True, server="127.0.0.1:7897")
    old_pac = "http://127.0.0.1:49152/guance-stale.pac"
    applied = replace(original, auto_config_url=old_pac)
    store = ProxyLeaseStore(paths(tmp_path).config_dir / "proxy-lease.json")
    store.save(ProxyLease(original, applied, 999999, "127.0.0.1", 18080, state="active"))
    monkeypatch.setattr("guance.application.runtime.service._pid_is_alive", lambda pid: False)
    external = replace(applied, server="127.0.0.1:9998")
    proxy = InMemoryProxyBackend(external)
    service = _scoped_service(tmp_path, proxy, FakeCapture([]))
    try:
        assert service.recover_stale_proxy()["ok"]
        assert proxy.current == replace(external, auto_config_url="")
        assert not store.path.exists()
    finally:
        service.close()


def test_runtime_paths_and_capture_confdir_share_mitmproxy_directory(tmp_path: Path) -> None:
    runtime_paths = RuntimePaths.from_mapping(
        {
            "data_dir": tmp_path / "data",
            "runtime_dir": tmp_path / "runtime",
            "config_dir": tmp_path / "config",
            "logs_dir": tmp_path / "logs",
            # Regression input from the old app_paths implementation.
            "cert_dir": tmp_path / "data" / "certs",
        }
    )
    service = RuntimeService(
        runtime_paths,
        options=RuntimeOptions(require_certificate=False),
        capture=FakeCapture([]),
        proxy=InMemoryProxyBackend(ProxySnapshot(enabled=False)),
        certificate=FakeCertificate(),
    )

    assert runtime_paths.cert_dir == tmp_path / "config" / "mitmproxy"
    assert service.paths.cert_dir == service.paths.config_dir / "mitmproxy"
    assert service._build_config().confdir == service.paths.cert_dir

    cert = service.paths.cert_dir / "mitmproxy-ca-cert.cer"
    cert.parent.mkdir(parents=True)
    cert.write_bytes(b"capture CA")
    inspected = RuntimeService(
        runtime_paths,
        options=RuntimeOptions(require_certificate=False),
        capture=FakeCapture([]),
        proxy=InMemoryProxyBackend(ProxySnapshot(enabled=False)),
        certificate=WindowsCertificateManager(
            runtime_paths.cert_dir,
            trust_checker=lambda _path: False,
            platform_name="test",
        ),
    ).inspect()
    assert inspected["certificate"]["state"] == "untrusted"
    assert inspected["certificate"]["path"] == str(cert)


def test_certificate_store_check_is_non_recursive_and_uses_one_sha1_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cert = tmp_path / "mitmproxy-ca-cert.cer"
    cert.write_bytes(b"fake DER certificate")
    calls: list[list[str]] = []
    monkeypatch.setattr(certificate_module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(certificate_module, "_certutil", lambda: "certutil.exe")

    def fake_run(args: list[str]) -> dict[str, bool]:
        calls.append(args)
        return {"ok": True}

    monkeypatch.setattr(certificate_module, "_run_certutil", fake_run)

    assert certificate_module.check_current_user_root(cert) is True
    expected_sha1 = hashlib.sha1(cert.read_bytes()).hexdigest().upper()
    assert calls == [["-user", "-store", "Root", expected_sha1]]


def test_certificate_store_remove_uses_same_sha1_thumbprint_and_not_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cert = tmp_path / "mitmproxy-ca-cert.cer"
    cert.write_bytes(b"fake DER certificate")
    calls: list[list[str]] = []
    monkeypatch.setattr(certificate_module, "os", SimpleNamespace(name="nt"))

    def fake_run(args: list[str]) -> dict[str, bool]:
        calls.append(args)
        return {"ok": True}

    monkeypatch.setattr(certificate_module, "_run_certutil", fake_run)

    result = certificate_module.remove_current_user_root(cert)
    expected_sha1 = hashlib.sha1(cert.read_bytes()).hexdigest().upper()
    assert result["ok"] is True
    assert calls == [["-user", "-delstore", "Root", expected_sha1]]
    assert cert.is_file()


def test_certutil_timeout_is_bounded_and_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(certificate_module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(certificate_module, "_certutil", lambda: "certutil.exe")

    def hanging_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(kwargs.get("args", args[0]), kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", hanging_run)
    result = certificate_module._run_certutil(["-user", "-store", "Root", "ABC"], timeout=0.01)
    assert result["ok"] is False
    assert result["timed_out"] is True
    assert "超时" in result["message"]


def test_certificate_manager_consent_and_state_transitions(tmp_path: Path) -> None:
    cert = tmp_path / "mitmproxy-ca-cert.cer"
    cert.write_bytes(b"fake certificate")
    trusted = False
    provision_calls: list[Path] = []
    remove_calls: list[Path] = []

    def checker(_path: Path) -> bool:
        return trusted

    def provisioner(path: Path, _hwnd: int) -> dict[str, bool]:
        nonlocal trusted
        provision_calls.append(path)
        trusted = True
        return {"ok": True}

    def remover(path: Path, _hwnd: int) -> dict[str, bool]:
        nonlocal trusted
        remove_calls.append(path)
        trusted = False
        # The generated CA file remains on disk after trust-store removal.
        return {"ok": True}

    manager = WindowsCertificateManager(
        tmp_path,
        trust_checker=checker,
        provisioner=provisioner,
        remover=remover,
        platform_name="test",
    )

    denied_prepare = manager.prepare(False, 12)
    denied_remove = manager.remove(False, 12)
    assert denied_prepare["state"] == "consent_required"
    assert denied_remove["state"] == "consent_required"
    assert not provision_calls and not remove_calls

    prepared = manager.prepare(True, 12)
    assert prepared["ok"] is True
    assert prepared["state"] == "trusted"
    assert provision_calls == [cert]

    repeated_prepare = manager.prepare(True, 12)
    assert repeated_prepare["ok"] is True
    assert repeated_prepare["state"] == "already_trusted"
    assert provision_calls == [cert]

    removed = manager.remove(True, 12)
    assert removed["ok"] is True
    assert removed["state"] == "removed"
    assert removed["certificate"]["state"] == "untrusted"
    assert remove_calls == [cert]
    assert cert.is_file()

    repeated_remove = manager.remove(True, 12)
    assert repeated_remove["ok"] is True
    assert repeated_remove["state"] == "removed"
    assert remove_calls == [cert, cert]


def test_certificate_manager_reports_missing_and_install_failure(tmp_path: Path) -> None:
    missing = WindowsCertificateManager(tmp_path, platform_name="test")
    assert missing.prepare(True)["state"] == "missing"

    cert = tmp_path / "mitmproxy-ca-cert.cer"
    cert.write_bytes(b"fake certificate")
    manager = WindowsCertificateManager(
        tmp_path,
        trust_checker=lambda _path: False,
        provisioner=lambda _path, _hwnd: {"ok": False, "stderr": "access denied"},
        platform_name="test",
    )
    failed = manager.prepare(True)
    assert failed["ok"] is False
    assert failed["state"] == "install_failed"
    assert "失败" in failed["message"]
    assert failed["operation"]["ok"] is False


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
                    "direction": "server",
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
    assert frames[0].direction == "server"
    assert any(status.get("state") == "helper_diag" for status in statuses)
    assert all(status.get("cookie") != "secret" for status in statuses)


@pytest.mark.parametrize("direction", ["client", "server"])
def test_addon_emits_direction_without_filtering(direction: str, monkeypatch, capsys) -> None:
    monkeypatch.setenv("GUANCE_ALLOWED_HOSTS", "game.example")
    addon = capture_addon.GuanceAddon()
    flow = SimpleNamespace(
        id="connection-1",
        request=SimpleNamespace(pretty_host="game.example"),
        websocket=SimpleNamespace(
            messages=[SimpleNamespace(content=b"payload", from_client=direction == "client")]
        ),
    )

    addon.websocket_message(flow)

    line = capsys.readouterr().out.strip()
    assert line.startswith("GUANCE_FRAME ")
    record = json.loads(line.removeprefix("GUANCE_FRAME "))
    assert record["direction"] == direction
    assert record["payload_b64"] == base64.b64encode(b"payload").decode("ascii")


@pytest.mark.parametrize("invalid_direction", [None, "CLIENT", "peer", 1, {"value": "server"}])
def test_supervisor_normalizes_invalid_direction_to_unknown(
    tmp_path: Path, invalid_direction: object
) -> None:
    helper = tmp_path / "mitmdump.exe"
    addon = tmp_path / "addon.py"
    helper.write_bytes(b"stub")
    addon.write_bytes(b"stub")
    process = FakeProcess()
    payload = b"payload"
    process.stdout = io.BytesIO(
        (
            "GUANCE_FRAME "
            + json.dumps(
                {
                    "host": "game.example",
                    "payload_b64": base64.b64encode(payload).decode("ascii"),
                    "received_at": 1.0,
                    "connection_id": "c1",
                    "sequence": 3,
                    "direction": invalid_direction,
                }
            )
            + "\n"
        ).encode()
    )
    frames: list[SourceFrame] = []
    supervisor = SubprocessCaptureSupervisor(
        process_factory=lambda *args, **kwargs: process,
        readiness_probe=lambda config, timeout: True,
    )
    config = CaptureConfig(helper, addon, tmp_path / "conf", max_frame_bytes=1024)
    supervisor.start(config, frames.append, lambda _status: None)
    assert supervisor.wait_ready(0.5)
    for _ in range(20):
        if frames:
            break
        threading.Event().wait(0.01)
    supervisor.stop(0.5)

    assert frames and frames[0].direction == "unknown"


@pytest.mark.parametrize(
    ("mode", "upstream"),
    [
        (CaptureMode.DIRECT, None),
        (CaptureMode.UPSTREAM_HTTP, UpstreamProxy("127.0.0.1", 7897, kind="http")),
        (CaptureMode.UPSTREAM_MIXED, UpstreamProxy("127.0.0.1", 7897, kind="mixed")),
    ],
)
def test_runtime_modes_preserve_full_proxy_snapshot_on_stop(
    tmp_path: Path, mode: CaptureMode, upstream: UpstreamProxy | None
) -> None:
    original = ProxySnapshot(
        enabled=True,
        server="127.0.0.1:7897",
        auto_config_url="https://proxy.example/pac.js",
        auto_detect=True,
        bypass="localhost;127.0.0.1;<local>",
    )
    proxy = InMemoryProxyBackend(original)
    service = RuntimeService(
        paths(tmp_path),
        options=RuntimeOptions(mode=mode, upstream=upstream, require_certificate=False),
        capture=FakeCapture([]),
        proxy=proxy,
        certificate=FakeCertificate(),
    )
    assert service.start()["ok"] is True
    assert proxy.current.enabled is True
    assert proxy.current.server == "127.0.0.1:18080"
    stopped = service.stop()
    assert stopped["ok"] is True
    assert proxy.current == original


def test_runtime_no_proxy_snapshot_is_restored_exactly(tmp_path: Path) -> None:
    original = ProxySnapshot(
        enabled=False,
        server="",
        auto_config_url="",
        auto_detect=False,
        bypass="*.internal;<local>",
    )
    proxy = InMemoryProxyBackend(original)
    service = RuntimeService(
        paths(tmp_path),
        options=RuntimeOptions(require_certificate=False),
        capture=FakeCapture([]),
        proxy=proxy,
        certificate=FakeCertificate(),
    )
    assert service.start()["ok"] is True
    assert service.stop()["ok"] is True
    assert proxy.current == original


def test_start_failure_cleans_lease_and_releases_instance_lock(tmp_path: Path) -> None:
    proxy = InMemoryProxyBackend(ProxySnapshot(enabled=True, server="127.0.0.1:7897"))
    service = RuntimeService(
        paths(tmp_path),
        options=RuntimeOptions(require_certificate=False),
        capture=FakeCapture([], ready=False),
        proxy=proxy,
        certificate=FakeCertificate(),
    )
    result = service.start()
    assert result["ok"] is False
    assert not (tmp_path / "config" / "proxy-lease.json").exists()
    assert service._instance_lock.held is False
    # A later runtime can acquire the same lock after a failed start.
    second = RuntimeService(
        paths(tmp_path),
        options=RuntimeOptions(require_certificate=False),
        capture=FakeCapture([]),
        proxy=proxy,
        certificate=FakeCertificate(),
    )
    assert second.start()["ok"] is True
    assert second.stop()["ok"] is True


def test_stale_lease_recovers_only_when_proxy_is_still_applied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = ProxySnapshot(enabled=True, server="127.0.0.1:7897", bypass="<local>")
    applied = ProxySnapshot(enabled=True, server="127.0.0.1:18080", bypass="<local>")
    proxy = InMemoryProxyBackend(applied)
    store = ProxyLeaseStore(tmp_path / "config" / "proxy-lease.json")
    lease = ProxyLease(
        original_proxy=original,
        applied_proxy=applied,
        app_pid=987654,
        helper_pid=987655,
        target_host="127.0.0.1",
        target_port=18080,
        state="active",
    )
    store.save(lease)
    monkeypatch.setattr("guance.application.runtime.service._pid_is_alive", lambda _pid: False)
    service = RuntimeService(
        paths(tmp_path),
        options=RuntimeOptions(require_certificate=False),
        capture=FakeCapture([]),
        proxy=proxy,
        certificate=FakeCertificate(),
    )
    result = service.recover_stale_proxy()
    assert result["ok"] is True
    assert proxy.current == original
    assert store.load() is None
    assert (tmp_path / "config" / "proxy-lease.journal.jsonl").exists()


def test_stale_lease_does_not_overwrite_external_proxy_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = ProxySnapshot(enabled=True, server="127.0.0.1:7897")
    applied = ProxySnapshot(enabled=True, server="127.0.0.1:18080")
    external = ProxySnapshot(enabled=True, server="127.0.0.1:9999")
    proxy = InMemoryProxyBackend(external)
    store = ProxyLeaseStore(tmp_path / "config" / "proxy-lease.json")
    lease = ProxyLease(
        original_proxy=original,
        applied_proxy=applied,
        app_pid=987654,
        target_host="127.0.0.1",
        target_port=18080,
        state="active",
    )
    store.save(lease)
    monkeypatch.setattr("guance.application.runtime.service._pid_is_alive", lambda _pid: False)
    service = RuntimeService(
        paths(tmp_path),
        options=RuntimeOptions(require_certificate=False),
        capture=FakeCapture([]),
        proxy=proxy,
        certificate=FakeCertificate(),
    )
    result = service.recover_stale_proxy()
    assert result["ok"] is False
    assert result["error_code"] == "PROXY_RECOVERY_CONFLICT"
    assert proxy.current == external
    assert store.load() is not None
    assert store.load().state == "conflict"


def test_second_runtime_cannot_take_over_first_runtime_lease(tmp_path: Path) -> None:
    proxy = InMemoryProxyBackend(ProxySnapshot(enabled=True, server="127.0.0.1:7897"))
    first = RuntimeService(
        paths(tmp_path),
        options=RuntimeOptions(require_certificate=False),
        capture=FakeCapture([]),
        proxy=proxy,
        certificate=FakeCertificate(),
    )
    second = RuntimeService(
        paths(tmp_path),
        options=RuntimeOptions(require_certificate=False),
        capture=FakeCapture([]),
        proxy=proxy,
        certificate=FakeCertificate(),
    )
    assert first.start()["ok"] is True
    blocked = second.start()
    assert blocked["ok"] is False
    assert blocked["error_code"] == "RUNTIME_ALREADY_ACTIVE"
    assert first.stop()["ok"] is True
