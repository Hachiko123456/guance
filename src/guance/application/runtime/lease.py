"""Crash-safe persistence and ownership for Guance's temporary proxy takeover."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from guance.ports.capture import ProxySnapshot


LEASE_SCHEMA = "guance-proxy-lease-v2"
_SUPPORTED_SCHEMAS = {"guance-proxy-lease-v1", LEASE_SCHEMA}
_VALID_STATES = {"prepared", "active", "restoring", "conflict", "recovery_failed"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class ProxyLease:
    """Serializable ownership record for a temporary local proxy."""

    original_proxy: ProxySnapshot
    applied_proxy: ProxySnapshot
    app_pid: int
    target_host: str
    target_port: int
    helper_pid: int | None = None
    state: str = "prepared"
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        if not isinstance(self.app_pid, int) or isinstance(self.app_pid, bool) or self.app_pid <= 0:
            raise ValueError("proxy lease app pid is invalid")
        if self.helper_pid is not None and (
            not isinstance(self.helper_pid, int) or isinstance(self.helper_pid, bool) or self.helper_pid <= 0
        ):
            raise ValueError("proxy lease helper pid is invalid")
        if not isinstance(self.target_port, int) or isinstance(self.target_port, bool) or not 1 <= self.target_port <= 65535:
            raise ValueError("proxy lease target port is invalid")
        if not self.target_host or any(char in self.target_host for char in "\r\n"):
            raise ValueError("proxy lease target host is invalid")
        if self.state not in _VALID_STATES:
            raise ValueError(f"proxy lease state is invalid: {self.state}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": LEASE_SCHEMA,
            "original_proxy": self.original_proxy.to_dict(),
            "applied_proxy": self.applied_proxy.to_dict(),
            "guance_pid": self.app_pid,
            "helper_pid": self.helper_pid,
            "target_local_proxy": {"host": self.target_host, "port": self.target_port},
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProxyLease":
        if value.get("schema") not in _SUPPORTED_SCHEMAS:
            raise ValueError("unsupported proxy lease schema")
        target = value.get("target_local_proxy")
        if not isinstance(target, Mapping):
            raise ValueError("proxy lease target is invalid")
        helper_pid = value.get("helper_pid")
        if helper_pid is not None and (
            not isinstance(helper_pid, int) or isinstance(helper_pid, bool) or helper_pid <= 0
        ):
            raise ValueError("proxy lease helper pid is invalid")
        return cls(
            original_proxy=ProxySnapshot.from_dict(_mapping(value, "original_proxy")),
            applied_proxy=ProxySnapshot.from_dict(_mapping(value, "applied_proxy")),
            app_pid=_positive_int(value.get("guance_pid"), "app"),
            target_host=_safe_host(target.get("host", "")),
            target_port=_port(target.get("port")),
            helper_pid=helper_pid,
            state=str(value.get("state", "prepared")),
            created_at=str(value.get("created_at", "")),
            updated_at=str(value.get("updated_at", "")),
        )

    def with_state(self, state: str) -> "ProxyLease":
        return self.with_updates(state=state)

    def with_updates(
        self,
        *,
        state: str | None = None,
        helper_pid: int | None | object = None,
    ) -> "ProxyLease":
        next_helper = self.helper_pid if helper_pid is None else helper_pid
        return ProxyLease(
            original_proxy=self.original_proxy,
            applied_proxy=self.applied_proxy,
            app_pid=self.app_pid,
            target_host=self.target_host,
            target_port=self.target_port,
            helper_pid=next_helper if isinstance(next_helper, int) else None,
            state=state or self.state,
            created_at=self.created_at,
            updated_at=_utc_now(),
        )


def _mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    nested = value.get(key)
    if not isinstance(nested, Mapping):
        raise ValueError(f"proxy lease {key} is invalid")
    return nested


def _positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"proxy lease {label} pid is invalid")
    return value


def _port(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
        raise ValueError("proxy lease target port is invalid")
    return value


def _safe_host(value: Any) -> str:
    host = str(value).strip()
    if not host or any(char in host for char in "\r\n"):
        raise ValueError("proxy lease target host is invalid")
    return host


class ProxyLeaseStore:
    """Atomically persist a proxy lease and append a small lifecycle journal."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.journal_path = self.path.with_name(f"{self.path.stem}.journal.jsonl")

    def load(self) -> ProxyLease | None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"proxy lease cannot be read: {type(exc).__name__}") from exc
        if not isinstance(raw, Mapping):
            raise ValueError("proxy lease root must be an object")
        return ProxyLease.from_dict(raw)

    def save(self, lease: ProxyLease) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(lease.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def update_state(self, lease: ProxyLease, state: str) -> ProxyLease:
        updated = lease.with_state(state)
        self.save(updated)
        self.append_event(state, updated)
        return updated

    def update_helper_pid(self, lease: ProxyLease, helper_pid: int | None) -> ProxyLease:
        updated = lease.with_updates(helper_pid=helper_pid)
        self.save(updated)
        self.append_event("helper_started", updated)
        return updated

    def append_event(self, event: str, lease: ProxyLease | None = None, **details: Any) -> None:
        """Append bounded, non-secret lifecycle metadata for post-crash diagnosis."""
        record: dict[str, Any] = {"at": _utc_now(), "event": str(event)[:64]}
        if lease is not None:
            record.update(
                {
                    "state": lease.state,
                    "guance_pid": lease.app_pid,
                    "helper_pid": lease.helper_pid,
                    "target_host": lease.target_host,
                    "target_port": lease.target_port,
                }
            )
        for key, value in list(details.items())[:16]:
            if key not in {"payload", "credentials", "authorization", "cookie"}:
                record[str(key)] = str(value)[:256]
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return


class RuntimeInstanceLock:
    """Cross-process lock preventing two Guance runtimes taking the proxy."""

    _local_guard = threading.Lock()
    _local_held: set[str] = set()

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._stream: Any | None = None
        self._key = str(self.path.resolve())
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def acquire(self) -> bool:
        if self._held:
            return True
        with self._local_guard:
            if self._key in self._local_held:
                return False
            self.path.parent.mkdir(parents=True, exist_ok=True)
            stream = self.path.open("a+", encoding="utf-8")
            stream.seek(0)
            stream.write(f"{os.getpid()}\n")
            stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover - exercised on POSIX CI only
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, IOError):
                stream.close()
                return False
            self._stream = stream
            self._held = True
            self._local_held.add(self._key)
            return True

    def release(self) -> None:
        if not self._held:
            return
        with self._local_guard:
            stream = self._stream
            try:
                if stream is not None:
                    if os.name == "nt":
                        import msvcrt

                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:  # pragma: no cover - exercised on POSIX CI only
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except (OSError, IOError):
                pass
            finally:
                if stream is not None:
                    stream.close()
                self._stream = None
                self._held = False
                self._local_held.discard(self._key)


__all__ = [
    "LEASE_SCHEMA",
    "ProxyLease",
    "ProxyLeaseStore",
    "RuntimeInstanceLock",
]
