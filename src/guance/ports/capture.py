"""Qt-free ports and value objects for the runtime/capture subsystem.

The runtime is deliberately split at these interfaces.  The application layer
can therefore be tested on a non-Windows host with small fakes while the
Windows adapters remain responsible for all operating-system side effects.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from guance.contracts import SourceFrame


class CaptureMode(StrEnum):
    """How the local capture helper reaches the game servers.

    ``DIRECT`` means that the helper connects directly.  The two upstream
    modes are intentionally separate so a configuration cannot silently infer
    an upstream from an arbitrary URL or environment variable.
    """

    DIRECT = "direct"
    UPSTREAM_HTTP = "upstream_http"
    UPSTREAM_MIXED = "upstream_mixed"
    AUTO = "auto"  # retained for config compatibility; runtime rejects it


@dataclass(frozen=True, slots=True)
class UpstreamProxy:
    """An explicitly configured HTTP-compatible upstream proxy.

    A Clash ``mixed-port`` is consumed as HTTP CONNECT by mitmproxy, so it is
    represented by ``kind='mixed'`` while retaining an ``http://`` endpoint.
    Credentials are intentionally not part of this value object.
    """

    host: str
    port: int
    kind: str = "http"
    scheme: str = "http"

    def __post_init__(self) -> None:
        host = self.host.strip()
        if not host:
            raise ValueError("upstream proxy host must not be empty")
        if any(char in host for char in "\\/\r\n @") or "://" in host:
            raise ValueError("upstream proxy host must be a hostname or IP without credentials")
        if (
            not isinstance(self.port, int)
            or isinstance(self.port, bool)
            or not 1 <= self.port <= 65535
        ):
            raise ValueError("upstream proxy port must be between 1 and 65535")
        if self.kind not in {"http", "mixed"}:
            raise ValueError("upstream proxy kind must be 'http' or 'mixed'")
        if self.scheme not in {"http", "https"}:
            raise ValueError("upstream proxy scheme must be http or https")
        object.__setattr__(self, "host", host)

    @property
    def url(self) -> str:
        host = self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{self.scheme}://{host}:{self.port}"


@dataclass(frozen=True, slots=True)
class CaptureConfig:
    """Validated configuration passed to the capture process."""

    helper_path: Path
    addon_path: Path
    confdir: Path
    listen_host: str = "127.0.0.1"
    listen_port: int = 18080
    mode: CaptureMode = CaptureMode.DIRECT
    upstream: UpstreamProxy | None = None
    allowed_hosts: tuple[str, ...] = ()
    max_frame_bytes: int = 1_048_576
    startup_timeout: float = 8.0
    stop_timeout: float = 3.0

    def __post_init__(self) -> None:
        try:
            mode = self.mode if isinstance(self.mode, CaptureMode) else CaptureMode(self.mode)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unsupported capture mode: {self.mode!r}") from exc
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "helper_path", Path(self.helper_path))
        object.__setattr__(self, "addon_path", Path(self.addon_path))
        object.__setattr__(self, "confdir", Path(self.confdir))
        if not self.listen_host.strip() or any(char in self.listen_host for char in "\r\n"):
            raise ValueError("listen_host must be a non-empty host without newlines")
        if not isinstance(self.listen_port, int) or not 1 <= self.listen_port <= 65535:
            raise ValueError("listen_port must be between 1 and 65535")
        if self.max_frame_bytes < 256:
            raise ValueError("max_frame_bytes must be at least 256")
        if self.startup_timeout <= 0 or self.stop_timeout <= 0:
            raise ValueError("capture timeouts must be positive")
        if self.mode is CaptureMode.AUTO:
            raise ValueError(
                "capture mode 'auto' is not supported; choose direct, upstream_http, "
                "or upstream_mixed"
            )
        if self.mode in {CaptureMode.UPSTREAM_HTTP, CaptureMode.UPSTREAM_MIXED}:
            if self.upstream is None:
                raise ValueError("an upstream proxy is required for upstream capture modes")
            expected = "http" if self.mode is CaptureMode.UPSTREAM_HTTP else "mixed"
            if self.upstream.kind != expected:
                raise ValueError(
                    f"capture mode {self.mode} requires an upstream kind of {expected}"
                )
        elif self.upstream is not None:
            raise ValueError("upstream must be omitted in direct capture mode")
        cleaned_hosts = tuple(
            str(host).strip().lower()
            for host in self.allowed_hosts
            if str(host).strip()
        )
        if any(any(char in host for char in "\r\n") for host in cleaned_hosts):
            raise ValueError("allowed hosts must not contain newlines")
        object.__setattr__(self, "allowed_hosts", cleaned_hosts)


@dataclass(frozen=True, slots=True)
class ProxySnapshot:
    """Current-user proxy settings needed for exact restoration."""

    enabled: bool
    server: str = ""
    auto_config_url: str = ""
    auto_detect: bool = False
    bypass: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "server": self.server,
            "auto_config_url": self.auto_config_url,
            "auto_detect": self.auto_detect,
            "bypass": self.bypass,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProxySnapshot":
        return cls(
            enabled=bool(value.get("enabled", False)),
            server=str(value.get("server", "")),
            auto_config_url=str(value.get("auto_config_url", "")),
            auto_detect=bool(value.get("auto_detect", False)),
            bypass=str(value.get("bypass", "")),
        )


@dataclass(frozen=True, slots=True)
class CertificateStatus:
    """A non-secret summary of the capture CA trust state."""

    state: str
    fingerprint: str = ""
    path: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "state": self.state,
            "fingerprint": self.fingerprint,
            "path": self.path,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class CertificateConsentRequest:
    """Data a UI can show before a certificate operation is attempted."""

    purpose: str
    subject: str
    fingerprint: str
    store_scope: str = "CurrentUser\\Root"
    owner_hwnd: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "subject": self.subject,
            "fingerprint": self.fingerprint,
            "store_scope": self.store_scope,
            "owner_hwnd": self.owner_hwnd,
        }


FrameHandler = Callable[[SourceFrame], None]
StatusHandler = Callable[[dict[str, Any]], None]


class CaptureProcessPort(Protocol):
    """Process boundary for mitmdump or a fake capture helper."""

    def start(
        self, config: CaptureConfig, on_frame: FrameHandler, on_status: StatusHandler
    ) -> None:
        ...

    def wait_ready(self, timeout: float) -> bool:
        ...

    def stop(self, timeout: float) -> None:
        ...

    @property
    def running(self) -> bool:
        ...


class ProxyPort(Protocol):
    """Current-user proxy snapshot/apply/restore boundary."""

    def snapshot(self) -> ProxySnapshot:
        ...

    def apply_local(self, host: str, port: int) -> ProxySnapshot:
        """Apply local proxy settings and return the resulting settings."""
        ...

    def pac_snapshot(self) -> ProxySnapshot:
        """Read the active per-connection proxy options."""
        ...

    def set_pac(self, url: str, *, expected: ProxySnapshot) -> bool:
        """Change the PAC URL only while the inspected settings still match."""
        ...

    def wpad_available(self) -> bool:
        """Report an automatically discovered PAC."""
        ...

    def restore(self, snapshot: ProxySnapshot, expected: ProxySnapshot | None = None) -> bool:
        """Restore only if ``expected`` still matches, when supplied."""
        ...


class CertificatePort(Protocol):
    """Certificate consent/trust boundary.

    Implementations must treat ``consent=False`` as a hard no-op.  The runtime
    never imports a CA or removes one without an explicit user decision.
    """

    def inspect(self) -> CertificateStatus:
        ...

    def prepare(self, consent: bool, owner_hwnd: int = 0) -> dict[str, Any]:
        ...

    def remove(self, consent: bool, owner_hwnd: int = 0) -> dict[str, Any]:
        ...


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """Typed runtime paths with one canonical mitmproxy certificate directory.

    The mitmproxy helper uses ``config_dir / "mitmproxy"`` as its confdir and
    generates its CA there.  ``cert_dir`` is retained in the public shape for
    compatibility with older app-path mappings, but is normalized to that same
    directory so certificate inspection can never look in a stale data/certs
    location.
    """

    data_dir: Path
    runtime_dir: Path
    config_dir: Path
    logs_dir: Path
    cert_dir: Path

    def __post_init__(self) -> None:
        for name in ("data_dir", "runtime_dir", "config_dir", "logs_dir", "cert_dir"):
            object.__setattr__(self, name, Path(getattr(self, name)))
        object.__setattr__(self, "cert_dir", self.config_dir / "mitmproxy")

    @classmethod
    def from_mapping(cls, paths: Mapping[str, Path | str]) -> "RuntimePaths":
        required = ("data_dir", "runtime_dir", "config_dir", "logs_dir")
        missing = [name for name in required if name not in paths]
        if missing:
            raise ValueError(f"missing runtime paths: {', '.join(missing)}")
        values = {name: Path(paths[name]) for name in required}
        values["cert_dir"] = Path(paths.get("cert_dir", values["config_dir"] / "mitmproxy"))
        return cls(**values)

    def as_dict(self) -> dict[str, Path]:
        return {
            "data_dir": self.data_dir,
            "runtime_dir": self.runtime_dir,
            "config_dir": self.config_dir,
            "logs_dir": self.logs_dir,
            "cert_dir": self.cert_dir,
        }


class RuntimeConfigError(ValueError):
    """Raised when runtime/capture configuration is invalid."""


class PlatformUnavailableError(RuntimeError):
    """Raised when a Windows side effect is requested on another platform."""


class ProxyStateConflictError(RuntimeError):
    """Raised by an adapter when current proxy settings changed unexpectedly."""


__all__ = [
    "CaptureConfig",
    "CaptureMode",
    "CaptureProcessPort",
    "CertificateConsentRequest",
    "CertificatePort",
    "CertificateStatus",
    "FrameHandler",
    "PlatformUnavailableError",
    "ProxyPort",
    "ProxySnapshot",
    "ProxyStateConflictError",
    "RuntimeConfigError",
    "RuntimePaths",
    "StatusHandler",
    "UpstreamProxy",
]
