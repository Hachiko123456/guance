"""Default-on, isolated background training-data recorder.

Controller integration: construct a new recorder for each capture run; call
``observe_update`` at the top of _handle_update, and ``invalidate_current`` on
capture/state errors or reconnect (non-state updates themselves are ignored).
After existing stop cleanup, call ``close(timeout=0.2)`` (or zero). No producer
method calls disk, model, JSON encoding or the status callback. Its only lock is
a short metadata/queue lock, never held by the writer during I/O.

Files: training_data/episodes/<stable episode ID>.jsonl contains complete
canonical decision lines, atomically replaced as one episode. A per-run
manifest records episode quality, losses and write errors, including zero-record
episodes. A manifest left ``open`` after a crash is NOT completed evidence.
``accepted_candidate`` is not a training-readiness claim. A new default
source_id is generated per recorder; for idempotent offline identity supply a
stable source_id (e.g. input SHA256) and source='legacy' to the synchronous
TrainingDataCollector instead.

close never waits longer than its join timeout (clamped to 0..1 second) for I/O.
A timed-out daemon may finish draining later; the returned report explicitly
marks outstanding updates/records and writer_alive. No exception messages,
source paths, raw payloads, or advice content are logged or persisted.
"""
from __future__ import annotations

from pathlib import Path
from queue import Empty, Full, Queue
import os
import threading
import uuid
from typing import Any, Callable

from guance.contracts import EngineUpdate

from .collector import INVALIDATION_REASONS, TrainingDataCollector
from .schema import SCHEMA_VERSION, canonical_json, finite_number, snapshot_update


