"""Qt-agnostic application controller connecting runtime, state, and model."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Any
from collections.abc import Callable

from guance.config import (
    app_paths,
    default_model_source,
    ensure_app_paths,
    load_preferences,
    save_preferences,
)
from guance.contracts import (
    CanonicalAdviceResult, EngineUpdate, ModelLifecycleState, ModelStatusSnapshot, SourceFrame,
)
from guance.domain.session import AggregateParser, SessionEngine
from guance.diagnostics import DiagnosticLog
from guance.application.model_service import ModelService
from guance.application.trusted_replay import project_trusted_replay_facts
from guance.application.runtime.service import RuntimeOptions
from guance.ports.capture import RuntimePaths
from guance.ports.model import ActiveSelectionDTO, ModelBackendError, ModelCatalogPort, ModelClientFactory

# Test/embedding compatibility hook; production supplies factories from bootstrap.
TrainingDataRecorder = None


class _NoLiveSubsetProvider:
    def adapt_event(self, event: object, _state: object, _session_id: str):
        return (event,)
    def observe_applied(self, _event: object, _state: object, _session_id: str) -> None:
        return None
    def reset_session(self) -> None:
        return None
    def bind_capture(self, _capture_id: str) -> None:
        return None
    def unbind_capture(self) -> None:
        return None
    def bind_runtime_session(self, _session_id: str) -> None:
        return None
    def unbind_runtime_session(self) -> None:
        return None
    def drain_decisions(self) -> tuple[object, ...]:
        return ()


class DebugCaptureError(RuntimeError):
    """Raised when an enabled local debug capture cannot be written."""


class DebugFrameRecorder:
    """Persist validated source frames before the protocol parser sees them.

    This deliberately records only the already allow-listed WebSocket frames
    delivered by the capture supervisor. It never sees headers, cookies, or
    proxy credentials. The file is local-only and ignored by Git.
    """

    MAX_BYTES = 256 * 1024 * 1024
    SIDECAR_SCHEMA = "guance-capture-closed-sidecar-v1"
    CAPTURE_SCHEMA = "guance-debug-frame-jsonl-v1"
    PRODUCER = "guance.DebugFrameRecorder"
    PRODUCER_VERSION = "1"
    PROTOCOL_IDENTITY = "TencentDaguandanObservedProtocol.v20261014"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.session_dir: Path | None = None
        self.path: Path | None = None
        self.games_dir: Path | None = None
        self.status_path: Path | None = None
        self._stream = None
        self._bytes_written = 0
        self._frame_count = 0
        self._direction_counts: dict[str, int] = {}
        self._host_counts: dict[str, int] = {}
        self._games: dict[str, dict[str, Any]] = {}
        self._current_game_id = ""
        self._connection_epochs: list[dict[str, Any]] = []
        self._current_connection_id = ""
        self._final_sequence: int | None = None

    @property
    def enabled(self) -> bool:
        return self._stream is not None

    def start(self) -> Path:
        if self._stream is not None:
            return self.path or self.root
        session = datetime.now().strftime("%Y%m%d-%H%M%S")
        session_dir = self.root / session
        suffix = 1
        while session_dir.exists():
            suffix += 1
            session_dir = self.root / f"{session}-{suffix:02d}"
        session_dir.mkdir(parents=True, exist_ok=False)
        path = session_dir / "frames.jsonl"
        try:
            self._stream = path.open("a", encoding="utf-8", newline="\n")
        except OSError as exc:
            raise DebugCaptureError(f"无法创建调试报文文件：{exc}") from exc
        self.session_dir = session_dir
        self.path = path
        self.games_dir = session_dir / "games"
        self.status_path = session_dir / "capture_status.json"
        self._bytes_written = 0
        self._frame_count = 0
        self._direction_counts = {}
        self._host_counts = {}
        self._games = {}
        self._current_game_id = ""
        self._connection_epochs = []
        self._current_connection_id = ""
        self._final_sequence = None
        self._write_status("waiting_for_frames")
        return path

    def _write_status(self, state: str, *, close_reason: str = "") -> None:
        """Atomically persist bounded capture status without source contents."""
        if self.session_dir is None or self.status_path is None:
            return
        source_sha256 = ""
        source_bytes = self._bytes_written
        closed_at = ""
        if state == "closed":
            if self.path is None or not self.path.is_file():
                return
            try:
                digest = hashlib.sha256()
                source_bytes = 0
                with self.path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        source_bytes += len(chunk)
                        digest.update(chunk)
                source_sha256 = digest.hexdigest()
            except OSError:
                return
            closed_at = datetime.now().astimezone().isoformat(timespec="seconds")
        value = {
            "schema": self.SIDECAR_SCHEMA,
            "schema_version": 1,
            "capture_schema": self.CAPTURE_SCHEMA,
            "producer": self.PRODUCER,
            "producer_version": self.PRODUCER_VERSION,
            "protocol_identity": self.PROTOCOL_IDENTITY,
            "capture_id": self.session_dir.name,
            "source_filename": self.path.name if self.path is not None else "frames.jsonl",
            "source_sha256": source_sha256,
            "source_bytes": source_bytes,
            "state": str(state),
            "started_at": self.session_dir.name,
            "closed_at": closed_at,
            "close_reason": str(close_reason),
            "frames": self._frame_count,
            "bytes": self._bytes_written,
            "directions": dict(sorted(self._direction_counts.items())),
            "hosts": dict(sorted(self._host_counts.items())),
            "connection_epochs": [dict(item) for item in self._connection_epochs],
            "final_sequence": self._final_sequence,
            "current_game_id": self._current_game_id,
            "games": [dict(item) for item in self._games.values()],
        }
        temporary = self.status_path.with_suffix(".tmp")
        try:
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            temporary.replace(self.status_path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def append(self, frame: SourceFrame) -> None:
        stream = self._stream
        if stream is None:
            return
        record = {
            "schema_version": 1,
            "host": str(frame.host),
            "received_at": float(frame.received_at),
            "connection_id": str(frame.connection_id),
            "sequence": int(frame.sequence),
            "direction": str(frame.direction),
            "payload_bytes": len(frame.payload),
            "payload_sha256": hashlib.sha256(frame.payload).hexdigest(),
            "payload_b64": base64.b64encode(frame.payload).decode("ascii"),
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        encoded_size = len(line.encode("utf-8"))
        if self._bytes_written + encoded_size > self.MAX_BYTES:
            raise DebugCaptureError("调试报文已达到 256 MB 上限，请先停止并清理旧会话。")
        try:
            stream.write(line)
            stream.flush()
        except OSError as exc:
            raise DebugCaptureError(f"调试报文写入失败：{exc}") from exc
        self._bytes_written += encoded_size
        self._frame_count += 1
        direction = str(frame.direction)
        self._direction_counts[direction] = self._direction_counts.get(direction, 0) + 1
        connection_id = str(frame.connection_id)
        if not self._connection_epochs or connection_id != self._current_connection_id:
            self._current_connection_id = connection_id
            self._connection_epochs.append({
                "ordinal": len(self._connection_epochs) + 1,
                "start_sequence": int(frame.sequence),
                "end_sequence": int(frame.sequence),
                "frames": 0,
                "directions": {},
            })
        epoch = self._connection_epochs[-1]
        epoch["end_sequence"] = int(frame.sequence)
        epoch["frames"] = int(epoch["frames"]) + 1
        epoch_directions = dict(epoch["directions"])
        epoch_directions[direction] = epoch_directions.get(direction, 0) + 1
        epoch["directions"] = dict(sorted(epoch_directions.items()))
        self._final_sequence = int(frame.sequence)
        host = str(frame.host)
        self._host_counts[host] = self._host_counts.get(host, 0) + 1
        if self._current_game_id:
            game = self._games.get(self._current_game_id)
            if game is not None:
                game["end_frame_index"] = self._frame_count - 1
                game["end_sequence"] = int(frame.sequence)
        if self._frame_count == 1 or self._frame_count % 32 == 0:
            self._write_status("capturing")

    @staticmethod
    def _safe_game_name(game_id: str) -> str:
        value = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in str(game_id))
        return value[:120] or "game"

    def begin_game(self, game_id: str, sequence: int) -> None:
        """Start a logical game segment after a validated deal was observed."""
        game_id = str(game_id)
        if not game_id or game_id == self._current_game_id:
            return
        if self._current_game_id:
            self.finish_game(self._current_game_id, max(0, int(sequence) - 1), "superseded")
        assert self.games_dir is not None
        self.games_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "game_id": game_id,
            "status": "active",
            # The deal frame has already been appended before the parser emits
            # the state update, so include the current line in this segment.
            "start_frame_index": max(0, self._frame_count - 1),
            "end_frame_index": max(0, self._frame_count - 1),
            "start_sequence": int(sequence),
            "end_sequence": int(sequence),
        }
        self._games[game_id] = record
        self._current_game_id = game_id
        self._write_game_metadata(record)
        self._write_status("capturing_game")

    def _write_game_metadata(self, record: dict[str, Any]) -> None:
        if self.games_dir is None:
            return
        directory = self.games_dir / self._safe_game_name(str(record.get("game_id", "game")))
        try:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "metadata.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError:
            return

    def finish_game(self, game_id: str, sequence: int, status: str = "settled") -> None:
        game = self._games.get(str(game_id))
        if game is None:
            return
        game["status"] = str(status)
        game["end_frame_index"] = max(int(game.get("start_frame_index", 0)), self._frame_count - 1)
        game["end_sequence"] = int(sequence)
        self._write_game_metadata(game)
        if status == "settled":
            # The session stream is flushed per frame, so materialize this
            # completed game immediately instead of waiting for app shutdown.
            try:
                self._materialize_games()
            except OSError:
                pass
            self._write_status("settled")
        else:
            self._write_status("capturing_game")
        if self._current_game_id == str(game_id):
            self._current_game_id = ""

    def _materialize_games(self) -> None:
        if self.path is None or self.games_dir is None or not self._games:
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError:
            return
        for game_id, record in self._games.items():
            directory = self.games_dir / self._safe_game_name(game_id)
            directory.mkdir(parents=True, exist_ok=True)
            start = max(0, int(record.get("start_frame_index", 0)))
            end = min(len(lines) - 1, int(record.get("end_frame_index", len(lines) - 1)))
            if end >= start:
                (directory / "frames.jsonl").write_text("".join(lines[start:end + 1]), encoding="utf-8")
            (directory / "metadata.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )

    @property
    def stats(self) -> dict[str, Any]:
        """Return bounded, metadata-only capture statistics."""
        return {
            "session_directory": str(self.session_dir) if self.session_dir else "",
            "file": str(self.path) if self.path else "",
            "frames": self._frame_count,
            "bytes": self._bytes_written,
            "directions": dict(sorted(self._direction_counts.items())),
            "hosts": dict(sorted(self._host_counts.items())),
            "status_file": str(self.status_path) if self.status_path else "",
            "games": [dict(item) for item in self._games.values()],
        }

    def close(self) -> None:
        if self._stream is None:
            return
        stream, self._stream = self._stream, None
        source_closed = True
        if stream is not None:
            try:
                stream.flush()
            except OSError:
                source_closed = False
            try:
                stream.close()
            except OSError:
                source_closed = False
        if not source_closed:
            self._write_status("close_failed", close_reason="source_close_failed")
            return
        close_reason = "recorder_closed"
        if self._current_game_id:
            game = self._games.get(self._current_game_id)
            if game is not None:
                game["status"] = "incomplete"
                self._write_game_metadata(game)
                close_reason = "recorder_closed_with_incomplete_game"
            self._current_game_id = ""
        if self._bytes_written:
            try:
                self._materialize_games()
            except OSError:
                # The complete session capture remains available even if a
                # per-game forensic copy cannot be materialized.
                pass
            self._write_status("closed", close_reason=close_reason)
            return
        # Avoid leaving a directory behind when startup produced no frames.
        # Only remove the exact files/directories created by this recorder.
        if self.path is not None:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
        if self.session_dir is not None:
            try:
                self.session_dir.rmdir()
            except OSError:
                pass



class GuanceController:
    def __init__(self, viewmodel: Any, *, paths: dict[str, Path] | None = None,
                 model_client_factory: ModelClientFactory | None = None,
                 aggregate_parser: AggregateParser | None = None,
                 accepted_host: str | None = None,
                 runtime_factory: Callable[..., Any] | None = None,
                 diagnostics_factory: Callable[[], Any] | None = None,
                 training_recorder_factory: Callable[..., Any] | None = None,
                 certificate_inspector: Callable[[Path], Any] | None = None,
                 live_subset_provider: Any | None = None,
                 canonical_converter_factory: Callable[[Path, str], Any] | None = None,
                 confirmed_decision_projector: Callable[..., Any] | None = None,
                 catalog: ModelCatalogPort | None = None,
                 initial_packaged_model: Path | None = None):
        self.viewmodel = viewmodel
        self.paths = paths or app_paths()
        ensure_app_paths(self.paths)
        self.log = DiagnosticLog(self.paths["logs_dir"])
        self.preferences = load_preferences(self.paths)
        # The application layer owns orchestration; the composition root injects
        # the concrete process/client adapter. Without one, model use is explicit
        # unavailable rather than an implicit provider fallback.
        self._model_client_factory = model_client_factory
        self._runtime_factory = runtime_factory
        self._diagnostics_factory = diagnostics_factory
        self._training_recorder_factory = training_recorder_factory
        self._certificate_inspector = certificate_inspector
        self._canonical_converter_factory = canonical_converter_factory
        self._decision_projector = confirmed_decision_projector
        self._catalog = catalog
        self._live_subset = live_subset_provider or _NoLiveSubsetProvider()
        identity_hook = getattr(self._live_subset, "set_model_artifact_identity_provider", None)
        if callable(identity_hook):
            identity_hook(self._model_artifact_identity)
        self.session = SessionEngine(
            "desktop", aggregate_parser=aggregate_parser, accepted_host=accepted_host,
            event_adapter=self._live_subset.adapt_event,
            event_observer=self._live_subset.observe_applied,
            reset_observer=self._live_subset.reset_session,
        )
        self._lock = threading.RLock()
        self._runtime: Any | None = None
        self._operation_lock = threading.Lock()
        self._debug_recorder: DebugFrameRecorder | None = None
        self._canonical_converter: Any | None = None
        self._last_canonical_close: Any | None = None
        self._debug_capture_failed = False
        self._training_recorder: Any | None = None
        self._training_failed = False
        self._training_stopped = False
        self._training_last_stats: dict[str, Any] = {}
        self._last_debug_capture_stats: dict[str, Any] = {}
        self._current_game_id = ""
        self._table_game_key: tuple[str, str] | None = None
        self._table_seat_confirmed = False
        self._last_frame_received_at: float | None = None
        self._active_advice_key: tuple[str, str, int] | None = None
        self._latest_model_observation = None
        self._pending_model_observation = None
        self._pending_model_generation: int | None = None
        self._pending_confirmed_key: tuple[str, str, int] | None = None
        # Replay-recommendation scheduling (V2): identities already submitted
        # (one advice per confirmed turn, ever) and turns awaiting a trusted
        # confirmation that arrived after their observation update.
        self._advice_submitted: set[tuple[str, str, int]] = set()
        self._advice_pending: dict[tuple[str, str, int], int | None] = {}
        self._primary_engine_error: dict[str, Any] | None = None
        self._engine_error_repeats = 0
        self._model_selection_lock = threading.RLock()
        self._selection_generation = 0
        self._model_ui_sequence = 0
        self._selection_failed = bool(self.preferences.get('model_selection_failed', False))
        self._selection_failure_code = 'MODEL_NOT_LOADED'
        self._no_selection = False
        self._active_catalog_id = ""
        self._selection_in_progress = False
        # Generation that most recently reached a verified ready. Used to keep a
        # mid-session (post-ready) inference error in-session only: it must not
        # persist a durable selection-failure latch that would block the next
        # launch, whereas a genuine load failure (the generation never became
        # ready) does.
        self._ready_generation = -1
        self._controller_closed = False
        if self._selection_failed:
            self._requested_source = self.preferences.get('requested_model_path', '')
        else:
            resolved = self._resolve_startup_source(initial_packaged_model)
            if self._selection_failed:
                # Tampered/broken active selection: fail-closed latch, explicit retry only.
                self._requested_source = ""
            elif resolved is None:
                self._no_selection = True
                self._requested_source = ""
            else:
                self._requested_source = resolved
        self._persisted_source = self.preferences.get('model_path', '')
        self._create_model_service()
        if hasattr(self.viewmodel, "set_split_layout"):
            self.viewmodel.set_split_layout(self.preferences.get("split_layout", "side"))
        self._attach_signals()
        self._refresh_catalog_ui()
        self.viewmodel.set_debug_capture_enabled(
            bool(self.preferences.get("debug_capture_enabled", False))
        )
        saved_layout = self.preferences.get("floating_layout", {})
        if isinstance(saved_layout, dict):
            self.viewmodel.restore_floating_layout(saved_layout)

    def _resolve_startup_source(self, initial_packaged_model: Path | None) -> Path | None:
        """Honor the explicit selection before using the full package's initial model."""
        override = os.environ.get("GUANCE_MODEL_BUNDLE") or os.environ.get("GUANCE_MODEL_PATH")
        if isinstance(override, str) and override.strip():
            return Path(override)
        if self._catalog is None:
            # Legacy embedding compatibility only; the production composition root
            # always injects a catalog, so real startup is selection-driven.
            return default_model_source(self.paths, self.preferences)
        try:
            document = self._catalog.load_active()
        except ModelBackendError as exc:
            # A tampered/corrupt selection document is explicit failure, not "no selection".
            self._selection_failed = True
            self._selection_failure_code = exc.code if exc.code in (
                'ACTIVE_SELECTION_INVALID', 'MODEL_PREFERENCES_FAILED') else 'ACTIVE_SELECTION_INVALID'
            self._persist_startup_selection_failure("")
            return None
        except Exception:
            self._selection_failed = True
            self._selection_failure_code = 'ACTIVE_SELECTION_INVALID'
            self._persist_startup_selection_failure("")
            return None
        if document is None:
            legacy = self.preferences.get('model_path', '')
            if isinstance(legacy, str) and legacy.strip():
                # The pre-catalog explicit choice is adopted only after the active
                # document is committed; migration failure is explicit unavailable.
                if not self._migrate_legacy_selection(legacy.strip()):
                    return None
                return Path(legacy.strip())
            return initial_packaged_model
        if document.kind == "catalog":
            try:
                source = self._catalog.resolve_bundle(document.model_id, document.bundle)
            except ModelBackendError:
                self._selection_failed = True
                self._selection_failure_code = 'ACTIVE_SELECTION_INVALID'
                self._persist_startup_selection_failure(document.model_id)
                return None
            except Exception:
                self._selection_failed = True
                self._selection_failure_code = 'ACTIVE_SELECTION_INVALID'
                self._persist_startup_selection_failure(document.model_id)
                return None
            self._active_catalog_id = document.model_id
            return source
        return Path(document.path)

    def _migrate_legacy_selection(self, legacy: str) -> bool:
        """Record the pre-catalog explicit choice as a committed active document.

        The legacy source is adopted only when the active document commits;
        otherwise startup stays explicit unavailable (never a silent load).
        """
        if self._catalog is None:
            return False
        selection = ActiveSelectionDTO(kind="legacy", path=legacy,
                                       selected_at=self._selection_timestamp())
        snapshot: object | None = None
        try:
            snapshot = self._catalog.stage_active(selection)
            self._catalog.commit_active()
        except Exception:
            if snapshot is not None:
                try:
                    self._catalog.restore_active(snapshot)
                except Exception:
                    pass
            self.log.write("model_selection_migration_failed", level="ERROR",
                           message="legacy model_path could not be recorded as active selection")
            return False
        self.log.write("model_selection_migrated",
                       message="legacy model_path recorded as active selection")
        return True

    @staticmethod
    def _selection_timestamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec='seconds')

    def _persist_startup_selection_failure(self, requested: object) -> None:
        """Persist the fail-closed latch for a broken/ambiguous startup selection.

        The durable latch forces an explicit user re-selection even after the
        external cause (tamper, duplicate, reparse) has been removed; best effort
        only when storage itself is unavailable.
        """
        try:
            preferences = {**self.preferences,
                           'model_selection_failed': True,
                           'requested_model_path': str(requested)}
            save_preferences(self.paths, preferences)
        except Exception:
            return
        self.preferences = preferences

    def _refresh_catalog_ui(self) -> None:
        if self._catalog is None:
            return
        try:
            scan = self._catalog.scan()
            entries = [{"model_id": entry.model_id, "directory": entry.directory,
                        "provider": entry.provider, "backend": entry.backend,
                        "runtime_contract": entry.runtime_contract,
                        "classification": entry.classification,
                        "experimental": entry.experimental,
                        "production_ready": entry.production_ready,
                        "strength_claim": entry.strength_claim,
                        "artifact_sha256": entry.artifact_sha256}
                       for entry in scan.entries]
            problems = [{"directory": problem.directory, "code": problem.code}
                        for problem in scan.problems]
        except ModelBackendError as exc:
            entries, problems = [], [{"directory": "", "code": exc.code}]
        except Exception:
            entries, problems = [], [{"directory": "", "code": "CATALOG_BUNDLE_INVALID"}]
        source = self._persisted_source or self._requested_source
        if self._active_catalog_id:
            active_summary = "catalog：" + self._active_catalog_id
        elif isinstance(source, (str, Path)) and str(source).strip():
            active_summary = "legacy：" + Path(str(source)).name
        else:
            active_summary = ""
        try:
            self.viewmodel.set_catalog(entries, problems, self._active_catalog_id, active_summary)
        except AttributeError:
            return

    def _attach_signals(self) -> None:
        self.viewmodel.startRequested.connect(self.start)
        self.viewmodel.stopRequested.connect(self.stop)
        self.viewmodel.authorizeRequested.connect(self.authorize)
        self.viewmodel.repairRequested.connect(self.repair)
        self.viewmodel.removeCertificateRequested.connect(self.remove_certificate)
        self.viewmodel.exportDiagnosticsRequested.connect(self.export_diagnostics)
        self.viewmodel.openDiagnosticsDirectoryRequested.connect(self.open_export_directory)
        self.viewmodel.openCaptureDirectoryRequested.connect(self.open_capture_directory)
        self.viewmodel.floatingLayoutSaveRequested.connect(self.save_floating_layout)
        if hasattr(self.viewmodel, "splitLayoutSaveRequested"):
            self.viewmodel.splitLayoutSaveRequested.connect(self.save_split_layout)
        self.viewmodel.confirmSeatRequested.connect(self.confirm_seat)
        self.viewmodel.modelImportRequested.connect(self.import_model)
        self.viewmodel.debugCaptureRequested.connect(self.set_debug_capture_enabled)
        if hasattr(self.viewmodel, "catalogModelRequested"):
            self.viewmodel.catalogModelRequested.connect(self.select_catalog_model)
        if hasattr(self.viewmodel, "catalogRefreshRequested"):
            self.viewmodel.catalogRefreshRequested.connect(self.refresh_catalog)

    def refresh_catalog(self) -> None:
        with self._lock:
            if self._controller_closed:
                return
        self._refresh_catalog_ui()


    def confirm_seat(self, relation: Any) -> None:
        """Apply a GUI-requested relative first-seat confirmation safely.

        The presentation layer intentionally exposes only relative labels. This
        handler validates that boundary again before calling the domain engine,
        serializes the call with frame handling, and routes every resulting
        update through the normal update path so an own-turn confirmation can
        submit exactly the same observation as a captured frame.
        """
        allowed = {"self", "left", "partner", "right"}
        if not isinstance(relation, str) or relation.strip().lower() not in allowed:
            self._handle_update(EngineUpdate(
                kind="error",
                message="invalid seat relation; expected self, left, partner, or right",
                error_code="manual_seat_invalid",
            ))
            return
        relation = relation.strip().lower()
        with self._lock:
            updates = self.session.confirm_my_seat_relative(
                relation,
                captured_at=self._last_frame_received_at,
            )
            for update in updates:
                self._handle_update(update)

    def save_split_layout(self, layout: str) -> None:
        if layout not in ("side", "stacked"):
            return
        with self._lock:
            self.preferences["split_layout"] = layout
            try:
                save_preferences(self.paths, self.preferences)
            except OSError:
                self.viewmodel.append_log({"level": "ERROR", "code": "SPLIT_LAYOUT_SAVE_FAILED",
                                           "message": "分屏布局保存失败。"})

    def save_floating_layout(self, layout: dict[str, Any]) -> None:
        """Persist a bounded sidecar layout without affecting runtime state."""
        if not isinstance(layout, dict):
            return
        self.preferences["floating_layout"] = dict(layout)
        try:
            save_preferences(self.paths, self.preferences)
        except OSError as exc:
            self.viewmodel.append_log({
                "level": "ERROR",
                "code": "FLOATING_LAYOUT_SAVE_FAILED",
                "message": f"小窗口布局保存失败：{type(exc).__name__}",
            })

    def set_debug_capture_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        self.preferences["debug_capture_enabled"] = enabled
        try:
            save_preferences(self.paths, self.preferences)
        except OSError as exc:
            self.viewmodel.append_log({
                "level": "ERROR",
                "code": "DEBUG_CAPTURE_PREFERENCE_FAILED",
                "message": f"调试报文开关保存失败：{type(exc).__name__}",
            })
            return
        self.viewmodel.append_log({
            "level": "INFO",
            "code": "DEBUG_CAPTURE_ENABLED" if enabled else "DEBUG_CAPTURE_DISABLED",
            "message": "调试报文保存将在下一次开始使用时生效。" if enabled
            else "已关闭调试报文保存。",
        })

    def _debug_capture_root(self) -> Path:
        if getattr(sys, "frozen", False):
            return self.paths["data_dir"] / "debug" / "captures"
        return self.paths["root_dir"] / "debug" / "captures"

    @staticmethod
    def _capture_port_available(host: str = "127.0.0.1", port: int = 18080) -> bool:
        """Reject stale/foreign listeners instead of mistaking them for readiness."""
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
            return True
        except OSError:
            return False
        finally:
            probe.close()

    def _open_debug_recorder(self) -> None:
        self._debug_capture_failed = False
        self._last_canonical_close = None
        # Recommendation runtime/session identity is independent of optional
        # debug-evidence recording: the confirmed-decision producer is bound on
        # every capture run, whether or not raw frames are written.
        self._bind_recommendation_session()
        if not bool(self.preferences.get("debug_capture_enabled", False)):
            self._debug_recorder = None
            self._canonical_converter = None
            return
        recorder = DebugFrameRecorder(self._debug_capture_root())
        path = recorder.start()
        try:
            converter = (self._canonical_converter_factory(path.parent, path.parent.name)
                         if self._canonical_converter_factory is not None else None)
            self._live_subset.bind_capture(path.parent.name)
        except Exception:
            recorder.close()
            raise
        self._debug_recorder = recorder
        self._canonical_converter = converter
        self.viewmodel.append_log({
            "level": "INFO",
            "code": "DEBUG_CAPTURE_STARTED",
            "message": f"调试报文保存已启动：{path}",
        })

    def _bind_recommendation_session(self) -> None:
        """Bind the real decision producer even when debug recording is disabled.

        Recommendation decisions need a stable runtime/session identity that is
        independent of the optional evidence recorder (no raw frame file or
        converter is required).
        """
        try:
            self._live_subset.bind_runtime_session("runtime-session")
        except Exception:
            pass  # provider stays unbindable -> no decisions (fail closed)

    def _clear_recommendation_state_locked(self) -> None:
        """Clear every recommendation identity together (caller holds the lock)."""
        self._active_advice_key = None
        self._pending_confirmed_key = None
        self._pending_model_observation = None
        self._pending_model_generation = None
        self._latest_model_observation = None
        # Pending deferred confirmations die with the recommendation state;
        # submitted identities stay, so an already-answered turn is never
        # answered twice even after a reset.
        self._advice_pending.clear()

    def _clear_pending_model_requests(self) -> None:
        """Clear every stale/pending recommendation identity (never partially)."""
        with self._lock:
            self._clear_recommendation_state_locked()

    def _close_debug_recorder(self) -> None:
        recorder, self._debug_recorder = self._debug_recorder, None
        converter, self._canonical_converter = self._canonical_converter, None
        source_error: Exception | None = None
        path = recorder.path if recorder is not None else None
        if recorder is not None:
            try:
                recorder.close()
            except Exception as exc:  # source must close before the converter
                source_error = exc
            self._last_debug_capture_stats = recorder.stats
        if converter is not None:
            if source_error is not None:
                converter.block("SOURCE_CLOSE_FAILED")
            try:
                self._last_canonical_close = converter.close()
            except Exception:
                self._last_canonical_close = None
        self._live_subset.unbind_capture()
        self._live_subset.unbind_runtime_session()
        if path is not None and self._last_debug_capture_stats.get("frames", 0):
            self.viewmodel.append_log({
                "level": "INFO",
                "code": "DEBUG_CAPTURE_STOPPED",
                "message": f"调试报文已保存：{path}",
            })
        if source_error is not None:
            raise source_error

    def _ensure_training_recorder(self) -> None:
        # The capture callback and asynchronous start thread can race here.
        # Use the existing RLock so only one recorder owns a capture run.
        with self._lock:
            if (self._training_recorder is not None or self._training_failed or self._training_stopped
                    or os.environ.get("GUANCE_TRAINING_DATA", "1").lower() in {"0", "false", "off"}):
                return
            try:
                factory = self._training_recorder_factory or TrainingDataRecorder
                if factory is None:
                    raise RuntimeError("training recorder composition is unavailable")
                self._training_recorder = factory(
                    self.paths["data_dir"], rule_profile="tencent_daguandan",
                    rule_revision="guance-observed-v1",
                )
            except Exception:
                self._training_failed = True

    def _training_update(self, update: EngineUpdate) -> None:
        """Optional subscriber: bounded in-memory copy/queue, never disk or model work.

        Recording is enabled by default and independent of debug frame recording.
        A failure must never escape into the verified state/recommendation path.
        """
        if self._training_stopped or self._training_failed:
            return
        if os.environ.get("GUANCE_TRAINING_DATA", "1").lower() in {"0", "false", "off"}:
            return
        try:
            if update.kind == "error":
                self._training_invalidate("state_error")
                return
            if update.kind != "state":
                return
            self._ensure_training_recorder()
            if self._training_recorder is not None:
                self._training_recorder.observe_update(update)
        except Exception:
            # Do not synchronously log private exception text or touch UI/model.
            self._training_failed = True
            self._training_invalidate("external_loss")

    def _training_invalidate(self, reason: str) -> None:
        try:
            if self._training_recorder is not None:
                self._training_recorder.invalidate_current(reason)
        except Exception:
            self._training_failed = True

    def _close_training_recorder(self) -> None:
        recorder, self._training_recorder = self._training_recorder, None
        if recorder is not None:
            try:
                # Existing network/model cleanup always precedes a stop call.
                # Daemon drains asynchronously; application shutdown never waits on disk.
                self._training_last_stats = recorder.close(timeout=0.0)
            except Exception:
                self._training_failed = True

    def _training_stats(self) -> dict[str, Any]:
        try:
            value = self._training_recorder.stats() if self._training_recorder else self._training_last_stats
            # Metadata-only diagnostics; never include decisions or file paths.
            return {k: v for k, v in value.items() if type(v) in (int, bool)} | {
                "subscriber_failed": self._training_failed,
            }
        except Exception:
            return {"subscriber_failed": True}

    @staticmethod
    def _open_directory(path: Path) -> bool:
        """Open a local directory without passing it through a shell."""
        path = Path(path)
        if not path.is_dir():
            return False
        try:
            if hasattr(os, "startfile"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except (OSError, ValueError):
            return False
        return True

    def open_export_directory(self) -> bool:
        """Open the diagnostics export directory in the platform file manager."""
        opened = self._open_directory(self.paths["exports_dir"])
        if not opened:
            self.viewmodel.append_log({
                "level": "ERROR",
                "code": "EXPORT_DIRECTORY_OPEN_FAILED",
                "message": f"无法打开导出目录：{self.paths['exports_dir']}",
            })
        return opened

    def open_capture_directory(self) -> bool:
        """Open the current or most recent local capture directory."""
        directory = ""
        if self._debug_recorder is not None and self._debug_recorder.session_dir is not None:
            directory = str(self._debug_recorder.session_dir)
        else:
            directory = str(self._last_debug_capture_stats.get("session_directory", ""))
        opened = bool(directory) and self._open_directory(Path(directory))
        if not opened:
            self.viewmodel.append_log({
                "level": "ERROR",
                "code": "CAPTURE_DIRECTORY_OPEN_FAILED",
                "message": "当前没有可打开的调试报文目录。",
            })
        return opened


    def recover_stale_runtime_proxy(self) -> dict[str, Any]:
        """Recover a previous crash lease before building live capture options."""
        lease_path = self.paths["config_dir"] / "proxy-lease.json"
        if not lease_path.is_file():
            return {"ok": True, "state": "no_stale_lease"}
        runtime = self._build_runtime()
        try:
            result = runtime.recover_stale_proxy()
        finally:
            runtime.close()
        self._runtime = None
        if not result.get("ok"):
            self._status({
                "phase": "error",
                "error_message": result.get("message", "上次代理接管状态未能恢复"),
                "detail": "为避免覆盖其他程序的代理设置，当前未启动采集。",
            })
        return result

    def _mode_options(self) -> RuntimeOptions:
        helper = self.paths["runtime_dir"] / "mitmproxy" / "mitmdump.exe"
        addon = self.paths["runtime_dir"] / "mitmproxy" / "addon.py"
        # A frozen no-capture package must remain explicitly unavailable.  It must
        # never discover an unrelated system helper or source-tree addon after the
        # package audit deliberately excluded its controlled runtime.
        if not getattr(sys, "frozen", False):
            if not helper.is_file():
                helper = Path(r"C:\Program Files\mitmproxy\bin\mitmdump.exe")
            if not addon.is_file():
                addon = Path(__file__).resolve().parents[1] / "infrastructure" / "capture" / "addon.py"
        return RuntimeOptions(
            helper_path=helper, addon_path=addon, scoped_proxy=True,
            allowed_hosts=("hlxyxws.huanle.qq.com", "hlsjwshall.huanle.qq.com",
                           "long.wxagame.weixin.qq.com", "guandangamelog.huanle.qq.com"),
            require_certificate=True,
        )

    def _build_runtime(self) -> Any:
        if self._runtime_factory is None:
            raise RuntimeError("runtime composition is unavailable")
        options = self._mode_options()
        paths = RuntimePaths.from_mapping(self.paths)
        diagnostics = self._diagnostics_factory() if self._diagnostics_factory is not None else None
        return self._runtime_factory(
            paths, on_frame=self._on_frame, on_status=self._on_runtime_status,
            options=options, diagnostics=diagnostics)

    def _ensure_certificate_material(self) -> None:
        """Start the helper briefly so mitmproxy creates its per-install CA.

        This never changes Windows proxy settings. Trust is installed only later
        by RuntimeService after the user has explicitly consented.
        """
        options = self._mode_options()
        helper = options.helper_path
        if helper is None or not helper.is_file():
            raise RuntimeError("采集组件未安装")
        conf = self.paths["cert_dir"]
        conf.mkdir(parents=True, exist_ok=True)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        process = subprocess.Popen([str(helper), "--mode", "regular", "--listen-host", "127.0.0.1",
                                    "--listen-port", "18080", "--set", f"confdir={conf}",
                                    "--quiet"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=flags)
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                cert = conf / "mitmproxy-ca-cert.cer"
                if cert.is_file():
                    return
                if process.poll() is not None:
                    raise RuntimeError("采集组件无法生成本机证书")
                time.sleep(.1)
            raise RuntimeError("本机证书准备超时")
        finally:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=2)
                except subprocess.TimeoutExpired: process.kill()

    @staticmethod
    def _certificate_summary(state: str, message: str = "") -> str:
        """Translate certificate lifecycle states into actionable UI text."""
        state = str(state or "unknown").lower()
        if state in {"trusted", "ready", "installed", "already_trusted", "ok"}:
            return "本机网络组件已授权"
        if state == "missing":
            return "尚未生成本机网络证书，点击“开始使用”完成准备"
        if state in {"untrusted", "error"}:
            return "本机证书未被当前 Windows 用户信任，请重新授权或查看诊断"
        if state in {"consent_required", "unknown"}:
            return "首次使用需要你确认授权"
        return message or "本机网络组件状态异常，请打开诊断"

    def refresh_preflight(self) -> None:
        """Refresh side-effect-free certificate state for the first-use UI."""
        try:
            if self._certificate_inspector is None:
                return
            status = self._certificate_inspector(self.paths["cert_dir"])
            state = str(getattr(status, "state", "unknown"))
            message = str(getattr(status, "message", ""))
            self._status({
                "certificate_state": state,
                "certificate_summary": self._certificate_summary(state, message),
                "certificate_fingerprint": str(getattr(status, "fingerprint", "")),
            })
        except Exception as exc:
            self.log.write("certificate_preflight_failed", level="ERROR", message=type(exc).__name__)
            self._status({
                "certificate_state": "error",
                "certificate_summary": "无法检查本机网络证书，请打开诊断",
                "error_message": "证书状态检查失败",
            })

    def authorize(self, consent: bool) -> None:
        if not consent:
            self._status({"phase":"idle","busy":False,"title":"尚未连接","detail":"未授权，网络设置没有修改。"})
            return
        self._run_async("certificate", self._authorize_impl, consent)

    def _authorize_impl(self, consent: bool) -> None:
        self._status({"phase":"preparing","busy":True,"title":"正在准备","detail":"正在准备本机网络组件，请稍候。"})
        try:
            self._ensure_certificate_material()
            if self._runtime is None:
                self._runtime=self._build_runtime()
            result=self._runtime.prepare_certificate(bool(consent))
            certificate = result.get("certificate") or {}
            certificate_state = str(certificate.get("state", result.get("state", "unknown")))
            certificate_message = str(certificate.get("message", result.get("message", "")))
            certificate_data = {
                "certificate_state": certificate_state,
                "certificate_summary": self._certificate_summary(certificate_state, certificate_message),
                "certificate_fingerprint": str(certificate.get("fingerprint", "")),
            }
            if not result.get("ok"):
                self._status({"phase":"error","busy":False,
                              "error_message":result.get("message","证书初始化失败"),
                              "detail":self._certificate_summary(certificate_state, certificate_message),
                              **certificate_data})
                return
            self._status(certificate_data)
            self._start_impl()
        except Exception as exc:
            self.log.write("certificate_prepare_failed", level="ERROR", message=type(exc).__name__)
            self._status({"phase":"error","busy":False,"error_message":"网络组件初始化失败","detail":"请打开诊断查看处理建议。"})
    def start(self) -> None:
        self._run_async("start", self._start_impl)

    def _start_impl(self) -> None:
        self._status({"phase":"preparing","busy":True,"title":"正在准备","detail":"正在启动对局采集。"})
        try:
            # A new capture run starts a fresh transport/session boundary. This
            # is also the explicit recovery path after an active reconnect or
            # protocol error has put the previous engine into fail-closed mode.
            with self._lock:
                self._close_training_recorder()
                self._training_failed = False
                self._training_stopped = False
                self.session.reset()
                self._last_frame_received_at = None
                self._clear_recommendation_state_locked()
                self._table_game_key = None
                self._table_seat_confirmed = False
                self.viewmodel.apply_table_state({})
            self.viewmodel.apply_observation({})
            self._current_game_id = ""
            self._primary_engine_error = None
            self._engine_error_repeats = 0
            self._model.invalidate()
            self.viewmodel.clear_advice("新的采集会话已开始，等待新局。")
            if not self._capture_port_available():
                self._close_training_recorder()
                self._status({"phase":"error","running":False,"connected":False,"busy":False,
                              "error_message":"采集端口 18080 已被占用",
                              "detail":"请关闭旧的掼策/mitmdump 进程后再开始使用。"})
                return
            self._open_debug_recorder()
            if self._runtime is None:self._runtime=self._build_runtime()
            result=self._runtime.start()
            if result.get("ok"):
                self._ensure_training_recorder()
                if isinstance(self._model, ModelService) and self._model.status.get("model_status") == "closed":
                    self._create_model_service()
                if not self._selection_failed and not self._selection_in_progress \
                        and not getattr(self, '_no_selection', False):
                    self._model.start()
                self._status({"phase":"waiting","running":True,"connected":True,"busy":False,"network_mode":result.get("mode","direct"),"detail":"采集已启动。请关闭当前大掼蛋小程序，重新打开大掼蛋并进入“经典玩法”实际对局；只停留在首页或大厅不会产生局面。"})
            else:
                self._close_debug_recorder()
                self._close_training_recorder()
                self._status({"phase":"error","running":False,"connected":False,"busy":False,"error_message":result.get("message","采集启动失败"),"detail":"当前无法安全启动采集。"})
        except Exception as exc:
            self._close_debug_recorder()
            self._close_training_recorder()
            self.log.write("runtime_start_failed", level="ERROR", message=type(exc).__name__)
            self._status({"phase":"error","running":False,"connected":False,"busy":False,"error_message":"采集服务启动失败","detail":"请打开诊断。"})
    def stop(self) -> None:
        self._run_async("stop", self._stop_impl)

    def _stop_impl(self, recovery_message: str = "") -> None:
        with self._lock:
            self._selection_generation += 1
            generation = self._selection_generation
            self._model_ui_sequence = 0
            if self._selection_in_progress:
                self._selection_failed = True
            self._active_advice_key = None
        self.viewmodel.set_model_selection_generation(generation)
        result = self._runtime.stop() if self._runtime is not None else {"ok": True}
        with self._lock:
            self._training_stopped = True
            self._close_debug_recorder()
            self.session.reset()
            self._last_frame_received_at = None
            self._clear_recommendation_state_locked()
            self._table_game_key = None
            self._table_seat_confirmed = False
            self.viewmodel.apply_table_state({})
            self.viewmodel.apply_observation({})
            self._current_game_id = ""
            self._primary_engine_error = None
            self._engine_error_repeats = 0
            self._model.invalidate()
        self._model.close()
        self._close_training_recorder()
        status = {"running": False, "connected": False, "busy": False}
        if result.get("ok"):
            status.update(phase="stopped", error_message="", error_code="")
            if recovery_message:
                status.update(detail=recovery_message)
        else:
            status.update(phase="error", error_code=result.get("error_code", "CAPTURE_STOP_FAILED"),
                          error_message=result.get("message", "停止采集或恢复网络失败"))
        self._status(status)

    def _run_async(self, operation: str, callback, *args) -> None:
        if not self._operation_lock.acquire(blocking=False):
            self._status({"phase":"error","busy":False,"error_message":"已有操作正在执行","detail":"请稍候再试。"})
            return
        def runner() -> None:
            try:
                callback(*args)
            finally:
                self._operation_lock.release()
        threading.Thread(target=runner, name=f"guance-{operation}", daemon=True).start()

    def repair(self) -> None:
        result=self._runtime.repair() if self._runtime else {"ok":True}
        self._status({"phase":"idle" if result.get("ok") else "error","detail":result.get("message","修复完成"),"error_message":"" if result.get("ok") else result.get("message","")})

    def remove_certificate(self, consent: bool) -> None:
        result=self._runtime.remove_certificate(consent) if self._runtime else {"ok":False,"message":"助手尚未初始化"}
        certificate = result.get("certificate") or {}
        state = str(certificate.get("state", "missing" if result.get("ok") else "error"))
        self._status({"phase":"idle" if result.get("ok") else "error",
                      "detail":result.get("message", "证书处理完成"),
                      "certificate_state":state,
                      "certificate_summary":self._certificate_summary(state, str(result.get("message", ""))),
                      "certificate_fingerprint":str(certificate.get("fingerprint", "")),
                      "error_message":"" if result.get("ok") else result.get("message", "证书处理失败")})

    @property
    def requested_source(self):
        return self._requested_source

    @property
    def persisted_source(self):
        return self._persisted_source

    @property
    def active_source(self):
        return self._model.active_source if isinstance(self._model, ModelService) else None

    @property
    def selection_generation(self) -> int:
        return self._selection_generation

    def _model_artifact_identity(self) -> tuple[str, str] | None:
        """Selected MARVEL model artifact identity for decision/cache binding."""
        model = self._model
        if not isinstance(model, ModelService) or model.status.get("model_status") != "ready":
            return None
        metadata = dict(model.status.metadata)
        if metadata.get("provider") != "guandan_marvel_v2":
            return None
        artifact_id = str(metadata.get("model_version", ""))
        artifact_sha = str(metadata.get("model_sha256", ""))
        if (not artifact_id or len(artifact_id) > 128
                or not all(c.isalnum() or c in "._:-" for c in artifact_id)
                or len(artifact_sha) != 64
                or any(c not in "0123456789abcdef" for c in artifact_sha)):
            return None
        return artifact_id, artifact_sha

    def _marvel_confirmed_active(self) -> bool:
        return (self._decision_projector is not None
                and self._model_artifact_identity() is not None)

    def _maybe_submit_confirmed(self, decision: Any) -> None:
        """Submit the confirmed-decision request for the current MARVEL turn only."""
        if not self._marvel_confirmed_active():
            return
        identity = decision.public_state.identity
        key = (identity.session_id, identity.game_id, identity.state_version)
        latest = (self._latest_model_observation.key
                  if self._latest_model_observation is not None else None)
        with self._lock:
            pending = self._pending_confirmed_key
            active = self._active_advice_key
        if pending is None or key != pending or latest != key or active != key:
            return
        model_identity = self._model_artifact_identity()
        if model_identity is None:
            return
        try:
            payload = self._decision_projector(decision, model_identity[0], model_identity[1])
            with self._lock:
                if self._controller_closed or key != self._active_advice_key:
                    return
                self._pending_confirmed_key = None
            self._model.submit_confirmed(payload, timeout_seconds=120.0)
        except Exception:
            self._clear_pending_model_requests()
            self._model.invalidate()
            self.viewmodel.clear_advice("严格模型输入不可用，推荐已暂停。")

    def _drain_live_decisions(self) -> None:
        """Drain provider decisions every frame; evidence recording is optional.

        Recommendation submission never depends on debug-capture recording:
        the canonical capture converter only records evidence when enabled.
        Deferred trusted confirmations are flushed first (they can arrive on
        a frame without an observation update).
        """
        self._flush_deferred_advice()
        for decision in self._live_subset.drain_decisions():
            if self._canonical_converter is not None:
                try:
                    self._canonical_converter.observe_decision_opportunity(
                        frame_sequence=decision.frame_sequence,
                        state_version=decision.public_state.identity.state_version,
                        public_action_index=len(decision.public_state.history.actions),
                    )
                    self._canonical_converter.record_decision(decision)
                except Exception as exc:
                    self._canonical_converter.block(getattr(exc, "code", "DECISION_WRITE_FAILED"))
                    self._clear_pending_model_requests()
                    self._model.invalidate()
                    self.viewmodel.clear_advice("严格模型输入不可用，推荐已暂停。")
                    continue
            self._maybe_submit_confirmed(decision)
        self._flush_confirmed_pending()

    def _flush_confirmed_pending(self) -> None:
        """Fail closed when an eligible MARVEL turn produced no confirmed decision."""
        with self._lock:
            if self._pending_confirmed_key is None:
                return
            self._clear_recommendation_state_locked()
        self._model.invalidate()
        self.viewmodel.clear_advice("严格模型输入不可用，推荐已暂停。")

    def _requested_model_device(self) -> str:
        """Explicit device preference; environment override is operator-scoped."""
        env_value = os.environ.get("GUANCE_MODEL_DEVICE", "")
        for value in (env_value, self.preferences.get("model_device", "auto")):
            if isinstance(value, str) and value in ("auto", "cpu", "cuda"):
                return value
        return "auto"

    def _new_model_service(self, generation: int) -> ModelService:
        timeout = self.preferences.get('advice_timeout_ms', 2000)
        threads = self.preferences.get('inference_threads', 2)
        timeout = timeout if type(timeout) in (int, float) and 0 < timeout <= 60000 else 2000
        threads = threads if type(threads) is int and 1 <= threads <= 4 else 2
        return ModelService(on_result=lambda value: self._on_advice(value, generation=generation),
            on_status=lambda value: self._on_model_status(value, generation=generation),
            client_factory=self._model_client_factory,
            timeout_seconds=timeout / 1000, threads=threads,
            requested_device=self._requested_model_device())

    def _create_model_service(self) -> ModelService:
        # Restart is session-owned: never re-read an old preference/default after
        # a failed explicit selection. Explicit import is the only retry path.
        with self._lock:
            self._selection_generation += 1
            generation = self._selection_generation
            self._model_ui_sequence = 0
            service = self._new_model_service(generation)
            self._model = service
            blocked = self._selection_failed or self._selection_in_progress or self._controller_closed
            no_selection = self._no_selection
            failure_code = self._selection_failure_code
            requested = self._requested_source
        self.viewmodel.set_model_selection_generation(generation)
        if blocked:
            service.fail_selection(failure_code)
        elif no_selection:
            service.fail_selection('MODEL_NOT_SELECTED', state=ModelLifecycleState.UNAVAILABLE)
        else:
            service._select(requested, start=False)
        return service

    def _model_ui_payload(self, data: dict, generation: int) -> dict | None:
        with self._lock:
            if generation != self._selection_generation:
                return None
            self._model_ui_sequence += 1
            sequence = self._model_ui_sequence
        return {**data, '_model_selection_generation': generation,
                '_model_ui_sequence': sequence}

    def _model_ui(self, data: dict, generation: int) -> None:
        payload = self._model_ui_payload(data, generation)
        if payload is not None:
            self._status(payload)

    def _clear_model_advice(self, reason: str, generation: int) -> None:
        payload = self._model_ui_payload({'reason': reason}, generation)
        if payload is not None:
            self.viewmodel.clear_advice(reason, generation=generation,
                                        sequence=payload['_model_ui_sequence'])

    def _persist_selection_failure_locked(self, generation: int, requested_source: object) -> None:
        """Persist fail-closed intent while the selection commit lock is held."""
        with self._lock:
            if generation != self._selection_generation or self._controller_closed:
                return
            preferences = {**self.preferences,
                'model_selection_failed': True,
                'requested_model_path': str(requested_source)}
        try:
            save_preferences(self.paths, preferences)
        except Exception:
            return
        with self._lock:
            if generation == self._selection_generation and not self._controller_closed:
                self.preferences = preferences
                self._persisted_source = preferences.get('model_path', '')

    def _persist_selection_failure(self, generation: int, requested_source: object) -> None:
        """Persist fail-closed intent when storage is usable.

        If storage itself is unavailable the in-memory latch remains authoritative
        for this application session; no cross-process durability is claimed.
        """
        acquired = self._model_selection_lock.acquire(timeout=2.0)
        if not acquired:
            return
        try:
            self._persist_selection_failure_locked(generation, requested_source)
        finally:
            self._model_selection_lock.release()

    def import_model(self, path: str) -> None:
        # Empty dialog cancellation is not a confirmed selection.
        if path == '':
            return
        self._begin_selection(path, catalog_model_id=None)

    def select_catalog_model(self, model_id: str) -> None:
        """Explicit selection of one discovered catalog model; discovery itself never selects."""
        if not isinstance(model_id, str) or not model_id.strip():
            return
        if self._catalog is None:
            return
        self._begin_selection("", catalog_model_id=model_id.strip())

    def _resolve_catalog_selection(self, model_id: str) -> tuple[Path, ActiveSelectionDTO] | None:
        if self._catalog is None:
            return None
        try:
            scan = self._catalog.scan()
        except Exception:
            return None
        for entry in scan.entries:
            if entry.model_id == model_id:
                document = ActiveSelectionDTO(kind="catalog", model_id=entry.model_id,
                                              bundle=entry.directory,
                                              selected_at=self._selection_timestamp())
                return entry.source, document
        return None

    def _begin_selection(self, path: str, *, catalog_model_id: str | None) -> None:
        requested_label: object = catalog_model_id if catalog_model_id is not None else path
        with self._lock:
            if self._controller_closed:
                return
            self._selection_generation += 1
            generation = self._selection_generation
            self._model_ui_sequence = 0
            self._selection_failed = True
            self._selection_in_progress = True
            self._no_selection = False
            self._requested_source = requested_label
            self._active_advice_key = None
            self._pending_confirmed_key = None
            latest = self._latest_model_observation
            self._pending_model_observation = latest if (
                latest is not None and not self.session.paused
                and latest.current_seat == latest.my_seat) else None
            self._pending_model_generation = (generation
                                              if self._pending_model_observation is not None
                                              else None)
            old = self._model
            service = self._new_model_service(generation)
            self._model = service
        self.viewmodel.set_model_selection_generation(generation)
        self._clear_model_advice('模型已切换，等待校验。', generation)
        self._model_ui({'model_state': 'loading', 'model_metadata': {},
            'model_summary': '正在校验所选模型包', 'error_message': '',
            'detail': '正在校验并保存模型选择。'}, generation)
        # Service.close logically detaches first; no game-state lock is held here.
        old.close()
        if isinstance(old, ModelService) and old.cleanup_errors:
            service.fail_selection('MODEL_CLEANUP_FAILED')
            self._persist_selection_failure(generation, requested_label)
            with self._lock:
                if generation == self._selection_generation:
                    self._selection_in_progress = False
            self._clear_pending_model_requests()
            return
        if not self._model_selection_lock.acquire(timeout=2.0):
            service.fail_selection('MODEL_SELECTION_BUSY')
            self._persist_selection_failure(generation, requested_label)
            with self._lock:
                if generation == self._selection_generation:
                    self._selection_in_progress = False
            self._clear_pending_model_requests()
            return
        activated = False
        try:
            with self._lock:
                if generation != self._selection_generation or self._controller_closed:
                    return
            if catalog_model_id is not None:
                resolved = self._resolve_catalog_selection(catalog_model_id)
                if resolved is None:
                    service.fail_selection('CATALOG_MODEL_UNAVAILABLE')
                    self._persist_selection_failure_locked(generation, requested_label)
                    return
                selected, document = resolved
            else:
                try:
                    if not isinstance(path, (str, Path)) or not str(path).strip():
                        raise ValueError('invalid model source')
                    selected = Path(path).resolve()
                except Exception:
                    service.fail_selection('MODEL_SOURCE_INVALID')
                    self._persist_selection_failure(generation, path)
                    return
                document = ActiveSelectionDTO(kind="legacy", path=str(selected),
                                              selected_at=self._selection_timestamp())
            # The active document is staged first but only committed after the
            # preferences mirror succeeds; a failed later step restores the exact
            # prior bytes, and the commit marker keeps an unrestored state
            # fail-closed across restarts.
            active_snapshot: object | None = None
            if self._catalog is not None:
                try:
                    active_snapshot = self._catalog.stage_active(document)
                except Exception:
                    service.fail_selection('MODEL_PREFERENCES_FAILED')
                    return
            preferences = {**self.preferences, 'model_path': str(selected),
                'model_selection_failed': False, 'requested_model_path': str(selected)}
            try:
                save_preferences(self.paths, preferences)
            except Exception:
                if active_snapshot is not None:
                    self._catalog.restore_active(active_snapshot)
                service.fail_selection('MODEL_PREFERENCES_FAILED')
                return
            with self._lock:
                # The selection commit lock preserves write order. A superseded
                # writer records no in-memory identity and cannot activate; the
                # newest waiting writer commits next. Close also forbids revival.
                if generation != self._selection_generation or self._controller_closed:
                    return
                self._persisted_source = str(selected)
                self.preferences = preferences
                self._requested_source = selected
                self._selection_failed = False
                self._active_catalog_id = catalog_model_id if catalog_model_id is not None else ""
            if active_snapshot is not None:
                try:
                    self._catalog.commit_active()
                except Exception:
                    self._catalog.restore_active(active_snapshot)
                    service.fail_selection('MODEL_PREFERENCES_FAILED')
                    self._persist_selection_failure_locked(generation, requested_label)
                    return
            service.import_model(selected)
            # A real client loads asynchronously: `start()` returns while the
            # worker reports `loading`. Only a *terminal* non-ready outcome is a
            # selection failure. An in-progress load must never persist the
            # durable failure latch, otherwise a successful real provider load
            # would block the next launch (the ready/error callback decides).
            selected_state = service.status.get('model_status')
            activated = selected_state == 'ready'
            if not activated and selected_state in ('error', 'unavailable', 'timeout'):
                self._persist_selection_failure_locked(generation, selected)
        finally:
            with self._lock:
                if generation == self._selection_generation:
                    self._selection_in_progress = False
            self._model_selection_lock.release()
        pending = None
        pending_generation = None
        if activated:
            with self._lock:
                if generation == self._selection_generation and not self._controller_closed:
                    pending, self._pending_model_observation = self._pending_model_observation, None
                    pending_generation, self._pending_model_generation = \
                        self._pending_model_generation, None
        else:
            with self._lock:
                if generation == self._selection_generation:
                    self._pending_model_observation = None
                    self._pending_model_generation = None
            if generation == self._selection_generation:
                self._clear_pending_model_requests()
        if pending is not None and pending_generation == generation:
            with self._lock:
                submit = (generation == self._selection_generation
                          and self._model is service and not self._controller_closed
                          and not self._selection_failed)
                confirmed_path = False
                if submit:
                    self._active_advice_key = pending.key
                    # MARVEL never replays a public turn: wait for the new
                    # generation's matching confirmed decision (same key) instead.
                    if self._marvel_confirmed_active():
                        self._pending_confirmed_key = pending.key
                        confirmed_path = True
            if submit:
                self._model_ui({'phase': 'thinking', 'connected': True, 'running': True}, generation)
                if not confirmed_path:
                    try:
                        service.submit(pending)
                    except Exception:
                        # A concurrent close/new selection already revoked delivery.
                        pass
        self._refresh_catalog_ui()

    def _diagnostic_snapshot(self) -> dict[str, Any]:
        """Build a bounded, metadata-only diagnostic snapshot.

        This intentionally excludes hand/card payloads, capture payloads, and
        model files. The session summary contains only public counts and state
        metadata needed to diagnose synchronization failures.
        """
        runtime_status: dict[str, Any]
        if self._runtime is None:
            runtime_status = {"state": "uninitialized"}
        else:
            try:
                inspected = self._runtime.inspect()
                runtime_status = inspected if isinstance(inspected, dict) else {"state": str(inspected)}
            except Exception as exc:
                runtime_status = {"state": "inspect_failed", "error": type(exc).__name__}

        vm = self.viewmodel
        vm_logs = []
        for item in list(getattr(vm, "logs", []) or [])[-50:]:
            if isinstance(item, dict):
                vm_logs.append({
                    "time": str(item.get("time", "")),
                    "level": str(item.get("level", "INFO")),
                    "code": str(item.get("code", "")),
                    "message": str(item.get("message", "")),
                })
        vm_diagnostics = []
        for item in list(getattr(vm, "diagnostics", []) or [])[-40:]:
            if isinstance(item, dict):
                vm_diagnostics.append({
                    "label": str(item.get("label", "")),
                    "value": str(item.get("value", "")),
                })

        session_state = self.session.state
        session_summary: dict[str, Any] = {
            "paused": bool(self.session.paused),
            "paused_reason": getattr(self.session, "_paused_reason", ""),
            "state_version": None,
            "primary_engine_error": dict(self._primary_engine_error) if self._primary_engine_error else None,
            "engine_error_repeats": self._engine_error_repeats,
        }
        if session_state is not None:
            snapshot = session_state.snapshot()
            session_summary.update({
                "game_id": snapshot.game_id,
                "phase": snapshot.phase,
                "state_version": snapshot.state_version,
                "level": snapshot.level,
                "my_seat": snapshot.my_seat,
                "first_seat": snapshot.first_seat,
                "current_seat": snapshot.current_seat,
                "remaining_counts": list(snapshot.remaining),
                "history_length": len(snapshot.history),
                "trick": snapshot.trick,
                "free_lead": snapshot.free_lead,
            })

        capture_stats = dict(self._last_debug_capture_stats)
        if self._debug_recorder is not None:
            capture_stats = self._debug_recorder.stats
        return {
            "runtime": runtime_status,
            "view_model": {
                "phase": str(getattr(vm, "phase", "")),
                "status_title": str(getattr(vm, "statusTitle", "")),
                "status_detail": str(getattr(vm, "statusDetail", "")),
                "last_error": str(getattr(vm, "lastError", "")),
                "diagnostics": vm_diagnostics,
                "recent_logs": vm_logs,
            },
            "session": session_summary,
            "debug_capture": capture_stats,
            "training_data": self._training_stats(),
        }

    def export_diagnostics(self) -> None:
        destination = self.paths["exports_dir"] / f"diagnostics-{time.time_ns()}.zip"
        temporary = destination.with_suffix(".tmp.zip")
        try:
            snapshot = self._diagnostic_snapshot()
            self.log.export(temporary, status=snapshot)
            temporary.replace(destination)
        except Exception as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            self.log.write(
                "diagnostics_export_failed",
                level="ERROR",
                message=type(exc).__name__,
            )
            self.viewmodel.append_log({
                "level": "ERROR",
                "code": "DIAGNOSTICS_EXPORT_FAILED",
                "message": f"诊断包导出失败：{type(exc).__name__}",
            })
            return

        self.log.write(
            "diagnostics_exported",
            message="诊断包已生成",
            path=str(destination),
        )
        self.viewmodel.append_log({
            "level": "INFO",
            "code": "DIAGNOSTICS_EXPORTED",
            "message": f"诊断包已生成：{destination}",
        })

    def _on_frame(self, frame: SourceFrame) -> None:
        # The debug file is the source of truth for this MVP: persist first,
        # then allow the parser to consume the frame. Serialize recorder close
        # and session.feed with this callback so stop cannot close the stream
        # while a capture thread is appending to it.
        with self._lock:
            self._last_frame_received_at = float(frame.received_at)
            if self._debug_recorder is not None:
                try:
                    self._debug_recorder.append(frame)
                except DebugCaptureError as exc:
                    self._debug_capture_failed = True
                    self._training_invalidate("capture_error")
                    self._close_debug_recorder()
                    self._clear_recommendation_state_locked()
                    self._model.invalidate()
                    self.viewmodel.clear_advice("调试报文保存失败，推荐已暂停。")
                    self._status({"phase":"error","error_message":str(exc),"detail":"调试报文未能保存，已暂停继续解析。"})
                    return
            if self._debug_capture_failed:
                return
            updates = self.session.feed(frame)
            for update in updates:
                if self._canonical_converter is not None:
                    self._canonical_converter.observe_engine_update(update)
                self._handle_update(update)
            self._drain_live_decisions()

    def _handle_update(self, update: EngineUpdate) -> None:
        self._training_update(update)
        if update.kind == "error":
            with self._lock:
                self._clear_recommendation_state_locked()
            self._model.invalidate()
            self.viewmodel.clear_advice("局面无法安全同步，推荐已暂停。")
            if update.error_code == "engine_paused" and self._primary_engine_error is not None:
                self._engine_error_repeats += 1
                return
            self._primary_engine_error = {
                "code": update.error_code or "ENGINE_UPDATE_ERROR",
                "message": update.message,
                "game_id": self._current_game_id,
            }
            self.log.write(
                "engine_update_error",
                level="ERROR",
                message=update.message,
                error_code=update.error_code,
                game_id=self._current_game_id,
            )
            self.viewmodel.append_log({
                "level": "ERROR",
                "code": update.error_code or "ENGINE_UPDATE_ERROR",
                "message": update.message,
            })
            self._status({
                "phase": "error",
                "error_message": update.message,
                "error_code": update.error_code,
                "detail": "局面无法安全同步，推荐已暂停。",
                "game_id": self._current_game_id,
                "diagnostics": {
                    "primary_error": update.error_code or "ENGINE_UPDATE_ERROR",
                    "engine_error_repeats": self._engine_error_repeats,
                },
            })
            return
        if update.kind == "state":
            # Display state is independent from recommendation eligibility. A
            # freshly dealt hand and public plays are safe to show even while
            # the observer seat is unknown. Informational 3002/3003 updates
            # must not invalidate a valid recommendation because they do not
            # advance the game observation/state version.
            table = update.data.get("table")
            if isinstance(table, dict):
                self.viewmodel.apply_table_state(table)
                session_id = str(table.get("session_id", ""))
                table_game_id = str(table.get("game_id", update.data.get("game_id", "")))
                self._table_game_key = (session_id, table_game_id) if table_game_id else None
                try:
                    table_seat = int(table.get("my_seat", -1))
                except (TypeError, ValueError):
                    table_seat = -1
                self._table_seat_confirmed = bool(table.get("seat_confirmed")) and table_seat in range(4)
            state_phase = str(update.data.get("phase", ""))
            game_id = str(update.data.get("game_id", ""))
            source_sequence = int(update.data.get("source_sequence", update.data.get("state_version", 0)) or 0)
            is_information = update.message == "information"
            if game_id and game_id != self._current_game_id:
                self._current_game_id = game_id
                self._primary_engine_error = None
                self._engine_error_repeats = 0
                with self._lock:
                    self._clear_recommendation_state_locked()
                self._model.invalidate()
                self.viewmodel.clear_advice("局面已切换，等待新局推荐。")
            if not is_information:
                with self._lock:
                    self._clear_recommendation_state_locked()
                self._model.invalidate()
            if state_phase == "waiting_start" and game_id and self._debug_recorder is not None:
                self._debug_recorder.begin_game(game_id, source_sequence)
            if state_phase in {"ended", "settled"}:
                self._model.invalidate()
                self._clear_pending_model_requests()
                if self._debug_recorder is not None and game_id:
                    self._debug_recorder.finish_game(game_id, source_sequence, state_phase)
                self.viewmodel.clear_advice("本局已结束，等待下一局。")
                ui_phase = state_phase
                detail = "本局已结算，等待下一局。" if state_phase == "settled" else "本局已结束，等待结算或下一局。"
            elif state_phase == "active" and int(update.data.get("my_seat", -1)) not in range(4):
                ui_phase = "confirming_seat"
                detail = "已收到牌局；等待首次实际出牌确认座位，确认前不推荐。"
            else:
                ui_phase = "ready" if state_phase == "active" else "waiting_start"
                detail = "局面已同步。" if state_phase == "active" else "已收到发牌，等待开局；座位确认前不推荐。"
            status_data = {
                "connected": True,
                "running": True,
                "game_id": game_id,
                "state_phase": state_phase,
                "state_version": update.data.get("state_version", 0),
                "diagnostics": {
                    "game_id": game_id,
                    "state_version": update.data.get("state_version", 0),
                    "座位": "已确认" if update.data.get("my_seat", -1) in range(4) else "待首次实际出牌确认",
                },
            }
            if not is_information or state_phase in {"ended", "settled"}:
                status_data.update({"phase": ui_phase, "detail": detail})
            self._status(status_data)
        if update.kind == "observation" and update.observation:
            observation = update.observation
            self._latest_model_observation = observation
            self.viewmodel.apply_observation(observation.to_dict())
            table_matches = (
                self._table_game_key is None
                or self._table_game_key[1] == observation.game_id
            )
            seat_confirmed = (
                observation.my_seat in range(4)
                and (self._table_game_key is None or self._table_seat_confirmed)
            )
            eligible = (not self.session.paused and table_matches and seat_confirmed
                        and observation.current_seat == observation.my_seat)
            if eligible and getattr(self, '_selection_in_progress', False):
                self._pending_model_observation = observation
                with self._lock:
                    self._pending_model_generation = self._selection_generation
            elif (eligible and not getattr(self, '_selection_failed', False)
                  and not getattr(self, '_no_selection', False)):
                self._active_advice_key = observation.key
                if self._marvel_confirmed_active():
                    self._status({'phase': 'thinking', 'connected': True, 'running': True})
                    # Single-path MARVEL turn: the matching confirmed decision is
                    # submitted from this frame's decision drain; never a second
                    # public-view request for the same turn.
                    with self._lock:
                        self._pending_confirmed_key = observation.key
                else:
                    self._submit_public_advice(observation)

    def _declared_facts_consumer_active(self) -> bool:
        """True when the ready model consumes declared facts (DanKS family)."""
        model = self._model
        if not isinstance(model, ModelService) or model.status.get("model_status") != "ready":
            return False
        metadata = dict(model.status.metadata)
        return metadata.get("provider") == "danks_v3"

    def _submit_public_advice(self, observation) -> None:
        """Public-view submission with verified facts when trusted replay confirms.

        The DanKS family reaches the model through the trusted-replay
        projection: the provider's confirmed public state for this exact turn
        is verified item by item and only the verified facts are projected
        (normalized canonical card order).  One submission per turn identity,
        ever: a repeated observation update or a duplicate confirmation for an
        already-submitted turn never re-submits.

        Without a verified match the evidence is INSUFFICIENT: clear the
        current advice, record the reason, and register the turn for a
        deferred confirmation (the trusted decision may arrive on a later
        frame).  No ineligible request is submitted and nothing latches - the
        model keeps handling later positions.  Real model failures keep the
        original status handling.  Non-DanKS families keep the verbatim
        submission behavior unchanged.
        """
        key = observation.key
        if key in self._advice_submitted:
            return  # already answered once; duplicates never re-submit
        budget = 6.0 if observation.free_lead else 2.0
        if self._declared_facts_consumer_active():
            verified = project_trusted_replay_facts(observation, self._live_subset)
            if verified is not None:
                self._submit_danks_advice(observation, verified, budget)
                return
            if key not in self._advice_pending:
                self._advice_pending[key] = int(getattr(observation, "state_version", 0))
                self._record_advice_deferred(observation)
            return
        if isinstance(self._model, ModelService) and self._model.selected_provider == "danks_v3":
            self._advice_pending[key] = None
            self.viewmodel.clear_advice("DanKS 正在加载，已记录当前回合。")
            return
        self._status({"phase": "thinking", "connected": True, "running": True})
        self._model.submit(observation,
                           declared_facts=self._declared_facts_for(observation),
                           budget_seconds=budget)

    def _submit_danks_advice(self, observation, verified, budget: float) -> None:
        self._advice_submitted.add(observation.key)
        self._advice_pending.pop(observation.key, None)
        self._status({"phase": "thinking", "connected": True, "running": True})
        self.log.write("advice_submitted", game_id=observation.game_id,
                       state_version=observation.state_version)
        view, facts = verified
        self._model.submit(view, declared_facts=facts, budget_seconds=budget)

    def _record_advice_deferred(self, observation) -> None:
        """Clear the displayed advice and record why it was withheld."""
        code = getattr(self._live_subset, "danks_defer_code", "")
        reason = {
            "LIVE_DECLARATION_AMBIGUOUS": "本局有出牌的牌型无法唯一确认，建议暂缓。",
            "LIVE_PLAY_DOES_NOT_BEAT": "本局有出牌与已确认的压制关系冲突，建议暂缓。",
        }.get(code, "本回合公开证据不足，正在等待确认。")
        self.viewmodel.clear_advice(reason)
        self.log.write(
            "advice_deferred_insufficient_evidence",
            message=reason,
            game_id=observation.game_id,
            state_version=observation.state_version,
            error_code=code,
        )

    def _turn_still_eligible(self, observation) -> bool:
        """Mirror the observation-update gate for a deferred confirmation."""
        table_matches = (
            self._table_game_key is None
            or self._table_game_key[1] == observation.game_id
        )
        seat_confirmed = (
            observation.my_seat in range(4)
            and (self._table_game_key is None or self._table_seat_confirmed)
        )
        return (not self.session.paused and table_matches and seat_confirmed
                and observation.current_seat == observation.my_seat)

    def _flush_deferred_advice(self) -> None:
        """Submit pending turns exactly once when their confirmation arrives.

        Runs every frame before the decision drain: a trusted confirmation
        queued on a later frame than the observation update (no observation
        update fires for it) still produces exactly one advice, provided the
        turn is still the observer's and the advice key is still active.
        Expired turns (state moved on, no longer my seat, or no longer the
        active advice key) are dropped WITHOUT advice; duplicate
        confirmations for already-submitted identities never re-submit.
        """
        if not self._advice_pending or not self._declared_facts_consumer_active():
            return
        latest = self._latest_model_observation
        latest_game = latest.game_id if latest is not None else None
        for key, version in list(self._advice_pending.items()):
            if latest_game is not None and key[1] != latest_game:
                # A previous game ended while its turn was pending.
                self._advice_pending.pop(key, None)
                continue
            if (latest is None or latest.key != key
                    or not self._turn_still_eligible(latest)
                    or self._active_advice_key != key
                    or key in self._advice_submitted):
                self._advice_pending.pop(key, None)
                continue
            verified = project_trusted_replay_facts(latest, self._live_subset)
            if verified is None:
                if version is None:
                    self._advice_pending[key] = latest.state_version
                    self._record_advice_deferred(latest)
                continue  # confirmation not arrived yet; keep waiting
            budget = 6.0 if latest.free_lead else 2.0
            self._submit_danks_advice(latest, verified, budget)
        # Housekeeping: submitted identities of finished games are never
        # revisited; drop them so the set stays bounded.
        if latest_game is not None:
            self._advice_submitted = {key for key in self._advice_submitted
                                      if key[1] == latest_game}

    def _declared_facts_for(self, observation) -> 'DeclaredFacts | None':
        """Declaration facts from the session state for the pending view.

        The state's own current_winning_declaration Knowledge is projected
        verbatim (known/absent/unsupported/unknown); nothing is inferred from
        zero/False, and a missing or foreign state yields no facts so the
        provider applies its own eligibility gate.
        """
        from guance.contracts import (CurrentWinningDeclarationFact,
                                      DECLARED_FACTS_SCHEMA, DeclaredFacts)
        from guance.domain.state import GameState
        state = self.session.state
        if not isinstance(state, GameState) or                 state.game_id != observation.game_id:
            return None
        winning = state.current_winning_declaration
        if winning is None:
            status = 'absent' if observation.free_lead else 'unsupported'
            fact = CurrentWinningDeclarationFact(
                status=status,
                reason='' if status == 'absent' else 'live_declaration_unverified')
        elif winning.status.value == 'known':
            fact = CurrentWinningDeclarationFact(status='known',
                                                 declaration=winning.value)
        elif winning.status.value == 'absent':
            fact = CurrentWinningDeclarationFact(status='absent')
        else:
            fact = CurrentWinningDeclarationFact(status=winning.status.value,
                                                 reason=winning.reason)
        # History provenance is projected verbatim from the state machine; the
        # machine keeps it unsupported for everything it can observe today
        # (phase 1 finding), so live states stay ineligible until an upstream
        # evidence source exists.
        try:
            v2 = state.public_state_v2(self.session.session_id)
            history = v2.history
            transport_status = history.transport_contiguous.status.value
            transport_value = (history.transport_contiguous.value
                               if history.transport_contiguous.status.value == 'known'
                               else None)
            complete_status = history.public_history_complete.status.value
            complete_value = (history.public_history_complete.value
                              if history.public_history_complete.status.value == 'known'
                              else None)
        except Exception:
            transport_status, transport_value = 'unsupported', None
            complete_status, complete_value = 'unsupported', None
        # Project index-aligned history declarations from the state machine's
        # committed actions (verbatim: known declarations carry the full
        # PublicDeclaration; passes are absent; unsupported stays explicit).
        from guance.contracts import ActionDeclarationFact
        history_declarations = []
        for order, action in enumerate(state.committed_actions_v2, 1):
            decl = action.declaration
            if decl.status.value == 'known':
                history_declarations.append(ActionDeclarationFact(
                    index=order, status='known', declaration=decl.value))
            elif decl.status.value == 'absent':
                history_declarations.append(ActionDeclarationFact(
                    index=order, status='absent'))
            else:
                history_declarations.append(ActionDeclarationFact(
                    index=order, status=decl.status.value, reason=decl.reason))
        return DeclaredFacts(schema=DECLARED_FACTS_SCHEMA,
                             current_winning_declaration=fact,
                             history_declarations=history_declarations,
                             history_origin='live_capture',
                             transport_contiguous=transport_status,
                             public_history_complete=complete_status,
                             history_complete_value=complete_value,
                             transport_value=transport_value)

    def _on_advice(self, result: CanonicalAdviceResult | dict, *, generation: int | None = None) -> None:
        if generation is not None and (generation != self._selection_generation or self._selection_failed):
            return
        # DTOs stay typed until this presentation boundary. Keep a narrow legacy
        # mapping path for existing injected test/adapters.
        wire = result.to_dict() if isinstance(result, CanonicalAdviceResult) else result
        if not isinstance(wire, dict):
            return
        # A delayed callback must not resurrect the previous turn/game or
        # replace "confirming seat"/terminal/error status with "ready".
        with self._lock:
            key = (wire.get("session_id"), wire.get("game_id"), wire.get("state_version"))
            if self._active_advice_key is None or key != self._active_advice_key or self.session.paused:
                return
            if self._table_game_key is not None and key[1] != self._table_game_key[1]:
                return
            if generation is not None and generation != self._selection_generation:
                return
            if generation is not None:
                wire = self._model_ui_payload(wire, generation)
                if wire is None:
                    return
            if wire.get("provider") == "danks_v3" and wire.get("status") != "ready":
                messages = {
                    "MODEL_TIMEOUT": "本回合推理超时，等待下一回合。",
                    "MODEL_WORKER_EXITED": "DanKS 进程已退出，下个本人回合会重新启动模型。",
                    "MODEL_INFERENCE_FAILED": "DanKS 本回合计算失败，下个本人回合会继续计算。",
                    "MODEL_OUTPUT_INVALID": "本回合候选格式或牌面不合法，等待下一回合。",
                    "MODEL_OUTPUT_NOT_BEATING": "本回合候选未通过牌型校验，下个本人回合会继续计算。",
                    "MODEL_OUTPUT_AMBIGUOUS": "本回合牌型有歧义，下个本人回合会重新检查。",
                }
                wire = {**wire, "error_message": messages.get(
                    wire.get("error_code"), "本回合牌型或公开历史无法确认，建议暂缓。",
                )}
            self.viewmodel.apply_advice(wire)
            data = {'phase': 'ready', 'connected': True, 'running': True}
            if generation is None:
                self._status(data)
            else:
                self._model_ui(data, generation)
        if wire.get("provider") == "danks_v3":
            self.log.write("advice_result", game_id=key[1], state_version=key[2],
                           result_status=wire.get("status", ""), error_code=wire.get("error_code", ""))

    @staticmethod
    def _model_summary(metadata: dict[str, Any], state: str) -> str:
        if not isinstance(metadata, dict) or not metadata:
            return "模型状态：" + state
        parts = [str(metadata.get("provider", "model")),
                 str(metadata.get("backend", "")), str(metadata.get("runtime", "")),
                 str(metadata.get("model_version", ""))]
        parts = [part for part in parts if part]
        fingerprint = str(metadata.get("model_sha256", ""))
        if fingerprint:
            parts.append("sha256:" + fingerprint[:12])
        if metadata.get("experimental"):
            parts.append("experimental")
        if metadata.get("classification") == "evaluation_only":
            parts.append("evaluation-only")
        if metadata.get("test_only"):
            parts.append("test-only")
        if metadata.get("production_ready") is False:
            parts.append("not-production-ready")
        return " / ".join(parts) + "（" + state + "）"

    def _on_model_status(self, status: ModelStatusSnapshot | dict, *, generation: int | None = None) -> None:
        if status.get("event") == "stale_advice_dropped":
            return
        state = status.get("model_status")
        error_code = status.get("error_code", "")
        no_selection_event = state == "unavailable" and error_code == "MODEL_NOT_SELECTED"
        with self._lock:
            if generation is not None and generation != self._selection_generation:
                return
            ready_generation = self._ready_generation
            if generation is not None and state in {'error', 'unavailable', 'timeout'}:
                if no_selection_event:
                    requested = None
                else:
                    self._selection_failed = True
                    self._active_advice_key = None
                    # Only a load-phase failure (this generation never reached
                    # ready) persists the durable selection latch. A post-ready
                    # inference error stays in-session fail-closed and must not
                    # block the next launch.
                    requested = (self._requested_source
                                 if generation != ready_generation else None)
            else:
                requested = None
            if generation is not None and state == 'ready' and self._selection_failed:
                return
            if generation is not None and state == 'ready':
                self._ready_generation = generation
        metadata = status.get("metadata", {})
        publish = self._status if generation is None else lambda data: self._model_ui(data, generation)
        if no_selection_event:
            # Discovery never activates; an unselected model is explicitly
            # unavailable and must not become a persistent failure latch.
            publish({"model_state": "unavailable", "model_metadata": {},
                     "model_summary": "尚未选择模型；请在设置中从模型目录选择模型包。",
                     "error_code": "MODEL_NOT_SELECTED", "error_message": ""})
            return
        if state == "ready":
            ready = {"model_state": "ready", "model_summary": self._model_summary(metadata, "ready"),
                     "model_metadata": metadata, "error_message": ""}
            # A prior selection failure moved the presentation into an error
            # phase that deliberately rejects observations. Explicit retry is
            # the only recovery and must reopen observation admission without
            # claiming that a game is already synchronized.
            if generation is not None and getattr(self.viewmodel, "_phase", "") == "error":
                ready.update({"phase": "waiting", "detail": "模型已就绪，等待当前局面。"})
            publish(ready)
            with self._lock:
                if self._advice_pending:
                    self._flush_deferred_advice()
        elif state in {"error", "unavailable", "timeout"}:
            self.log.write("model_lifecycle_failed", level="ERROR", model_state=state,
                           error_code=error_code)
            self._clear_pending_model_requests()
            if generation is None and hasattr(self, '_model'):
                self._model.invalidate()
            if generation is None:
                self.viewmodel.clear_advice('模型不可用，推荐已暂停。')
            else:
                self._clear_model_advice('模型不可用，推荐已暂停。', generation)
                # An asynchronous load failure must persist the durable
                # fail-closed latch here: the selection transaction already
                # returned while the worker was still loading, so only this
                # terminal status callback can record that the explicit
                # selection failed.
                if requested is not None:
                    self._persist_selection_failure(generation, requested)
            update = {"model_state": state, "model_metadata": metadata,
                      "model_summary": self._model_summary(metadata, state or "unavailable"),
                      "error_code": status.get("error_code", ""),
                      "error_message": status.get("message", "模型异常")}
            if state in {"error", "timeout"}:
                update["phase"] = "error"
            publish(update)
        elif state in {"loading", "uninitialized", "closed"}:
            publish({"model_state": state, "model_metadata": {},
                          "model_summary": "正在加载本地模型" if state == "loading" else "尚未加载模型"})

    def _on_runtime_status(self, status: dict) -> None:
        if status.get("state") in {"helper_recovered", "network_settings_changed"}:
            self._run_async("runtime-recovery", self._stop_impl, str(status.get("message", "")))
        if status.get("state") in {"error","stop_error"}:
            self._training_invalidate("capture_error")
            self._model.invalidate()
            self._clear_pending_model_requests()
            self.viewmodel.clear_advice("采集异常，推荐已暂停。")
            self._status({"phase":"error","error_message":status.get("message","采集异常")})
        self.viewmodel.append_log({"level":"INFO","code":str(status.get("state","RUNTIME")),"message":str(status.get("message",status.get("state","")))})

    def _status(self, data: dict) -> None:
        self.viewmodel.apply_status(data)

    def close(self) -> None:
        """Synchronously release helper, proxy ownership, recorder and model."""
        with self._lock:
            self._controller_closed = True
        try:
            self._stop_impl()
        finally:
            self._close_training_recorder()
            self.log.close()
