"""Application orchestration for capture, proxy, and certificate lifecycles."""
from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from guance.contracts import SourceFrame
from guance.infrastructure.capture.diagnostics import BoundedDiagnostics, redact_text, redact_value
from guance.infrastructure.capture.supervisor import CaptureStartError, SubprocessCaptureSupervisor
from guance.infrastructure.windows.certificate import WindowsCertificateManager
from guance.infrastructure.windows.proxy import WindowsProxyBackend
from guance.ports.capture import (
    CaptureConfig,
    CaptureMode,
    CaptureProcessPort,
    CertificatePort,
    CertificateStatus,
    FrameHandler,
    ProxyPort,
    ProxySnapshot,
    RuntimeConfigError,
    RuntimePaths,
    StatusHandler,
    UpstreamProxy,
)


@dataclass(frozen=True, slots=True)
class RuntimeOptions:
    """Explicit capture options used to build a :class:`CaptureConfig`."""

    mode: CaptureMode = CaptureMode.DIRECT
    upstream: UpstreamProxy | None = None
    helper_path: Path | None = None
    addon_path: Path | None = None
    listen_host: str = "127.0.0.1"
    listen_port: int = 18080
    allowed_hosts: tuple[str, ...] = ()
    max_frame_bytes: int = 1_048_576
    startup_timeout: float = 8.0
    stop_timeout: float = 3.0
    require_certificate: bool = True

    def __post_init__(self) -> None:
        try:
            mode = self.mode if isinstance(self.mode, CaptureMode) else CaptureMode(self.mode)
        except (TypeError, ValueError) as exc:
            raise RuntimeConfigError(f"unsupported capture mode: {self.mode!r}") from exc
        object.__setattr__(self, "mode", mode)
        if self.upstream is not None and not isinstance(self.upstream, UpstreamProxy):
            raise RuntimeConfigError("upstream must be an UpstreamProxy")
        if self.helper_path is not None:
            object.__setattr__(self, "helper_path", Path(self.helper_path))
        if self.addon_path is not None:
            object.__setattr__(self, "addon_path", Path(self.addon_path))
        object.__setattr__(
            self,
            "allowed_hosts",
            tuple(str(item).strip() for item in self.allowed_hosts if str(item).strip()),
        )
        if mode is CaptureMode.AUTO:
            raise RuntimeConfigError(
                "capture mode 'auto' is not supported; choose direct, upstream_http, "
                "or upstream_mixed"
            )
        if mode is CaptureMode.DIRECT and self.upstream is not None:
            raise RuntimeConfigError("direct capture mode must not include an upstream proxy")
        if mode is CaptureMode.UPSTREAM_HTTP:
            if self.upstream is None or self.upstream.kind != "http":
                raise RuntimeConfigError("upstream_http mode requires an HTTP upstream proxy")
        if mode is CaptureMode.UPSTREAM_MIXED:
            if self.upstream is None or self.upstream.kind != "mixed":
                raise RuntimeConfigError("upstream_mixed mode requires a mixed upstream proxy")
        if not 1 <= self.listen_port <= 65535:
            raise RuntimeConfigError("listen_port must be between 1 and 65535")
        if self.startup_timeout <= 0 or self.stop_timeout <= 0:
            raise RuntimeConfigError("runtime timeouts must be positive")


