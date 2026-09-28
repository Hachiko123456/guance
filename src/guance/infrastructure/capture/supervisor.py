"""Supervise the bundled mitmdump process and decode its bounded line protocol."""
from __future__ import annotations

import base64
import binascii
import json
import math
import os
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from guance.contracts import SourceFrame
from guance.ports.capture import CaptureConfig, FrameHandler, StatusHandler

from .command import build_mitm_command
from .diagnostics import redact_text, redact_value

FRAME_PREFIX = "GUANCE_FRAME "
DIAG_PREFIX = "GUANCE_DIAG "
_MAX_LINE_BYTES = 2 * 1024 * 1024


class CaptureStartError(RuntimeError):
    """Raised when the capture helper cannot be started or made ready."""


class SubprocessCaptureSupervisor:
    """Run a mitmdump helper with no shell and supervise stdout/stderr.

    The helper's stdout is treated as an untrusted transport.  Only lines with
    the explicit ``GUANCE_FRAME`` prefix are decoded into ``SourceFrame``
    objects; all other output is bounded and reduced to diagnostics.
    """

    def __init__(
        self,
        *,
        process_factory: Any = subprocess.Popen,
        readiness_probe: Callable[[CaptureConfig, float], bool] | None = None,
        max_line_bytes: int = _MAX_LINE_BYTES,
    ) -> None:
        if max_line_bytes < 1024:
            raise ValueError("max_line_bytes must be at least 1024")
        self._process_factory = process_factory
        self._readiness_probe = readiness_probe
        self._max_line_bytes = max_line_bytes
        self._process: Any | None = None
        self._config: CaptureConfig | None = None
        self._on_frame: FrameHandler | None = None
        self._on_status: StatusHandler | None = None
        self._lock = threading.RLock()
        self._reader_threads: list[threading.Thread] = []
        self._watch_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @property
    def running(self) -> bool:
        with self._lock:
            process = self._process
        if process is None:
            return False
        try:
            return process.poll() is None
        except (AttributeError, OSError):
            return False

    def start(
        self, config: CaptureConfig, on_frame: FrameHandler, on_status: StatusHandler
    ) -> None:
        with self._lock:
            if self.running:
                raise CaptureStartError("capture helper is already running")
            if not Path(config.helper_path).is_file():
                raise CaptureStartError(f"capture helper not found: {config.helper_path}")
            if not Path(config.addon_path).is_file():
                raise CaptureStartError(f"capture addon not found: {config.addon_path}")
            try:
                config.confdir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise CaptureStartError(
                    f"unable to prepare capture directory: {redact_text(str(exc))}"
                ) from exc
            env = os.environ.copy()
            env["GUANCE_ALLOWED_HOSTS"] = ",".join(config.allowed_hosts)
            env["GUANCE_MAX_FRAME_BYTES"] = str(config.max_frame_bytes)
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            try:
                process = self._process_factory(
                    build_mitm_command(config),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    shell=False,
                    stderr=subprocess.PIPE,
                    cwd=os.fspath(config.confdir),
                    env=env,
                    creationflags=flags,
                )
            except (OSError, ValueError) as exc:
                raise CaptureStartError(
                    f"unable to start capture helper: {redact_text(str(exc))}"
                ) from exc
            self._process = process
            self._config = config
            self._on_frame = on_frame
            self._on_status = on_status
            self._stop_event.clear()
            self._reader_threads = []
            streams = (
                (getattr(process, "stdout", None), "stdout"),
                (getattr(process, "stderr", None), "stderr"),
            )
            for stream, name in streams:
                if stream is None:
                    continue
                thread = threading.Thread(
                    target=self._read_stream,
                    args=(stream, name),
                    name=f"guance-capture-{name}",
                    daemon=True,
                )
                self._reader_threads.append(thread)
                thread.start()
            self._watch_thread = threading.Thread(
                target=self._watch_process,
                args=(process,),
                name="guance-capture-watch",
                daemon=True,
            )
            self._watch_thread.start()
            self._emit_status({"state": "started", "pid": getattr(process, "pid", 0)})

    def wait_ready(self, timeout: float) -> bool:
        config = self._config
        if config is None or timeout <= 0:
            return False
        if self._readiness_probe is not None:
            try:
                return bool(self._readiness_probe(config, timeout)) and self.running
            except Exception as exc:
                self._emit_status({"state": "readiness_error", "error": redact_text(str(exc))})
                return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.running:
                return False
            try:
                with socket.create_connection(
                    (config.listen_host, config.listen_port), timeout=0.15
                ):
                    return True
            except OSError:
                time.sleep(0.05)
        return False

    def stop(self, timeout: float) -> None:
        if timeout <= 0:
            timeout = 0.1
        with self._lock:
            process = self._process
            if process is None:
                return
            self._stop_event.set()
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=timeout)
        except (OSError, subprocess.TimeoutExpired, AttributeError) as exc:
            self._emit_status({"state": "stop_error", "error": redact_text(str(exc))})
        finally:
            with self._lock:
                if self._process is process:
                    self._process = None
                    self._config = None
                    self._on_frame = None
                    self._on_status = None
            for thread in self._reader_threads:
                thread.join(timeout=0.5)
            self._reader_threads = []
            watch = self._watch_thread
            if watch is not None and watch is not threading.current_thread():
                watch.join(timeout=0.5)
            self._watch_thread = None

    def close(self) -> None:
        self.stop(3.0)

    def _watch_process(self, process: Any) -> None:
        try:
            while not self._stop_event.is_set():
                if process.poll() is not None:
                    self._emit_status({"state": "helper_exit", "returncode": process.poll()})
                    return
                time.sleep(0.1)
        except Exception as exc:
            self._emit_status({"state": "watch_error", "error": redact_text(str(exc))})

    def _read_stream(self, stream: Any, name: str) -> None:
        while not self._stop_event.is_set():
            try:
                line = stream.readline(self._max_line_bytes + 1)
            except (OSError, ValueError, TypeError) as exc:
                self._emit_status(
                    {"state": "stream_error", "stream": name, "error": redact_text(str(exc))}
                )
                return
            if not line:
                return
            if isinstance(line, bytes):
                line_size = len(line)
                has_newline = line.endswith((b"\n", b"\r"))
                if line_size > self._max_line_bytes:
                    self._emit_status(
                        {"state": "line_dropped", "stream": name, "reason": "too_large"}
                    )
                    if not has_newline:
                        self._discard_line_remainder(stream)
                    continue
                line = line.decode("utf-8", errors="replace")
            else:
                line_size = len(str(line).encode("utf-8", errors="replace"))
                has_newline = str(line).endswith(("\n", "\r"))
                if line_size > self._max_line_bytes:
                    self._emit_status(
                        {"state": "line_dropped", "stream": name, "reason": "too_large"}
                    )
                    if not has_newline:
                        self._discard_line_remainder(stream)
                    continue
            line = str(line).rstrip("\r\n")
            if line.startswith(FRAME_PREFIX):
                self._handle_frame_line(line[len(FRAME_PREFIX) :])
            elif line.startswith(DIAG_PREFIX):
                self._handle_diag_line(line[len(DIAG_PREFIX) :])
            elif line.strip():
                self._emit_status(
                    {"state": "helper_output", "stream": name, "message": redact_text(line)}
                )

    def _discard_line_remainder(self, stream: Any) -> None:
        """Drain an oversized physical line without forwarding its contents."""
        while not self._stop_event.is_set():
            try:
                chunk = stream.readline(self._max_line_bytes + 1)
            except (OSError, ValueError, TypeError):
                return
            has_newline = (
                chunk.endswith(b"\n")
                if isinstance(chunk, bytes)
                else str(chunk).endswith("\n")
            )
            if not chunk or has_newline:
                return

    def _handle_diag_line(self, content: str) -> None:
        try:
            value = json.loads(content)
        except json.JSONDecodeError:
            self._emit_status({"state": "helper_diag", "message": redact_text(content)})
            return
        if isinstance(value, dict):
            safe = redact_value({str(key): value[key] for key in list(value)[:16]})
            if isinstance(safe, dict):
                safe.pop("state", None)
            self._emit_status({"state": "helper_diag", **(safe if isinstance(safe, dict) else {})})
        else:
            self._emit_status({"state": "helper_diag", "message": redact_text(repr(value))})

    def _handle_frame_line(self, content: str) -> None:
        try:
            value = json.loads(content)
            if not isinstance(value, dict):
                raise ValueError("frame record is not an object")
            payload_value = value.get("payload_b64", "")
            if not isinstance(payload_value, str):
                raise ValueError("payload_b64 is not a string")
            payload = base64.b64decode(payload_value, validate=True)
            config = self._config
            if config is None or len(payload) > config.max_frame_bytes:
                raise ValueError("frame exceeds configured limit")
            received_at = float(value.get("received_at", time.time()))
            if not math.isfinite(received_at):
                raise ValueError("received_at must be finite")
            sequence = int(value.get("sequence", 0))
            if sequence < 0:
                raise ValueError("sequence must not be negative")
            frame = SourceFrame(
                host=redact_text(str(value.get("host", "")), 255),
                payload=payload,
                received_at=received_at,
                connection_id=redact_text(str(value.get("connection_id", "")), 96),
                sequence=sequence,
            )
        except (
            ValueError,
            TypeError,
            KeyError,
            json.JSONDecodeError,
            binascii.Error,
            OverflowError,
        ) as exc:
            self._emit_status({"state": "frame_dropped", "reason": redact_text(str(exc))})
            return
        callback = self._on_frame
        if callback is None:
            return
        try:
            callback(frame)
        except Exception as exc:  # callbacks must never kill the reader thread
            self._emit_status({"state": "frame_callback_error", "error": redact_text(str(exc))})

    def _emit_status(self, value: dict[str, Any]) -> None:
        callback = self._on_status
        if callback is None:
            return
        try:
            safe = redact_value({str(key): value[key] for key in list(value)[:32]})
            if isinstance(safe, dict):
                callback(safe)
        except Exception:
            return


__all__ = ["CaptureStartError", "SubprocessCaptureSupervisor", "FRAME_PREFIX", "DIAG_PREFIX"]
