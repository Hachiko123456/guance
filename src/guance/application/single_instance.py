"""Single-instance coordination for the Guance desktop application.

The composition root owns the lifecycle of this object.  The module deliberately
has no dependency on the controller, runtime, proxy, or GUI layers so that a
second process can be rejected before any helper or network side effect starts.

Protocol contract
-----------------
``enter(message)`` starts a per-user local server and returns an
:class:`EnterResult`.  The first process receives ``acquired=True``.  A second
process sends its JSON message to the first process and receives
``acquired=False, forwarded=True``; the caller must exit without starting the
runtime or changing the proxy.  ``leave()`` releases the server.  Messages are
JSON objects and are delivered to the callback supplied to the constructor.
"""
from __future__ import annotations

from dataclasses import dataclass
import ctypes
import json
import os
from typing import Any, Callable, Mapping

from PySide6.QtCore import QCoreApplication, QObject, Slot
from PySide6.QtNetwork import QLocalServer, QLocalSocket


_DEFAULT_SERVER_NAME = "Guance.SingleInstance.v1"
_MAX_MESSAGE_BYTES = 64 * 1024


class SingleInstanceError(RuntimeError):
    """Raised when the single-instance endpoint cannot be acquired."""


@dataclass(frozen=True, slots=True)
class EnterResult:
    """Result of trying to acquire the application instance."""

    acquired: bool
    forwarded: bool = False
    recovered_stale_endpoint: bool = False


MessageHandler = Callable[[dict[str, Any]], None]


