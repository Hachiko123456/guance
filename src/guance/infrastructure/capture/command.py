"""Command construction for the bundled mitmdump helper."""
from __future__ import annotations

import os
import re
from pathlib import Path

from guance.ports.capture import CaptureConfig, CaptureMode


class CaptureCommandError(ValueError):
    """Raised when a helper command cannot be constructed safely."""


def _safe_argument(value: str, name: str) -> str:
    value = str(value)
    if not value or any(char in value for char in "\r\n"):
        raise CaptureCommandError(f"{name} must be non-empty and must not contain newlines")
    return value


def build_mitm_command(config: CaptureConfig) -> list[str]:
    """Build an argument list without shell interpolation.

    Keeping this as a list is important on Windows: paths and proxy URLs are
    never concatenated into a shell command, so spaces and metacharacters do
    not change execution semantics.
    """
    helper = Path(config.helper_path)
    addon = Path(config.addon_path)
    if not helper.name:
        raise CaptureCommandError("capture helper path is empty")
    if not addon.name:
        raise CaptureCommandError("capture addon path is empty")
    listen_host = _safe_argument(config.listen_host, "listen_host")
    if config.mode is CaptureMode.DIRECT:
        mode_args = ["--mode", "regular"]
    elif config.mode in {CaptureMode.UPSTREAM_HTTP, CaptureMode.UPSTREAM_MIXED}:
        if config.upstream is None:  # defensive: CaptureConfig validates this
            raise CaptureCommandError("upstream configuration is missing")
        mode_args = ["--mode", f"upstream:{config.upstream.url}"]
    else:
        raise CaptureCommandError(f"unresolved capture mode: {config.mode}")

    allowed_hosts = []
    if config.allowed_hosts:
        joined = "|".join(re.escape(host) for host in config.allowed_hosts)
        allowed_hosts = ["--allow-hosts", rf"(?i)^(?:{joined})\.?(:[0-9]+)?$"]
    return [
        os.fspath(helper),
        *mode_args,
        "--listen-host",
        listen_host,
        "--listen-port",
        str(config.listen_port),
        "--set",
        f"confdir={os.fspath(config.confdir)}",
        "--set",
        "block_global=false",
        "--quiet",
        *allowed_hosts,
        "-s",
        os.fspath(addon),
    ]


# A descriptive alias keeps the adapter usable by callers that do not want to
# couple themselves to the mitmproxy product name.
build_capture_command = build_mitm_command


__all__ = ["CaptureCommandError", "build_capture_command", "build_mitm_command"]