class RuntimeService:
    """Coordinate a safe capture session.

    Start order is intentionally strict: inspect trust, snapshot the current
    proxy, launch the helper, wait for its listener, and only then switch the
    current-user proxy.  Stop order is the reverse safety order: restore the
    user's proxy first (with an ownership check), then terminate the helper.
    """

    _TRUSTED_STATES = frozenset({"trusted", "ready", "installed", "ok"})

    def __init__(
        self,
        paths: RuntimePaths | Mapping[str, Path | str],
        on_frame: FrameHandler | None = None,
        on_status: StatusHandler | None = None,
        *,
        options: RuntimeOptions | None = None,
        capture: CaptureProcessPort | None = None,
        capture_process: CaptureProcessPort | None = None,
        proxy: ProxyPort | None = None,
        proxy_port: ProxyPort | None = None,
        certificate: CertificatePort | None = None,
        certificate_port: CertificatePort | None = None,
        diagnostics: BoundedDiagnostics | None = None,
    ) -> None:
        if capture is not None and capture_process is not None:
            raise TypeError("provide only one of capture or capture_process")
        if proxy is not None and proxy_port is not None:
            raise TypeError("provide only one of proxy or proxy_port")
        if certificate is not None and certificate_port is not None:
            raise TypeError("provide only one of certificate or certificate_port")
        self.paths = paths if isinstance(paths, RuntimePaths) else RuntimePaths.from_mapping(paths)
        self.options = options or RuntimeOptions()
        self._on_frame = on_frame or (lambda _frame: None)
        self._on_status = on_status or (lambda _status: None)
        self._diagnostics = diagnostics or BoundedDiagnostics()
        self._capture = capture or capture_process or SubprocessCaptureSupervisor()
        self._proxy = proxy or proxy_port or WindowsProxyBackend()
        self._certificate = (
            certificate
            or certificate_port
            or WindowsCertificateManager(self.paths.cert_dir)
        )
        self._lock = threading.RLock()
        self._state = "idle"
        self._closed = False
        self._original_proxy: ProxySnapshot | None = None
        self._applied_proxy: ProxySnapshot | None = None
        self._config: CaptureConfig | None = None
        self._last_error: dict[str, Any] | None = None

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def diagnostics(self) -> BoundedDiagnostics:
        return self._diagnostics

    @property
    def running(self) -> bool:
        try:
            return bool(self._capture.running)
        except Exception:
            return False

    def inspect(self) -> dict[str, Any]:
        with self._lock:
            certificate = self._inspect_certificate()
            current_proxy: ProxySnapshot | None = None
            try:
                current_proxy = self._proxy.snapshot()
            except Exception as exc:
                self._record("warning", "proxy_inspect_failed", str(exc))
            config = self._config
            helper_path = self._helper_path()
            result: dict[str, Any] = {
                "ok": True,
                "state": self._state,
                "running": self.running,
                "mode": self.options.mode.value,
                "upstream": self._upstream_summary(),
                "helper": {
                    "path": helper_path.name,
                    "exists": helper_path.is_file(),
                    "configured": config is not None,
                },
                "certificate": certificate,
                "proxy": {
                    "available": current_proxy is not None,
                    "active": self._applied_proxy is not None,
                    "current": current_proxy.to_dict() if current_proxy else None,
                },
                "last_error": self._last_error,
                "diagnostics": self._diagnostics.snapshot(),
            }
            return redact_value(result)

    def prepare_certificate(self, consent: bool, owner_hwnd: int = 0) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                return self._error("CLOSED", "runtime service is closed")
            try:
                result = self._certificate.prepare(bool(consent), int(owner_hwnd))
            except Exception as exc:
                return self._error(
                    "CERTIFICATE_OPERATION",
                    f"certificate preparation failed: {type(exc).__name__}",
                )
            safe = self._safe_mapping(result)
            self._record_result("certificate_prepare", safe)
            self._emit_status({"state": "certificate", **safe})
            return safe

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                return self._error("CLOSED", "runtime service is closed")
            if self.running or self._state == "running":
                return {
                    "ok": True,
                    "state": "already_running",
                    "message": "capture is already running",
                }
            if self._state == "starting":
                return self._error("BUSY", "capture start is already in progress")
            self._state = "starting"
            self._last_error = None
            self._emit_status({"state": "starting", "mode": self.options.mode.value})

            try:
                certificate = self._inspect_certificate()
                certificate_state = str(certificate.get("state", "")).lower()
                if (
                    self.options.require_certificate
                    and certificate_state not in self._TRUSTED_STATES
                ):
                    return self._start_failure(
                        "CERTIFICATE_NOT_READY",
                        "capture CA is not trusted; prepare it with explicit consent first",
                        certificate=certificate,
                    )
                config = self._build_config()
                if isinstance(self._capture, SubprocessCaptureSupervisor):
                    if not config.helper_path.is_file():
                        return self._start_failure(
                            "HELPER_NOT_FOUND", "capture helper was not found"
                        )
                    if not config.addon_path.is_file():
                        return self._start_failure("ADDON_NOT_FOUND", "capture addon was not found")
                original = self._proxy.snapshot()
                self._capture.start(config, self._handle_frame, self._handle_capture_status)
                self._config = config
                if not self._capture.wait_ready(config.startup_timeout):
                    return self._start_failure(
                        "HELPER_NOT_READY",
                        "capture helper did not expose its listener",
                    )
                applied = self._proxy.apply_local(config.listen_host, config.listen_port)
                if not isinstance(applied, ProxySnapshot):
                    raise RuntimeError("proxy adapter returned an invalid snapshot")
                self._original_proxy = original
                self._applied_proxy = applied
                self._state = "running"
                result = {
                    "ok": True,
                    "state": "running",
                    "mode": config.mode.value,
                    "listen_host": config.listen_host,
                    "listen_port": config.listen_port,
                }
                self._emit_status(result)
                return result
            except CaptureStartError as exc:
                return self._start_failure("HELPER_START_FAILED", str(exc))
            except Exception as exc:
                return self._start_failure(
                    "START_FAILED",
                    f"capture start failed: {type(exc).__name__}",
                )

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._closed and not self.running:
                return {"ok": True, "state": "closed", "message": "runtime service is closed"}
            proxy_ok = True
            proxy_error = ""
            if self._original_proxy is not None:
                try:
                    proxy_ok = bool(
                        self._proxy.restore(
                            self._original_proxy, expected=self._applied_proxy
                        )
                    )
                    if not proxy_ok:
                        proxy_error = (
                            "proxy settings changed outside Guance; current settings "
                            "were preserved"
                        )
                except Exception as exc:
                    proxy_ok = False
                    proxy_error = f"proxy restore failed: {type(exc).__name__}"
                if not proxy_ok:
                    self._record("error", "proxy_restore_failed", proxy_error)
            capture_error = ""
            try:
                if self.running:
                    self._capture.stop(self.options.stop_timeout)
            except Exception as exc:
                capture_error = f"capture stop failed: {type(exc).__name__}"
                self._record("error", "capture_stop_failed", capture_error)
            if proxy_ok:
                self._original_proxy = None
                self._applied_proxy = None
            self._config = None
            self._state = (
                "closed"
                if self._closed
                else ("error" if not proxy_ok or capture_error else "idle")
            )
            ok = proxy_ok and not capture_error
            result: dict[str, Any] = {
                "ok": ok,
                "state": "stopped" if ok else "stop_error",
                "runtime_state": self._state,
            }
            if proxy_error:
                result["error_code"] = "PROXY_RESTORE_FAILED"
                result["message"] = proxy_error
            if capture_error:
                result["error_code"] = "CAPTURE_STOP_FAILED"
                result["message"] = capture_error
            self._emit_status(result)
            return result

    def repair(self) -> dict[str, Any]:
        with self._lock:
            if self.running or self._state == "running":
                return self._error("RUNNING", "stop capture before repair")
            restored = True
            message = "no stale runtime proxy state was found"
            if self._original_proxy is not None:
                try:
                    restored = bool(
                        self._proxy.restore(
                            self._original_proxy, expected=self._applied_proxy
                        )
                    )
                    message = (
                        "stale runtime proxy state restored"
                        if restored
                        else "proxy changed outside Guance; it was not overwritten"
                    )
                except Exception as exc:
                    restored = False
                    message = f"proxy repair failed: {type(exc).__name__}"
            self._original_proxy = None
            self._applied_proxy = None
            result = {
                "ok": restored,
                "state": "repaired" if restored else "repair_error",
                "certificate": self._inspect_certificate(),
                "helper": {
                    "path": self._helper_path().name,
                    "exists": self._helper_path().is_file(),
                },
                "message": message,
            }
            if not restored:
                result["error_code"] = "PROXY_REPAIR_FAILED"
            self._emit_status(result)
            return redact_value(result)

    def remove_certificate(self, consent: bool, owner_hwnd: int = 0) -> dict[str, Any]:
        with self._lock:
            if self.running or self._state == "running":
                return self._error("RUNNING", "stop capture before removing the certificate")
            if self._closed:
                return self._error("CLOSED", "runtime service is closed")
            try:
                result = self._certificate.remove(bool(consent), int(owner_hwnd))
            except Exception as exc:
                return self._error(
                    "CERTIFICATE_OPERATION",
                    f"certificate removal failed: {type(exc).__name__}",
                )
            safe = self._safe_mapping(result)
            self._record_result("certificate_remove", safe)
            self._emit_status({"state": "certificate", **safe})
            return safe

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self.stop()
            try:
                self._capture.close() if hasattr(self._capture, "close") else None
            except Exception as exc:
                self._record("error", "capture_close_failed", type(exc).__name__)
            self._closed = True
            self._state = "closed"
            self._emit_status({"state": "closed"})

    def _build_config(self) -> CaptureConfig:
        helper_path = self.options.helper_path or (
            self.paths.runtime_dir / "mitmproxy" / "mitmdump.exe"
        )
        addon_path = self.options.addon_path or self._default_addon_path()
        confdir = self.paths.config_dir / "mitmproxy"
        return CaptureConfig(
            helper_path=helper_path,
            addon_path=addon_path,
            confdir=confdir,
            listen_host=self.options.listen_host,
            listen_port=self.options.listen_port,
            mode=self.options.mode,
            upstream=self.options.upstream,
            allowed_hosts=self.options.allowed_hosts,
            max_frame_bytes=self.options.max_frame_bytes,
            startup_timeout=self.options.startup_timeout,
            stop_timeout=self.options.stop_timeout,
        )

    def _default_addon_path(self) -> Path:
        bundled = self.paths.runtime_dir / "mitmproxy" / "addon.py"
        if bundled.is_file():
            return bundled
        return Path(__file__).resolve().parents[2] / "infrastructure" / "capture" / "addon.py"

    def _helper_path(self) -> Path:
        return self.options.helper_path or (self.paths.runtime_dir / "mitmproxy" / "mitmdump.exe")

    def _inspect_certificate(self) -> dict[str, Any]:
        try:
            status = self._certificate.inspect()
            if isinstance(status, CertificateStatus):
                return redact_value(status.to_dict())
            if isinstance(status, Mapping):
                return redact_value({str(k): status[k] for k in list(status)[:16]})
            return {"state": "error", "message": "certificate adapter returned an invalid status"}
        except Exception as exc:
            self._record("warning", "certificate_inspect_failed", type(exc).__name__)
            return {
                "state": "error",
                "message": f"certificate inspection failed: {type(exc).__name__}",
            }

    def _upstream_summary(self) -> dict[str, Any] | None:
        upstream = self.options.upstream
        if upstream is None:
            return None
        return {
            "kind": upstream.kind,
            "scheme": upstream.scheme,
            "host": upstream.host,
            "port": upstream.port,
        }

    def _handle_frame(self, frame: SourceFrame) -> None:
        try:
            self._on_frame(frame)
        except Exception as exc:
            self._record("error", "frame_callback_failed", type(exc).__name__)
            self._emit_status({"state": "frame_callback_error", "error": type(exc).__name__})

    def _handle_capture_status(self, status: dict[str, Any]) -> None:
        safe = redact_value({str(k): status[k] for k in list(status)[:32]})
        if not isinstance(safe, dict):
            return
        state = str(safe.get("state", "capture_status"))
        self._record(
            "info" if state not in {"stop_error", "frame_dropped"} else "warning",
            f"capture_{state}",
            "capture helper status",
            safe,
        )
        self._emit_status(safe)

    def _start_failure(self, code: str, message: str, **fields: Any) -> dict[str, Any]:
        # If the proxy was switched before a later step failed, restore it
        # using the expected applied value.  Stopping the helper is always the
        # final cleanup step.
        proxy_ok = True
        if self._original_proxy is not None and self._applied_proxy is not None:
            try:
                proxy_ok = bool(
                    self._proxy.restore(
                        self._original_proxy, expected=self._applied_proxy
                    )
                )
            except Exception:
                proxy_ok = False
        try:
            if self.running:
                self._capture.stop(self.options.stop_timeout)
        except Exception:
            pass
        if proxy_ok:
            self._original_proxy = None
            self._applied_proxy = None
        self._config = None
        self._state = "error"
        result: dict[str, Any] = {
            "ok": False,
            "state": "error",
            "error_code": code,
            "message": message,
            **fields,
        }
        if not proxy_ok:
            result["proxy_cleanup"] = "failed"
        self._last_error = redact_value(result)
        self._emit_status(result)
        return redact_value(result)

    def _error(self, code: str, message: str) -> dict[str, Any]:
        result = {"ok": False, "state": self._state, "error_code": code, "message": message}
        self._record("error", code.lower(), message)
        self._last_error = result
        self._emit_status(
            {
                "state": "error",
                "runtime_state": self._state,
                **{key: value for key, value in result.items() if key != "state"},
            }
        )
        return result

    def _record_result(self, code: str, result: Mapping[str, Any]) -> None:
        self._record(
            "info" if result.get("ok") else "warning",
            code,
            str(result.get("message", "")),
            result,
        )

    def _record(
        self, level: str, code: str, message: str, data: Mapping[str, Any] | None = None
    ) -> None:
        self._diagnostics.add(level, code, redact_text(message), data)

    def _safe_mapping(self, value: Any) -> dict[str, Any]:
        if isinstance(value, Mapping):
            safe = redact_value({str(k): value[k] for k in list(value)[:32]})
            return safe if isinstance(safe, dict) else {"ok": False, "state": "invalid"}
        return {"ok": False, "state": "invalid", "message": "adapter returned an invalid result"}

    def _emit_status(self, status: Mapping[str, Any]) -> None:
        safe = redact_value({str(k): status[k] for k in list(status)[:32]})
        if not isinstance(safe, dict):
            return
        try:
            self._on_status(safe)
        except Exception:
            self._record("warning", "status_callback_failed", "status callback raised")


__all__ = ["RuntimeOptions", "RuntimeService"]
