"""Application orchestration for capture, proxy, and certificate lifecycles."""
from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from guance.contracts import SourceFrame
from guance.infrastructure.capture.diagnostics import BoundedDiagnostics, redact_text, redact_value
from guance.infrastructure.capture.supervisor import CaptureStartError, SubprocessCaptureSupervisor
from guance.infrastructure.windows.certificate import WindowsCertificateManager
from guance.application.runtime.lease import ProxyLease, ProxyLeaseStore, RuntimeInstanceLock
from guance.infrastructure.windows.proxy import WindowsProxyBackend, build_local_proxy_snapshot
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


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return pid == os.getpid()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


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
        self._proxy_switched = False
        self._config: CaptureConfig | None = None
        self._last_error: dict[str, Any] | None = None
        self._lease_store = ProxyLeaseStore(self.paths.config_dir / "proxy-lease.json")
        self._instance_lock = RuntimeInstanceLock(self.paths.config_dir / "guance-runtime.lock")
        self._helper_recovery_scheduled = False
        self._shutting_down = False

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
                    f"证书准备失败：{type(exc).__name__}",
                )
            safe = self._safe_mapping(result)
            self._record_result("certificate_prepare", safe)
            self._emit_status({"state": "certificate", **safe})
            return safe

    def recover_stale_proxy(self) -> dict[str, Any]:
        """Recover a persisted lease only when Guance still owns the proxy."""
        with self._lock:
            if self.running or self._state in {"starting", "running"}:
                return self._error("RUNNING", "stop capture before recovering proxy state")
            acquired_here = False
            if not self._instance_lock.held:
                if not self._instance_lock.acquire():
                    return self._error(
                        "RUNTIME_ALREADY_ACTIVE",
                        "another Guance runtime owns the proxy lease",
                    )
                acquired_here = True
            try:
                return self._recover_stale_proxy_locked()
            finally:
                if acquired_here:
                    self._instance_lock.release()

    def _recover_stale_proxy_locked(self) -> dict[str, Any]:
        try:
            lease = self._lease_store.load()
        except Exception as exc:
            return self._error("PROXY_LEASE_INVALID", f"proxy lease could not be read: {type(exc).__name__}")
        if lease is None:
            result = {"ok": True, "state": "no_stale_lease", "message": "no stale proxy lease was found"}
            self._emit_status(result)
            return result
        if _pid_is_alive(lease.app_pid) and lease.app_pid != os.getpid():
            result = {
                "ok": False,
                "state": "proxy_conflict",
                "error_code": "RUNTIME_ALREADY_ACTIVE",
                "message": "another Guance process still owns the proxy lease",
                "guance_pid": lease.app_pid,
                "helper_pid": lease.helper_pid,
            }
            self._last_error = result
            self._emit_status(result)
            return result
        try:
            current = self._proxy.snapshot()
        except Exception as exc:
            return self._error("PROXY_RECOVERY_FAILED", f"proxy snapshot failed: {type(exc).__name__}")

        # If the process died before applying the local proxy, there is nothing
        # to restore.  The lease is still cleared only when the original state
        # is unchanged; an unrelated change remains a conflict.
        if current == lease.original_proxy:
            self._lease_store.append_event("stale_lease_cleared", lease)
            self._lease_store.clear()
            result = {"ok": True, "state": "stale_lease_cleared", "message": "unapplied proxy lease cleared"}
            self._emit_status(result)
            return result
        # Never overwrite an external proxy change.  Only the exact applied
        # snapshot proves that Guance still owns the settings.
        if current != lease.applied_proxy:
            try:
                self._lease_store.update_state(lease, "conflict")
            except Exception:
                pass
            result = {
                "ok": False,
                "state": "proxy_conflict",
                "error_code": "PROXY_RECOVERY_CONFLICT",
                "message": "proxy settings changed outside Guance; current settings were preserved",
            }
            self._last_error = result
            self._emit_status(result)
            return result
        try:
            lease = self._lease_store.update_state(lease, "restoring")
            restored = bool(self._proxy.restore(lease.original_proxy, expected=lease.applied_proxy))
        except Exception as exc:
            restored = False
            error_type = type(exc).__name__
        else:
            error_type = ""
        if not restored:
            try:
                latest = self._proxy.snapshot()
                state = "conflict" if latest != lease.applied_proxy else "recovery_failed"
                self._lease_store.update_state(lease, state)
            except Exception:
                pass
            result = {
                "ok": False,
                "state": "proxy_conflict" if not error_type else "repair_error",
                "error_code": "PROXY_RECOVERY_CONFLICT" if not error_type else "PROXY_RECOVERY_FAILED",
                "message": "proxy settings changed outside Guance; current settings were preserved"
                if not error_type else f"proxy recovery failed: {error_type}",
            }
            self._last_error = result
            self._emit_status(result)
            return result
        self._lease_store.append_event("stale_lease_restored", lease)
        self._lease_store.clear()
        result = {"ok": True, "state": "repaired", "message": "stale runtime proxy state restored"}
        self._emit_status(result)
        return result

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
            if not self._instance_lock.held and not self._instance_lock.acquire():
                return self._error(
                    "RUNTIME_ALREADY_ACTIVE",
                    "another Guance instance is already using the capture runtime",
                )
            recovery = self._recover_stale_proxy_locked()
            if not recovery.get("ok", False):
                self._instance_lock.release()
                return recovery
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
                target = build_local_proxy_snapshot(original, config.listen_host, config.listen_port)
                self._original_proxy = original
                self._applied_proxy = target
                self._proxy_switched = False
                lease = ProxyLease(
                    original_proxy=original,
                    applied_proxy=target,
                    app_pid=os.getpid(),
                    target_host=config.listen_host,
                    target_port=config.listen_port,
                    state="prepared",
                )
                self._lease_store.save(lease)
                self._lease_store.append_event("prepared", lease)
                self._capture.start(config, self._handle_frame, self._handle_capture_status)
                self._config = config
                helper_pid = getattr(self._capture, "pid", None)
                if isinstance(helper_pid, int) and helper_pid > 0:
                    lease = self._lease_store.update_helper_pid(lease, helper_pid)
                if not self._capture.wait_ready(config.startup_timeout):
                    return self._start_failure(
                        "HELPER_NOT_READY",
                        "capture helper did not expose its listener",
                    )
                applied = self._proxy.apply_local(config.listen_host, config.listen_port)
                if not isinstance(applied, ProxySnapshot):
                    raise RuntimeError("proxy adapter returned an invalid snapshot")
                self._applied_proxy = applied
                self._proxy_switched = True
                lease = self._lease_store.load()
                if lease is None:
                    raise RuntimeError("proxy lease disappeared during startup")
                active_lease = ProxyLease(
                    original_proxy=original,
                    applied_proxy=applied,
                    app_pid=lease.app_pid,
                    target_host=config.listen_host,
                    target_port=config.listen_port,
                    helper_pid=lease.helper_pid,
                    state="active",
                    created_at=lease.created_at,
                )
                self._lease_store.save(active_lease)
                self._lease_store.append_event("active", active_lease)
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
            self._shutting_down = True
            proxy_ok = True
            proxy_error = ""
            if self._original_proxy is not None:
                try:
                    lease = self._lease_store.load()
                    if lease is not None:
                        self._lease_store.update_state(lease, "restoring")
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
                self._proxy_switched = False
                try:
                    self._lease_store.clear()
                except Exception as exc:
                    proxy_ok = False
                    proxy_error = f"proxy lease cleanup failed: {type(exc).__name__}"
            else:
                try:
                    lease = self._lease_store.load()
                    if lease is not None:
                        self._lease_store.update_state(lease, "conflict")
                except Exception:
                    pass
            self._config = None
            self._shutting_down = False
            self._state = (
                "closed"
                if self._closed
                else ("error" if not proxy_ok or capture_error else "idle")
            )
            # The helper is stopped regardless of proxy ownership.  Release the
            # instance lock after cleanup so a later process can inspect a
            # conflict, but never let it restore settings it does not own.
            self._instance_lock.release()
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
        """Compatibility alias for cross-process stale proxy recovery."""
        return self.recover_stale_proxy()

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
                    f"证书移除失败：{type(exc).__name__}",
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
        confdir = self.paths.cert_dir
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
        if state == "started":
            helper_pid = safe.get("pid")
            if isinstance(helper_pid, int) and helper_pid > 0:
                try:
                    lease = self._lease_store.load()
                    if lease is not None and lease.helper_pid != helper_pid:
                        self._lease_store.update_helper_pid(lease, helper_pid)
                except Exception:
                    self._record("warning", "lease_helper_pid_update_failed", "helper pid could not be persisted")
        self._record(
            "info" if state not in {"stop_error", "frame_dropped"} else "warning",
            f"capture_{state}",
            "capture helper status",
            safe,
        )
        self._emit_status(safe)
        if state in {"helper_exit", "watch_error", "stream_error"}:
            self._schedule_helper_recovery()

    def _schedule_helper_recovery(self) -> None:
        with self._lock:
            if self._shutting_down or self._helper_recovery_scheduled:
                return
            if self._original_proxy is None or not self._proxy_switched:
                return
            self._helper_recovery_scheduled = True
        thread = threading.Thread(target=self._recover_after_helper_failure, name="guance-proxy-recovery", daemon=True)
        thread.start()

    def _recover_after_helper_failure(self) -> None:
        try:
            result = self.stop()
            if result.get("ok"):
                self._emit_status({"state": "helper_recovered", "message": "helper exited; proxy restored"})
        finally:
            with self._lock:
                self._helper_recovery_scheduled = False

    def _start_failure(self, code: str, message: str, **fields: Any) -> dict[str, Any]:
        """Clean up a partially started runtime without overwriting external changes."""
        proxy_ok = True
        if self._original_proxy is not None and self._applied_proxy is not None:
            try:
                current = self._proxy.snapshot()
                if current == self._original_proxy:
                    proxy_ok = True
                elif current == self._applied_proxy:
                    proxy_ok = bool(self._proxy.restore(self._original_proxy, expected=self._applied_proxy))
                else:
                    proxy_ok = False
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
            self._proxy_switched = False
            try:
                lease = self._lease_store.load()
                if lease is not None:
                    self._lease_store.append_event("start_failed_cleaned", lease, error_code=code)
                self._lease_store.clear()
            except Exception:
                pass
        else:
            try:
                lease = self._lease_store.load()
                if lease is not None:
                    self._lease_store.update_state(lease, "conflict")
            except Exception:
                pass
        self._config = None
        self._instance_lock.release()
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
