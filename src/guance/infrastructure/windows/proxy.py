"""Windows current-user proxy adapter.

Only this module knows how to talk to the Internet Settings registry.  The
application service consumes :class:`~guance.ports.capture.ProxyPort`, making
proxy lifecycle tests entirely fakeable on non-Windows hosts.
"""
from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from typing import Any, Protocol

from guance.ports.capture import PlatformUnavailableError, ProxySnapshot


def build_local_proxy_snapshot(current: ProxySnapshot, host: str, port: int) -> ProxySnapshot:
    """Build the exact local proxy snapshot without touching the OS."""
    if not host or any(char in str(host) for char in "\r\n"):
        raise ValueError("local proxy host must be non-empty and contain no newlines")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("local proxy port must be between 1 and 65535")
    formatted_host = str(host).strip()
    if ":" in formatted_host and not formatted_host.startswith("["):
        formatted_host = f"[{formatted_host}]"
    return ProxySnapshot(
        enabled=True,
        server=f"{formatted_host}:{port}",
        auto_config_url=current.auto_config_url,
        auto_detect=current.auto_detect,
        bypass=current.bypass,
    )


class RegistryAccessor(Protocol):
    def read_proxy(self) -> ProxySnapshot:
        ...

    def write_proxy(self, snapshot: ProxySnapshot) -> None:
        ...

    def refresh(self) -> None:
        ...


class WindowsProxyBackend:
    """Read and update the current user's Internet Settings proxy.

    Construction and import are safe on non-Windows.  A method raises
    ``PlatformUnavailableError`` there unless a registry accessor is injected.
    No method is called by importing Guance; the runtime only invokes it after
    a user starts a capture session.
    """

    _SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

    def __init__(self, *, registry: RegistryAccessor | None = None, refresh: bool = True) -> None:
        self._registry = registry
        self._refresh_enabled = refresh

    @property
    def available(self) -> bool:
        return self._registry is not None or os.name == "nt"

    def snapshot(self) -> ProxySnapshot:
        accessor = self._accessor()
        return accessor.read_proxy()

    def apply_local(self, host: str, port: int) -> ProxySnapshot:
        current = self.snapshot()
        target = build_local_proxy_snapshot(current, host, port)
        accessor = self._accessor()
        accessor.write_proxy(target)
        if self._refresh_enabled:
            accessor.refresh()
        return target

    def restore(self, snapshot: ProxySnapshot, expected: ProxySnapshot | None = None) -> bool:
        accessor = self._accessor()
        current = accessor.read_proxy()
        if expected is not None and current != expected:
            return False
        accessor.write_proxy(snapshot)
        if self._refresh_enabled:
            accessor.refresh()
        return True

    def _accessor(self) -> RegistryAccessor:
        if self._registry is not None:
            return self._registry
        if os.name != "nt":
            raise PlatformUnavailableError(
                "Windows proxy settings are unavailable on this platform"
            )
        return _WinregAccessor(refresh_enabled=self._refresh_enabled)

    @staticmethod
    def _format_host(host: str) -> str:
        host = str(host).strip()
        if ":" in host and not host.startswith("["):
            return f"[{host}]"
        return host


class _WinregAccessor:
    def __init__(self, *, refresh_enabled: bool = True) -> None:
        self._refresh_enabled = refresh_enabled

    def read_proxy(self) -> ProxySnapshot:
        import winreg  # type: ignore[import-not-found]  # pragma: no cover - Windows only

        values: dict[str, Any] = {}
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WindowsProxyBackend._SUBKEY) as key:
            for name, default in (
                ("ProxyEnable", 0),
                ("ProxyServer", ""),
                ("AutoConfigURL", ""),
                ("AutoDetect", 0),
                ("ProxyOverride", ""),
            ):
                try:
                    values[name] = winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError:
                    values[name] = default
        return ProxySnapshot(
            enabled=bool(values["ProxyEnable"]),
            server=str(values["ProxyServer"] or ""),
            auto_config_url=str(values["AutoConfigURL"] or ""),
            auto_detect=bool(values["AutoDetect"]),
            bypass=str(values["ProxyOverride"] or ""),
        )

    def write_proxy(self, snapshot: ProxySnapshot) -> None:
        import winreg  # type: ignore[import-not-found]  # pragma: no cover - Windows only

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            WindowsProxyBackend._SUBKEY,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, int(snapshot.enabled))
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, snapshot.server)
            winreg.SetValueEx(key, "AutoConfigURL", 0, winreg.REG_SZ, snapshot.auto_config_url)
            winreg.SetValueEx(key, "AutoDetect", 0, winreg.REG_DWORD, int(snapshot.auto_detect))
            winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, snapshot.bypass)

    def refresh(self) -> None:
        if not self._refresh_enabled:
            return
        # INTERNET_OPTION_SETTINGS_CHANGED=39 and SETTINGS_REFRESH=37.
        wininet = ctypes.windll.Wininet  # type: ignore[attr-defined]  # pragma: no cover
        if not wininet.InternetSetOptionW(None, 39, None, 0):
            raise OSError("Internet Settings did not accept the proxy change")
        if not wininet.InternetSetOptionW(None, 37, None, 0):
            raise OSError("Internet Settings did not refresh the proxy change")


@dataclass
class InMemoryProxyBackend:
    """Small deterministic fake useful for runtime tests and examples."""

    current: ProxySnapshot
    writes: int = 0

    def snapshot(self) -> ProxySnapshot:
        return self.current

    def apply_local(self, host: str, port: int) -> ProxySnapshot:
        self.current = ProxySnapshot(
            enabled=True,
            server=f"{WindowsProxyBackend._format_host(host)}:{port}",
            auto_config_url=self.current.auto_config_url,
            auto_detect=self.current.auto_detect,
            bypass=self.current.bypass,
        )
        self.writes += 1
        return self.current

    def restore(self, snapshot: ProxySnapshot, expected: ProxySnapshot | None = None) -> bool:
        if expected is not None and self.current != expected:
            return False
        self.current = snapshot
        self.writes += 1
        return True


__all__ = [
    "InMemoryProxyBackend",
    "RegistryAccessor",
    "WindowsProxyBackend",
    "build_local_proxy_snapshot",
]