class SingleInstance(QObject):
    """Own or forward a named local application instance.

    ``QLocalServer`` is backed by a named pipe on Windows, so an abnormal
    process exit releases the kernel endpoint without leaving a lock file.
    On platforms that use a socket path, a failed forward followed by
    ``removeServer`` repairs a stale endpoint before retrying ``listen``.
    """

    def __init__(
        self,
        name: str = _DEFAULT_SERVER_NAME,
        *,
        on_forward: MessageHandler | None = None,
        connect_timeout_ms: int = 500,
        write_timeout_ms: int = 500,
    ) -> None:
        super().__init__()
        if not name or "\x00" in name:
            raise ValueError("single-instance name must be a non-empty string")
        if connect_timeout_ms <= 0 or write_timeout_ms <= 0:
            raise ValueError("socket timeouts must be positive")
        self.name = name
        self._on_forward = on_forward
        self._connect_timeout_ms = int(connect_timeout_ms)
        self._write_timeout_ms = int(write_timeout_ms)
        self._server: QLocalServer | None = None
        self._owned = False
        self._sockets: set[QLocalSocket] = set()
        self._buffers: dict[QLocalSocket, bytearray] = {}
        self._mutex_handle: int | None = None

    @property
    def owns_instance(self) -> bool:
        """Whether this object currently owns the named instance."""

        return self._owned

    def enter(self, message: Mapping[str, Any] | None = None) -> EnterResult:
        """Acquire the instance or forward ``message`` to its owner.

        The method never starts a helper or changes system proxy state.  The
        composition root must return immediately when ``acquired`` is false.
        """

        if self._owned:
            return EnterResult(acquired=True)
        if self._server is not None:
            raise SingleInstanceError("single-instance entry is already in progress")

        mutex_acquired = self._acquire_mutex()
        if not mutex_acquired:
            # A live owner is authoritative. Never remove its endpoint merely
            # because forwarding failed; a second process must not start.
            return EnterResult(acquired=False, forwarded=self.forward(message))

        server = QLocalServer(self)
        server.newConnection.connect(self._accept_connections)
        if server.listen(self.name):
            self._server = server
            self._owned = True
            return EnterResult(acquired=True)

        # The mutex is ours but a stale QLocalServer endpoint may remain on
        # platforms that use filesystem sockets. Repair it only after ownership
        # of the named mutex has been established.
        QLocalServer.removeServer(self.name)
        if server.listen(self.name):
            self._server = server
            self._owned = True
            return EnterResult(acquired=True, recovered_stale_endpoint=True)

        error = server.errorString() or "unknown local-server error"
        server.deleteLater()
        self._release_mutex()
        raise SingleInstanceError(f"unable to acquire single-instance endpoint '{self.name}': {error}")


    def _mutex_name(self) -> str:
        return f"Local\\{self.name}"

    def _acquire_mutex(self) -> bool:
        if os.name != "nt":
            # QLocalServer provides the process-level guard on POSIX test/dev
            # systems; Windows uses a kernel mutex below for race-free startup.
            return True
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        handle = kernel32.CreateMutexW(None, True, self._mutex_name())
        if not handle:
            raise SingleInstanceError("unable to create single-instance mutex")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        self._mutex_handle = int(handle)
        return True

    def _release_mutex(self) -> None:
        handle = self._mutex_handle
        self._mutex_handle = None
        if handle is None or os.name != "nt":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel32.ReleaseMutex.restype = ctypes.c_bool
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        kernel32.ReleaseMutex(ctypes.c_void_p(handle))
        kernel32.CloseHandle(ctypes.c_void_p(handle))

    def forward(self, message: Mapping[str, Any] | None = None) -> bool:
        """Synchronously forward a JSON object to the owning instance."""

        payload = dict(message or {})
        try:
            encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
                "utf-8"
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("single-instance message must be JSON serializable") from exc
        if len(encoded) > _MAX_MESSAGE_BYTES:
            raise ValueError("single-instance message is too large")

        socket = QLocalSocket()
        try:
            socket.connectToServer(self.name)
            if not socket.waitForConnected(self._connect_timeout_ms):
                return False
            if socket.write(encoded) != len(encoded):
                return False
            socket.flush()
            # This also makes same-process tests deterministic; in production
            # the owner normally has its own GUI event loop running.
            deadline = self._write_timeout_ms
            while deadline > 0:
                QCoreApplication.processEvents()
                if socket.waitForBytesWritten(min(50, deadline)):
                    return True
                deadline -= 50
            return False
        finally:
            socket.disconnectFromServer()
            socket.deleteLater()

    @Slot()
    def _accept_connections(self) -> None:
        if self._server is None:
            return
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                return
            self._sockets.add(socket)
            self._buffers[socket] = bytearray()
            socket.readyRead.connect(lambda s=socket: self._read_socket(s))
            socket.disconnected.connect(lambda s=socket: self._forget_socket(s))

    def _read_socket(self, socket: QLocalSocket) -> None:
        if socket not in self._buffers:
            return
        buffer = self._buffers[socket]
        buffer.extend(bytes(socket.readAll()))
        if len(buffer) > _MAX_MESSAGE_BYTES:
            self._forget_socket(socket)
            return
        while b"\n" in buffer:
            line, _, remainder = buffer.partition(b"\n")
            buffer[:] = remainder
            if not line:
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(message, dict) or self._on_forward is None:
                continue
            self._on_forward(message)

    def _forget_socket(self, socket: QLocalSocket) -> None:
        self._buffers.pop(socket, None)
        self._sockets.discard(socket)
        socket.deleteLater()

    def leave(self) -> None:
        """Release the owned endpoint; safe to call repeatedly."""

        if not self._owned:
            return
        for socket in list(self._sockets):
            socket.disconnectFromServer()
            socket.deleteLater()
        self._sockets.clear()
        self._buffers.clear()
        if self._server is not None:
            self._server.close()
            self._server.deleteLater()
            self._server = None
        QLocalServer.removeServer(self.name)
        self._owned = False
        self._release_mutex()

    def __enter__(self) -> "SingleInstance":
        result = self.enter()
        if not result.acquired:
            raise SingleInstanceError("another Guance instance is already running")
        return self

    def __exit__(self, *_: object) -> None:
        self.leave()

    def __del__(self) -> None:
        # QObject destruction normally closes QLocalServer.  Avoid calling Qt
        # methods during interpreter teardown; leave() is the explicit API.
        self._owned = False