class TrainingDataRecorder:
    def __init__(
        self, data_root: str | Path, *, source: str = "real", source_id: str | None = None,
        max_queue: int = 256, on_status: Callable[[dict], None] | None = None,
        rule_profile: str | None = None, rule_revision: str | None = None,
    ) -> None:
        if type(max_queue) is not int or max_queue < 1:
            raise ValueError("invalid_max_queue")
        self.source_id = source_id if source_id is not None else uuid.uuid4().hex
        self._collector = TrainingDataCollector(
            source=source, source_id=self.source_id,
            rule_profile=rule_profile, rule_revision=rule_revision,
        )
        self._root = Path(data_root) / "training_data"
        self._run_id = uuid.uuid4().hex
        self._on_status = on_status
        self._queue: Queue[tuple[int, dict]] = Queue(maxsize=max_queue)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._accepting = True
        self._epoch = 0
        self._worker_epoch = 0
        self._key: tuple[str, str] | None = None
        self._bad_epochs: dict[int, str] = {}
        self._entries: dict[str, dict] = {}  # writer-owned; never exposed by reference
        self._counts = {
            "enqueued_updates": 0, "processed_updates": 0, "dropped_updates": 0,
            "invalid_updates": 0, "writer_errors": 0, "written_records": 0,
            "lost_records": 0, "written_episodes": 0, "pending_records": 0,
            "in_flight_records": 0, "ignored_advice": 0,
        }
        self._thread = threading.Thread(target=self._run, name="guance-training-data", daemon=True)
        self._thread.start()

    def observe_update(self, update: EngineUpdate) -> None:
        """Whitelist-copy then put_nowait. Queue loss never escapes to caller.

        Calls must be in engine update order (as in Controller's update lock).
        Upstream containers may be mutated after this method returns.
        """
        if not self._accepting:
            return
        try:
            message = snapshot_update(update)
        except Exception:
            with self._lock:
                self._counts["invalid_updates"] += 1
                self._bad_epochs[self._epoch] = "invalid_update"
            return
        if message is None:
            return
        table = message["table"]
        key = (table["session_id"], table["game_id"])
        with self._lock:
            if not self._accepting:
                return
            if message["message"] == "deal" and key != self._key:
                self._epoch += 1
                self._key = key
            try:
                self._queue.put_nowait((self._epoch, message))
                self._counts["enqueued_updates"] += 1
            except Full:
                self._counts["dropped_updates"] += 1
                self._bad_epochs[self._epoch] = "queue_overflow"

    # Submit is an alias for application adapters that prefer queue terminology.
    submit = observe_update

    def observe_advice(self, result: Any) -> None:
        """Intentionally ignore all advice, including candidate cards and scores."""
        with self._lock:
            if self._accepting:
                self._counts["ignored_advice"] += 1

    def invalidate_current(self, reason: str = "external_loss") -> None:
        reason = reason if reason in INVALIDATION_REASONS else "external_loss"
        with self._lock:
            if self._accepting:
                self._bad_epochs[self._epoch] = reason

    def boundary(self, reason: str = "reconnect") -> None:
        """Invalidate until a different fresh Deal; never fabricate a boundary."""
        self.invalidate_current(reason)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            result = dict(self._counts)
            result.update({
                "accepting": self._accepting,
                "active_invalidated": self._epoch in self._bad_epochs,
                "source_id": self.source_id,
            })
        result["writer_alive"] = self._thread.is_alive()
        result["unflushed_updates"] = result["enqueued_updates"] - result["processed_updates"]
        result["unflushed_records"] = result["pending_records"] + result["in_flight_records"]
        result["complete"] = (
            not result["accepting"] and not result["writer_alive"]
            and not result["writer_errors"] and not result["dropped_updates"]
            and not result["invalid_updates"] and not result["unflushed_updates"]
            and not result["unflushed_records"]
        )
        return result

    def close(self, timeout: float = 0.2) -> dict[str, Any]:
        try:
            wait = min(1.0, finite_number(timeout))
        except (TypeError, ValueError):
            wait = 0.0
        with self._lock:
            self._accepting = False
        self._stop.set()
        if threading.current_thread() is not self._thread:
            self._thread.join(wait)
        return self.stats()

    def _bad(self, epoch: int) -> str | None:
        with self._lock:
            return self._bad_epochs.get(epoch)

    def _writer_error(self, lost_records: int = 0) -> None:
        with self._lock:
            self._counts["writer_errors"] += 1
            self._counts["lost_records"] += lost_records
            self._bad_epochs[self._epoch] = "writer_error"
            self._bad_epochs[self._worker_epoch] = "writer_error"

    def _run(self) -> None:
        try:
            self._write_manifest("open")
        except Exception:
            self._writer_error()
        try:
            while True:
                try:
                    epoch, message = self._queue.get(timeout=0.01)
                except Empty:
                    bad = self._bad(self._worker_epoch)
                    if bad:
                        self._persist(self._collector.invalidate_current(bad))
                    if self._stop.is_set():
                        break
                    continue
                try:
                    bad = self._bad(self._worker_epoch)
                    if bad:
                        self._persist(self._collector.invalidate_current(bad))
                    batches = self._collector.submit(message)
                    self._worker_epoch = epoch
                    bad = self._bad(epoch)
                    if bad:
                        # A producer-side invalidation can race a settlement
                        # batch that was just built. Persist only the latest
                        # quarantine revision; never let stale accepted data
                        # overwrite it.
                        corrected = self._collector.invalidate_current(bad)
                        self._persist(corrected)
                    else:
                        self._persist(batches)
                except Exception:
                    self._writer_error()
                finally:
                    with self._lock:
                        self._counts["processed_updates"] += 1
                        self._counts["pending_records"] = self._collector.stats()["pending_records"]
                    self._queue.task_done()
            bad = self._bad(self._worker_epoch)
            if bad:
                self._persist(self._collector.invalidate_current(bad))
            self._persist(self._collector.close())
        except Exception:
            self._writer_error()
        finally:
            with self._lock:
                self._counts["pending_records"] = self._collector.stats()["pending_records"]
            try:
                counts = self.stats()
                degraded = any(counts[k] for k in ("writer_errors", "dropped_updates", "invalid_updates"))
                self._write_manifest("degraded" if degraded else "closed")
            except Exception:
                self._writer_error()
            # Arbitrary callbacks only run here, never under the producer lock.
            if self._on_status is not None:
                try:
                    self._on_status(self.stats())
                except Exception:
                    pass

    def _persist(self, batches: list[dict]) -> None:
        for episode in batches:
            with self._lock:
                self._counts["in_flight_records"] = len(episode["records"])
                self._counts["pending_records"] = self._collector.stats()["pending_records"]
            try:
                self._write_episode(episode)
                self._entries[episode["episode_id"]] = {k: v for k, v in episode.items() if k != "records"} | {
                    "record_count": len(episode["records"]),
                    "file": "episodes/" + episode["episode_id"] + ".jsonl",
                }
                self._write_manifest("open")
                with self._lock:
                    self._counts["written_records"] += len(episode["records"])
                    self._counts["written_episodes"] += 1
            except Exception:
                self._writer_error(len(episode["records"]))
            finally:
                with self._lock:
                    self._counts["in_flight_records"] = 0

    def _write_episode(self, episode: dict) -> None:
        """Writer-thread seam for isolated disk-failure/blocked-disk tests."""
        text = "".join(canonical_json(record) + "\n" for record in episode["records"])
        self._atomic_write(self._root / "episodes" / (episode["episode_id"] + ".jsonl"), text)

    def _write_manifest(self, status: str) -> None:
        manifest = {
            "record_type": "manifest", "schema_version": SCHEMA_VERSION,
            "source": self._collector.source, "source_id": self.source_id,
            "status": status, "episodes": list(self._entries.values()),
            "counters": dict(self._counts),
        }
        self._atomic_write(self._root / "manifests" / (self._run_id + ".json"), canonical_json(manifest) + "\n")

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
