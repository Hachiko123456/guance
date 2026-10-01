"""Qt-agnostic application controller connecting runtime, state, and model."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Any

from guance.config import (
    app_paths,
    default_model_path,
    ensure_app_paths,
    load_preferences,
    save_preferences,
)
from guance.contracts import EngineUpdate, SourceFrame
from guance.domain.session import SessionEngine
from guance.diagnostics import DiagnosticLog
from guance.infrastructure.capture.diagnostics import BoundedDiagnostics
from guance.infrastructure.model.client import InferenceClient
from guance.infrastructure.protocol.saved_capture import iter_game_frames
from guance.infrastructure.windows.certificate import WindowsCertificateManager
from guance.infrastructure.windows.proxy import WindowsProxyBackend
from guance.application.runtime.service import RuntimeOptions, RuntimeService
from guance.ports.capture import CaptureMode, RuntimePaths, UpstreamProxy


class DebugCaptureError(RuntimeError):
    """Raised when an enabled local debug capture cannot be written."""


class DebugFrameRecorder:
    """Persist validated source frames before the protocol parser sees them.

    This deliberately records only the already allow-listed WebSocket frames
    delivered by the capture supervisor. It never sees headers, cookies, or
    proxy credentials. The file is local-only and ignored by Git.
    """

    MAX_BYTES = 256 * 1024 * 1024

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
        self._write_status("waiting_for_frames")
        return path

    def _write_status(self, state: str) -> None:
        """Persist bounded live capture status without raw payloads."""
        if self.session_dir is None or self.status_path is None:
            return
        value = {
            "schema_version": 1,
            "state": str(state),
            "started_at": self.session_dir.name,
            "frames": self._frame_count,
            "bytes": self._bytes_written,
            "directions": dict(sorted(self._direction_counts.items())),
            "hosts": dict(sorted(self._host_counts.items())),
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
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.flush()
                stream.close()
            except OSError:
                pass
        if self._current_game_id:
            game = self._games.get(self._current_game_id)
            if game is not None:
                game["status"] = "incomplete"
                self._write_game_metadata(game)
            self._current_game_id = ""
        if self._bytes_written:
            try:
                self._materialize_games()
            except OSError:
                # The complete session capture remains available even if a
                # per-game forensic copy cannot be materialized.
                pass
            self._write_status("closed")
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
    def __init__(self, viewmodel: Any, *, paths: dict[str, Path] | None = None):
        self.viewmodel = viewmodel
        self.paths = paths or app_paths()
        ensure_app_paths(self.paths)
        self.log = DiagnosticLog(self.paths["logs_dir"])
        self.preferences = load_preferences(self.paths)
        self.session = SessionEngine("desktop")
        self._lock = threading.RLock()
        self._runtime: RuntimeService | None = None
        self._operation_lock = threading.Lock()
        self._debug_recorder: DebugFrameRecorder | None = None
        self._debug_capture_failed = False
        self._last_debug_capture_stats: dict[str, Any] = {}
        self._current_game_id = ""
        self._table_game_key: tuple[str, str] | None = None
        self._table_seat_confirmed = False
        self._last_frame_received_at: float | None = None
        self._active_advice_key: tuple[str, str, int] | None = None
        self._primary_engine_error: dict[str, Any] | None = None
        self._engine_error_repeats = 0
        self._model = InferenceClient(default_model_path(self.paths), on_result=self._on_advice,
                                      on_status=self._on_model_status, threads=2)
        self._attach_signals()
        self.viewmodel.set_debug_capture_enabled(
            bool(self.preferences.get("debug_capture_enabled", False))
        )
        saved_layout = self.preferences.get("floating_layout", {})
        if isinstance(saved_layout, dict):
            self.viewmodel.restore_floating_layout(saved_layout)

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
        self.viewmodel.confirmSeatRequested.connect(self.confirm_seat)
        self.viewmodel.modelImportRequested.connect(self.import_model)
        self.viewmodel.debugCaptureRequested.connect(self.set_debug_capture_enabled)


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
        if not bool(self.preferences.get("debug_capture_enabled", False)):
            self._debug_recorder = None
            return
        recorder = DebugFrameRecorder(self._debug_capture_root())
        path = recorder.start()
        self._debug_recorder = recorder
        self.viewmodel.append_log({
            "level": "INFO",
            "code": "DEBUG_CAPTURE_STARTED",
            "message": f"调试报文保存已启动：{path}",
        })

    def _close_debug_recorder(self) -> None:
        recorder, self._debug_recorder = self._debug_recorder, None
        if recorder is not None:
            path = recorder.path
            recorder.close()
            stats = recorder.stats
            self._last_debug_capture_stats = stats
            if path is not None and stats.get("frames", 0):
                self.viewmodel.append_log({
                    "level": "INFO",
                    "code": "DEBUG_CAPTURE_STOPPED",
                    "message": f"调试报文已保存：{path}",
                })

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
        if not helper.is_file():
            helper = Path(r"C:\Program Files\mitmproxy\bin\mitmdump.exe")
        addon = self.paths["runtime_dir"] / "mitmproxy" / "addon.py"
        if not addon.is_file():
            addon = Path(__file__).resolve().parents[1] / "infrastructure" / "capture" / "addon.py"
        proxy = WindowsProxyBackend().snapshot()
        upstream = None
        mode = CaptureMode.DIRECT
        if proxy.enabled and proxy.server:
            server = proxy.server.split(";", 1)[0]
            if "=" in server:
                server = server.split("=", 1)[1]
            if ":" in server:
                host, port_text = server.rsplit(":", 1)
                try:
                    port = int(port_text)
                except ValueError:
                    port = 0
                if port:
                    upstream = UpstreamProxy(kind="mixed", host=host.strip("[]"), port=port)
                    mode = CaptureMode.UPSTREAM_MIXED
        return RuntimeOptions(
            mode=mode, upstream=upstream, helper_path=helper, addon_path=addon,
            allowed_hosts=("hlxyxws.huanle.qq.com", "hlsjwshall.huanle.qq.com",
                           "long.wxagame.weixin.qq.com", "guandangamelog.huanle.qq.com"),
            require_certificate=True,
        )

    def _build_runtime(self) -> RuntimeService:
        options = self._mode_options()
        paths = RuntimePaths.from_mapping(self.paths)
        runtime = RuntimeService(paths, on_frame=self._on_frame, on_status=self._on_runtime_status,
                                 options=options, diagnostics=BoundedDiagnostics())
        return runtime

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
            status = WindowsCertificateManager(self.paths["cert_dir"]).inspect()
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
                self.session.reset()
                self._last_frame_received_at = None
                self._active_advice_key = None
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
                self._status({"phase":"error","running":False,"connected":False,"busy":False,
                              "error_message":"采集端口 18080 已被占用",
                              "detail":"请关闭旧的掼策/mitmdump 进程后再开始使用。"})
                return
            self._open_debug_recorder()
            if self._runtime is None:self._runtime=self._build_runtime()
            result=self._runtime.start()
            if result.get("ok"):
                self._model.start()
                self._status({"phase":"waiting","running":True,"connected":True,"busy":False,"network_mode":result.get("mode","direct"),"detail":"采集已启动。请关闭当前大掼蛋小程序，重新打开大掼蛋并进入“经典玩法”实际对局；只停留在首页或大厅不会产生局面。"})
            else:
                self._close_debug_recorder()
                self._status({"phase":"error","running":False,"connected":False,"busy":False,"error_message":result.get("message","采集启动失败"),"detail":"当前无法安全启动采集。"})
        except Exception as exc:
            self._close_debug_recorder()
            self.log.write("runtime_start_failed", level="ERROR", message=type(exc).__name__)
            self._status({"phase":"error","running":False,"connected":False,"busy":False,"error_message":"采集服务启动失败","detail":"请打开诊断。"})
    def stop(self) -> None:
        self._run_async("stop", self._stop_impl)

    def _stop_impl(self) -> None:
        if self._runtime is not None:
            self._runtime.stop()
        with self._lock:
            self._close_debug_recorder()
            self.session.reset()
            self._last_frame_received_at = None
            self._active_advice_key = None
            self._table_game_key = None
            self._table_seat_confirmed = False
            self.viewmodel.apply_table_state({})
            self.viewmodel.apply_observation({})
            self._current_game_id = ""
            self._primary_engine_error = None
            self._engine_error_repeats = 0
            self._model.invalidate()
        self._model.close()
        self._status({"phase":"stopped","running":False,"connected":False,"busy":False})

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

    def import_model(self, path: str) -> None:
        candidate=Path(path)
        if candidate.suffix.lower() != ".npz" or not candidate.is_file():
            self._status({"phase":"error","model_state":"error","error_message":"请选择有效的 .npz 模型文件","detail":"模型导入没有完成。"})
            return
        try:
            self._model.close()
            self._model=InferenceClient(candidate, on_result=self._on_advice, on_status=self._on_model_status, threads=2)
            self._model.start()
            self._status({"model_state":"loading","model_summary":f"正在加载：{candidate.name}","detail":"正在校验模型文件。"})
        except Exception as exc:
            self.log.write("model_import_failed", level="ERROR", message=type(exc).__name__)
            self._status({"phase":"error","model_state":"error","error_message":"模型导入失败","detail":"请查看诊断信息。"})

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
                    self._close_debug_recorder()
                    self._active_advice_key = None
                    self._model.invalidate()
                    self.viewmodel.clear_advice("调试报文保存失败，推荐已暂停。")
                    self._status({"phase":"error","error_message":str(exc),"detail":"调试报文未能保存，已暂停继续解析。"})
                    return
            if self._debug_capture_failed:
                return
            for update in self.session.feed(frame):
                self._handle_update(update)

    def _handle_update(self, update: EngineUpdate) -> None:
        if update.kind == "error":
            self._active_advice_key = None
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
                self._active_advice_key = None
                self._model.invalidate()
                self.viewmodel.clear_advice("局面已切换，等待新局推荐。")
            if not is_information:
                self._active_advice_key = None
                self._model.invalidate()
            if state_phase == "waiting_start" and game_id and self._debug_recorder is not None:
                self._debug_recorder.begin_game(game_id, source_sequence)
            if state_phase in {"ended", "settled"}:
                self._model.invalidate()
                self._active_advice_key = None
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
            self.viewmodel.apply_observation(observation.to_dict())
            table_matches = (
                self._table_game_key is None
                or self._table_game_key[1] == observation.game_id
            )
            seat_confirmed = (
                observation.my_seat in range(4)
                and (self._table_game_key is None or self._table_seat_confirmed)
            )
            if (not self.session.paused and table_matches and seat_confirmed
                    and observation.current_seat == observation.my_seat):
                self._active_advice_key = observation.key
                self._status({"phase": "thinking", "connected": True, "running": True})
                self._model.submit(observation)

    def _on_advice(self, result: dict) -> None:
        # A delayed callback must not resurrect the previous turn/game or
        # replace "confirming seat"/terminal/error status with "ready".
        with self._lock:
            key = (result.get("session_id"), result.get("game_id"), result.get("state_version"))
            if self._active_advice_key is None or key != self._active_advice_key or self.session.paused:
                return
            if self._table_game_key is not None and key[1] != self._table_game_key[1]:
                return
            self.viewmodel.apply_advice(result)
            self._status({"phase": "ready", "connected": True, "running": True})

    def _on_model_status(self, status: dict) -> None:
        if status.get("model_status") == "ready":
            self._status({"model_state":"ready","model_summary":"本地模型已就绪"})
        elif status.get("model_status") == "error":
            self._active_advice_key = None
            self._model.invalidate()
            self.viewmodel.clear_advice("模型异常，推荐已暂停。")
            self._status({"phase":"error","model_state":"error","error_message":status.get("message","模型异常")})

    def _on_runtime_status(self, status: dict) -> None:
        if status.get("state") in {"error","stop_error"}:
            self._active_advice_key = None
            self._model.invalidate()
            self.viewmodel.clear_advice("采集异常，推荐已暂停。")
            self._status({"phase":"error","error_message":status.get("message","采集异常")})
        self.viewmodel.append_log({"level":"INFO","code":str(status.get("state","RUNTIME")),"message":str(status.get("message",status.get("state","")))})

    def _status(self, data: dict) -> None:
        self.viewmodel.apply_status(data)

    def close(self) -> None:
        """Synchronously release helper, proxy ownership, recorder and model."""
        try:
            self._stop_impl()
        finally:
            self.log.close()
