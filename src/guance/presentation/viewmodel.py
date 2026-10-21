"""Qt view model. Emits intentions; never performs capture or platform operations.

All controller entry points can be called from background threads. Records are
copied and delivered to the object's Qt thread before notifying bindings.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from typing import Any

from PySide6.QtCore import QObject, QPoint, QRect, Property, QThread, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QGuiApplication, QWindow

from .formatting import display_card, hand_display_order, kind_name, public_play_display_order, safe_text, seat_name

_PHASES = {
    "idle": ("尚未启动", "点击“开始使用”后，掼策才会等待大掼蛋连接。", "neutral"),
    "preparing": ("正在准备", "正在检查本机组件，请稍候。", "info"),
    "starting": ("正在启动", "网络准备完成后，请关闭当前大掼蛋小程序，重新打开大掼蛋，再进入实际牌桌。", "info"),
    "waiting": ("等待游戏连接", "采集已启动，等待进入实际牌桌。", "info"),
    "connected": ("已连接，等待对局", "已捕获牌桌连接，等待发牌。", "success"),
    "confirming_seat": ("已收到牌局", "已收到牌局，待首次实际出牌确认座位；确认前不推荐。", "info"),
    "waiting_start": ("已收到牌局", "已收到手牌，等待首手实际出牌确认座位；确认前不推荐。", "info"),
    "ready": ("对局已同步", "只显示建议，实际出牌仍由你决定。", "success"),
    "thinking": ("正在计算", "正在根据当前公开局面计算推荐。", "info"),
    "stopping": ("正在停止", "正在恢复之前的网络设置。", "info"),
    "stopped": ("助手已停止", "可再次启动，等待新的对局。", "neutral"),
    "error": ("需要处理", "请查看诊断中的具体原因，修复后再试。", "danger"),
    "ended": ("本局已结束", "等待下一局，旧推荐已清除。", "neutral"),
    "settled": ("本局已结算", "等待下一局，旧推荐已清除。", "neutral"),
}


class GuanceViewModel(QObject):
    """Small presentation state with bounded diagnostics and no external I/O."""

    startRequested = Signal()
    stopRequested = Signal()
    authorizeRequested = Signal(bool)
    repairRequested = Signal()
    removeCertificateRequested = Signal(bool)
    modelImportRequested = Signal(str)
    catalogModelRequested = Signal(str)
    catalogRefreshRequested = Signal()
    exportDiagnosticsRequested = Signal()
    openDiagnosticsDirectoryRequested = Signal()
    openCaptureDirectoryRequested = Signal()
    demoRequested = Signal()
    debugCaptureRequested = Signal(bool)
    debugCaptureChanged = Signal()
    floatingChanged = Signal(bool)
    pageChanged = Signal()
    themeChanged = Signal()
    floatingVisibleChanged = Signal()
    statusChanged = Signal()
    observationChanged = Signal()
    tableChanged = Signal()
    adviceChanged = Signal()
    logsChanged = Signal()
    diagnosticsChanged = Signal()
    floatingLayoutChanged = Signal()
    floatingLayoutSaveRequested = Signal(dict)
    screenWorkAreaChanged = Signal()
    confirmSeatRequested = Signal(str)
    catalogChanged = Signal()
    _incoming = Signal(str, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._page = "assistant"
        self._theme = "system"
        self._floating = False
        self._demo = False
        self._debug_capture_enabled = False
        self._phase = "idle"
        self._status_title, self._status_detail, self._status_tone = _PHASES["idle"]
        self._network_mode = "自动检测"
        self._certificate_state = "unknown"
        self._certificate_summary = "首次使用需要你确认授权"
        self._certificate_fingerprint = ""
        self._model_state = "pending"
        self._model_summary = "尚未加载模型"
        self._catalog_entries: list[dict[str, Any]] = []
        self._catalog_problems: list[dict[str, Any]] = []
        self._active_model_id = ""
        self._active_model_summary = ""
        self._model_metadata: dict[str, Any] = {
            "status": "pending", "provider": "", "backend": "", "runtime": "",
            "runtime_version": "", "model_version": "", "capabilities": {},
            "fingerprint": "", "bundle_fingerprint": "", "deployment_fingerprint": "",
            "score_kind": "", "classification": "", "formal_evaluation_status": "",
            "champion_status": "", "experimental": False, "production_ready": False,
            "strength_claim": False, "public_self_supported": False,
        }
        self._running = False
        self._connected = False
        self._busy = False
        self._observation: dict[str, Any] = {}
        self._table: dict[str, Any] = {}
        self._table_hand: list[dict[str, Any]] = []
        self._table_rows: list[dict[str, Any]] = []
        self._table_counter: list[dict[str, Any]] = []
        self._table_unseen_total = 0
        self._table_seat_confirmed = False
        self._table_multiplier_evidence: dict[str, Any] = {}
        self._hand: list[dict[str, Any]] = []
        self._remaining: list[dict[str, Any]] = []
        self._recent_plays: list[dict[str, Any]] = []
        self._candidates: list[dict[str, Any]] = []
        self._advice_state = "empty"
        self._advice_message = "进入牌桌后，推荐会显示在这里。"
        self._elapsed_ms = 0.0
        self._logs: list[dict[str, str]] = []
        self._diagnostics: list[dict[str, str]] = []
        self._last_error = ""
        self._error_context: dict[str, str] = {}
        self._terminal_game_key: tuple[Any, Any, Any] | None = None
        self._floating_layout: dict[str, Any] = {
            "saved": False, "mode": "right", "sizeMode": "fit",
            "x": 0, "y": 0, "width": 560, "height": 460,
        }
        self._incoming.connect(self._receive, Qt.ConnectionType.QueuedConnection)
        app = QGuiApplication.instance()
        if app is not None:
            for screen in app.screens():
                self._watch_screen(screen)
            app.screenAdded.connect(self._watch_screen)
            app.screenRemoved.connect(self.screenWorkAreaChanged)

    # Properties are read-only unless user preferences are explicitly settable.
    page = Property(str, lambda s: s._page, notify=pageChanged)
    themeMode = Property(str, lambda s: s._theme, notify=themeChanged)
    floatingVisible = Property(bool, lambda s: s._floating, notify=floatingVisibleChanged)
    demoMode = Property(bool, lambda s: s._demo, notify=statusChanged)
    debugCaptureEnabled = Property(bool, lambda s: s._debug_capture_enabled, notify=debugCaptureChanged)
    phase = Property(str, lambda s: s._phase, notify=statusChanged)
    statusTitle = Property(str, lambda s: s._status_title, notify=statusChanged)
    statusDetail = Property(str, lambda s: s._status_detail, notify=statusChanged)
    statusTone = Property(str, lambda s: s._status_tone, notify=statusChanged)
    networkMode = Property(str, lambda s: s._network_mode, notify=statusChanged)
    certificateState = Property(str, lambda s: s._certificate_state, notify=statusChanged)
    certificateSummary = Property(str, lambda s: s._certificate_summary, notify=statusChanged)
    certificateFingerprint = Property(str, lambda s: s._certificate_fingerprint, notify=statusChanged)
    modelState = Property(str, lambda s: s._model_state, notify=statusChanged)
    modelSummary = Property(str, lambda s: s._model_summary, notify=statusChanged)
    modelMetadata = Property(dict, lambda s: deepcopy(s._model_metadata), notify=statusChanged)
    catalogEntries = Property(list, lambda s: deepcopy(s._catalog_entries), notify=catalogChanged)
    catalogProblems = Property(list, lambda s: deepcopy(s._catalog_problems), notify=catalogChanged)
    activeModelId = Property(str, lambda s: s._active_model_id, notify=catalogChanged)
    activeModelSummary = Property(str, lambda s: s._active_model_summary, notify=catalogChanged)
    connected = Property(bool, lambda s: s._connected, notify=statusChanged)
    running = Property(bool, lambda s: s._running, notify=statusChanged)
    busy = Property(bool, lambda s: s._busy, notify=statusChanged)
    level = Property(int, lambda s: int(s._observation.get("level", 0)), notify=observationChanged)
    mySeat = Property(int, lambda s: int(s._observation.get("my_seat", -1)), notify=observationChanged)
    currentSeat = Property(int, lambda s: int(s._observation.get("current_seat", -1)), notify=observationChanged)
    myTurn = Property(bool, lambda s: s._is_my_turn(), notify=tableChanged)
    turnLabel = Property(str, lambda s: s._turn_label(), notify=tableChanged)
    trick = Property(int, lambda s: int(s._observation.get("trick", 0)), notify=observationChanged)
    handCards = Property(list, lambda s: s._hand, notify=observationChanged)
    remainingCards = Property(list, lambda s: s._remaining, notify=observationChanged)
    recentPlays = Property(list, lambda s: s._recent_plays, notify=observationChanged)
    observationAvailable = Property(bool, lambda s: bool(s._observation), notify=observationChanged)
    tableAvailable = Property(bool, lambda s: bool(s._table), notify=tableChanged)
    seatConfirmed = Property(bool, lambda s: s._seat_confirmed(), notify=tableChanged)
    tableLevel = Property(int, lambda s: int(s._table.get("level", 0)), notify=tableChanged)
    tableMySeat = Property(int, lambda s: int(s._table.get("my_seat", -1)), notify=tableChanged)
    tableFirstSeat = Property(int, lambda s: int(s._table.get("first_seat", -1)), notify=tableChanged)
    tableCurrentSeat = Property(int, lambda s: int(s._table.get("current_seat", -1)), notify=tableChanged)
    tableTrick = Property(int, lambda s: int(s._table.get("trick", 0)), notify=tableChanged)
    tableHandCards = Property(list, lambda s: s._table_hand, notify=tableChanged)
    tableSeatRows = Property(list, lambda s: s._table_rows, notify=tableChanged)
    cardCounter = Property(list, lambda s: s._table_counter, notify=tableChanged)
    counterUnseenTotal = Property(int, lambda s: int(s._table_unseen_total), notify=tableChanged)
    counterPublicRemainingTotal = Property(int, lambda s: int(s._table_unseen_total), notify=tableChanged)
    multiplierEvidence = Property(dict, lambda s: deepcopy(s._table_multiplier_evidence), notify=tableChanged)
    multiplierSummary = Property(str, lambda s: s._multiplier_summary(), notify=tableChanged)
    statusLabel = Property(str, lambda s: s._status_label(), notify=statusChanged)
    floatingLayout = Property(dict, lambda s: deepcopy(s._floating_layout), notify=floatingLayoutChanged)
    seatOrderLabel = Property(str, lambda s: s._seat_order_label(), notify=tableChanged)
    boardAvailable = Property(bool, lambda s: bool(s._table) or bool(s._observation), notify=tableChanged)
    displayLevel = Property(int, lambda s: int(s._table.get("level", s._observation.get("level", 0))), notify=tableChanged)
    displayHandCards = Property(list, lambda s: s._table_hand if s._table else s._hand, notify=tableChanged)
    displaySeatRows = Property(list, lambda s: s._table_rows if s._table else s._recent_plays, notify=tableChanged)
    adviceVisible = Property(bool, lambda s: s._advice_is_visible(), notify=adviceChanged)
    adviceState = Property(str, lambda s: s._advice_state, notify=adviceChanged)
    adviceSubtitle = Property(str, lambda s: s._advice_message, notify=adviceChanged)
    adviceCandidates = Property(list, lambda s: s._candidates, notify=adviceChanged)
    adviceElapsedMs = Property(float, lambda s: s._elapsed_ms, notify=adviceChanged)
    logs = Property(list, lambda s: s._logs, notify=logsChanged)
    diagnostics = Property(list, lambda s: s._diagnostics, notify=diagnosticsChanged)
    lastError = Property(str, lambda s: s._last_error, notify=diagnosticsChanged)

    @Slot(str)
    def navigate(self, page: str) -> None:
        if page in {"assistant", "settings", "diagnostics"} and self._page != page:
            self._page = page
            self.pageChanged.emit()


    @Slot(bool)
    def set_debug_capture_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._debug_capture_enabled == enabled:
            return
        self._debug_capture_enabled = enabled
        self.debugCaptureChanged.emit()
        self.debugCaptureRequested.emit(enabled)


    @Slot(str)
    def set_theme(self, theme: str) -> None:
        if theme in {"system", "light", "dark"} and theme != self._theme:
            self._theme = theme
            self.themeChanged.emit()

    @Slot(bool)
    def set_floating(self, visible: bool) -> None:
        if self._floating != visible:
            self._floating = visible
            self.floatingVisibleChanged.emit()
            self.floatingChanged.emit(visible)

    @Slot(str)
    def request_confirm_seat(self, relation: str) -> None:
        """Request a manual relative-seat confirmation from the session engine."""
        relation = str(relation).strip().lower()
        if relation not in {"self", "left", "partner", "right"}:
            return
        if not self._table or self._table_seat_confirmed:
            return
        self.confirmSeatRequested.emit(relation)

    @Slot()
    def request_start(self) -> None:
        if not self._busy and not self._running and not self._demo:
            self.startRequested.emit()

    @Slot()
    def request_stop(self) -> None:
        if self._demo:
            self.exit_demo()
        elif not self._busy:
            self.stopRequested.emit()

    @Slot(bool)
    def request_authorize(self, consent: bool) -> None:
        # A button click alone is not enough: the checkbox must be explicit.
        if consent and not self._demo and not self._busy:
            self.authorizeRequested.emit(bool(consent))

    @Slot()
    def request_repair(self) -> None:
        if not self._demo and not self._busy:
            self.repairRequested.emit()

    @Slot(bool)
    def request_remove_certificate(self, consent: bool) -> None:
        if consent and not self._demo and not self._running and not self._busy:
            self.removeCertificateRequested.emit(bool(consent))

    @Slot(str)
    def request_model_import(self, path: str) -> None:
        if not self._demo and path and not self._running and not self._busy:
            url = QUrl(path)
            self.modelImportRequested.emit(url.toLocalFile() if url.isLocalFile() else path)

    @Slot(str)
    def request_catalog_model(self, model_id: str) -> None:
        value = model_id if isinstance(model_id, str) else ""
        if not self._demo and value.strip() and not self._running and not self._busy:
            self.catalogModelRequested.emit(value.strip())

    @Slot()
    def request_catalog_refresh(self) -> None:
        if not self._demo and not self._busy:
            self.catalogRefreshRequested.emit()

    def set_catalog(self, entries: Any, problems: Any, active_model_id: str,
                    active_model_summary: str) -> None:
        """Project bounded catalog discovery results into read-only QML properties."""
        def text(value: Any, limit: int) -> str:
            return safe_text(value, limit) if isinstance(value, str) else ""

        projected: list[dict[str, Any]] = []
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, Mapping):
                continue
            projected.append({
                "model_id": text(entry.get("model_id"), 128),
                "directory": text(entry.get("directory"), 128),
                "provider": text(entry.get("provider"), 64),
                "backend": text(entry.get("backend"), 64),
                "runtime_contract": text(entry.get("runtime_contract"), 64),
                "classification": text(entry.get("classification"), 64),
                "experimental": entry.get("experimental") is True,
                "production_ready": entry.get("production_ready") is True,
                "strength_claim": entry.get("strength_claim") is True,
                "fingerprint": text(entry.get("artifact_sha256"), 64)[:12],
            })
        bounded_problems: list[dict[str, Any]] = []
        for problem in problems if isinstance(problems, list) else []:
            if not isinstance(problem, Mapping):
                continue
            if len(bounded_problems) >= 32:
                break
            bounded_problems.append({"directory": text(problem.get("directory"), 128),
                                     "code": text(problem.get("code"), 64)})
        self._catalog_entries = projected
        self._catalog_problems = bounded_problems
        self._active_model_id = text(active_model_id, 128)
        self._active_model_summary = text(active_model_summary, 200)
        self.catalogChanged.emit()

    @Slot()
    def request_export_diagnostics(self) -> None:
        self.exportDiagnosticsRequested.emit()

    @Slot()
    def request_open_diagnostics_directory(self) -> None:
        """Ask the application layer to open the diagnostics export directory."""
        self.openDiagnosticsDirectoryRequested.emit()

    @Slot()
    def request_open_capture_directory(self) -> None:
        """Ask the application layer to open the current capture directory."""
        self.openCaptureDirectoryRequested.emit()

    def _watch_screen(self, screen: QObject) -> None:
        screen.availableGeometryChanged.connect(self.screenWorkAreaChanged)
        screen.geometryChanged.connect(self.screenWorkAreaChanged)
        self.screenWorkAreaChanged.emit()

    @Slot(QObject, result=dict)
    @Slot(float, float, result=dict)
    def screen_work_area(self, x: float | QObject = 0, y: float = 0) -> dict[str, int]:
        """Return logical work-area bounds, including frame clearance for windows."""
        app = QGuiApplication.instance()
        window = x if isinstance(x, QWindow) else None
        try:
            point = window.position() if window else QPoint(int(x), int(y))
        except (TypeError, ValueError, OverflowError):
            point = QPoint(0, 0)
        screen = (app.screenAt(point) or app.primaryScreen()) if app else None
        if window and app and app.screens():
            frame = window.frameGeometry()
            def overlap(candidate: Any) -> tuple[int, int]:
                rect = candidate.availableGeometry()
                shared = frame.intersected(rect)
                distance = (frame.center() - rect.center()).manhattanLength()
                return max(0, shared.width()) * max(0, shared.height()), -distance
            screen = max(app.screens(), key=overlap)
        rect = screen.availableGeometry() if screen else QRect(0, 0, 1280, 720)
        if window:
            margins = window.frameMargins()
            rect = rect.adjusted(margins.left() + 6, margins.top() + 6,
                                 -margins.right() - 6, -margins.bottom() - 6)
        return {"x": int(rect.x()), "y": int(rect.y()),
                "width": max(1, int(rect.width())), "height": max(1, int(rect.height()))}

    @staticmethod
    def _normalize_floating_layout(value: Mapping[str, Any]) -> dict[str, Any]:
        mode = str(value.get("mode", "right"))
        if mode not in {"right", "left", "top", "bottom", "free"}:
            mode = "right"
        saved = bool(value.get("saved", False))
        has_size_mode = "sizeMode" in value
        size_mode = str(value.get("sizeMode", "manual" if saved else "fit"))
        if size_mode not in {"fit", "manual"}:
            size_mode = "manual" if saved else "fit"
        def number(key: str, default: int) -> int:
            try:
                return int(value.get(key, default))
            except (TypeError, ValueError, OverflowError):
                return default
        normalized = {"saved": saved, "mode": mode,
                      "x": number("x", 0), "y": number("y", 0),
                      "width": max(1, number("width", 560)),
                      "height": max(1, number("height", 760))}
        # Legacy preferences predate sizeMode. Preserve their exact shape on
        # the persistence path; QML already treats a saved layout without this
        # optional field as manual. New snapshots carry the explicit mode.
        if has_size_mode or not saved:
            normalized["sizeMode"] = size_mode
        return normalized

    @Slot(dict)
    def restore_floating_layout(self, value: dict[str, Any]) -> None:
        """Install saved geometry without emitting a save request."""
        if not isinstance(value, Mapping):
            return
        normalized = self._normalize_floating_layout(value)
        if normalized != self._floating_layout:
            self._floating_layout = normalized
            self.floatingLayoutChanged.emit()

    @Slot(dict)
    def save_floating_layout(self, value: dict[str, Any]) -> None:
        """Expose a bounded persistence intent; the lead/controller owns storage."""
        if not isinstance(value, Mapping):
            return
        normalized = self._normalize_floating_layout(value)
        self._floating_layout = normalized
        self.floatingLayoutChanged.emit()
        self.floatingLayoutSaveRequested.emit(deepcopy(normalized))

    @Slot()
    def clear_last_error(self) -> None:
        """Clear only the presentation error banner; never changes runtime state."""
        if self._last_error or self._error_context:
            self._last_error = ""
            self._error_context = {}
            self.diagnosticsChanged.emit()

    @Slot()
    def request_demo(self) -> None:
        if not self._running and not self._busy:
            self.demoRequested.emit()

    @Slot()
    def clear_logs(self) -> None:
        self._logs = []
        self.logsChanged.emit()

    def set_model_selection_generation(self, generation: int) -> None:
        """Revoke already-queued model records before the next UI-thread delivery."""
        current = getattr(self, '_model_selection_generation', 0)
        if generation > current:
            self._model_selection_generation = generation
            self._model_ui_sequence = 0

    def _dispatch(self, name: str, record: dict[str, Any]) -> None:
        value = deepcopy(record)
        if QThread.currentThread() == self.thread():
            self._receive(name, value)
        else:
            self._incoming.emit(name, value)

    def apply_status(self, data: dict[str, Any]) -> None:
        self._dispatch("status", data)

    def apply_observation(self, data: dict[str, Any]) -> None:
        self._dispatch("observation", data)

    def apply_table_state(self, data: dict[str, Any]) -> None:
        """Receive the authoritative display snapshot from the core state engine."""
        self._dispatch("table", data)

    def apply_advice(self, data: dict[str, Any]) -> None:
        self._dispatch("advice", data)

    def append_log(self, data: dict[str, Any]) -> None:
        self._dispatch("log", data)

    def clear_advice(self, reason: str = '', *, generation: int | None = None,
                     sequence: int | None = None) -> None:
        record = {'reason': reason}
        if generation is not None:
            record['_model_selection_generation'] = generation
        if sequence is not None:
            record['_model_ui_sequence'] = sequence
        self._dispatch('clear', record)

    @Slot(str, object)
    def _receive(self, name: str, data: dict[str, Any]) -> None:
        generation = data.get('_model_selection_generation')
        if generation is not None:
            if generation != getattr(self, '_model_selection_generation', 0):
                return
            sequence = data.get('_model_ui_sequence')
            if type(sequence) is int:
                if sequence <= getattr(self, '_model_ui_sequence', 0):
                    return
                self._model_ui_sequence = sequence
        {"status": self._status, "observation": self._observe, "table": self._table_state,
         "advice": self._advise, "log": self._log, "clear": self._clear}[name](data)

    @staticmethod
    def _key(data: dict[str, Any]) -> tuple[Any, Any, Any]:
        return (data.get("session_id"), data.get("game_id"), data.get("state_version"))

    def _table_state(self, data: dict[str, Any]) -> None:
        if not isinstance(data, Mapping):
            return
        if not data:
            self._clear_table()
            return
        table = dict(data)
        incoming_key = self._key(table)
        previous_key = self._key(self._table)
        if self._table and incoming_key[:2] != previous_key[:2]:
            self._table = {}
            self._table_hand = []
            self._table_rows = []
            self._table_counter = []
            self._table_unseen_total = 0
            self._table_seat_confirmed = False
            self._table_multiplier_evidence = {}
            self._clear({"reason": "已切换到新一局，等待新的推荐。"})
            if self._observation and self._key(self._observation)[:2] != incoming_key[:2]:
                self._clear_observation()
        self._table = deepcopy(table)
        level = int(table.get("level", 0) or 0)
        self._table_hand = [display_card(value, level) for value in hand_display_order(list(table.get("hand", [])), level)]
        raw_seat = table.get("my_seat", -1)
        try:
            my_seat = int(raw_seat)
        except (TypeError, ValueError):
            my_seat = -1
        self._table_seat_confirmed = bool(table.get("seat_confirmed", my_seat in range(4))) and my_seat in range(4)
        evidence = table.get("multiplier_evidence", {})
        self._table_multiplier_evidence = deepcopy(evidence) if isinstance(evidence, Mapping) else {}
        self._table_rows = self._build_table_rows(table, level, my_seat)
        self._table_counter, self._table_unseen_total = self._build_counter(table)
        self.tableChanged.emit()
        # Advice visibility also depends on the table turn/seat confirmation.
        self.adviceChanged.emit()

    def _build_table_rows(self, data: Mapping[str, Any], level: int, my_seat: int) -> list[dict[str, Any]]:
        history = [item for item in data.get("history", ()) if isinstance(item, Mapping)]
        latest_trick = max((int(item.get("trick", 0) or 0) for item in history), default=0)
        selected: dict[int, Mapping[str, Any]] = {}
        for item in history:
            try:
                seat = int(item.get("player", -1))
            except (TypeError, ValueError):
                seat = -1
            if seat in range(4) and int(item.get("trick", 0) or 0) == latest_trick:
                selected[seat] = item
        order = [(my_seat + offset) % 4 for offset in (0, 3, 2, 1)] if my_seat in range(4) else [0, 1, 2, 3]
        remaining = data.get("remaining", ())
        finish_ranks = data.get("finish_ranks_by_seat", ())
        raw_current = data.get("current_seat", -1)
        try:
            current_seat = int(raw_current)
        except (TypeError, ValueError):
            current_seat = -1
        phase = str(data.get("phase", self._phase)).strip().lower()
        live = phase not in {"idle", "error", "stopped", "stopping", "ended", "settled", "game_over", "finished", "complete", "settlement"}
        rows: list[dict[str, Any]] = []
        for index, seat in enumerate(order):
            item = selected.get(seat)
            kind = str(item.get("kind", "none")) if item else "none"
            cards = [display_card(card, level) for card in public_play_display_order(item.get("cards", ()), level)] if item else []
            try:
                count = int(remaining[seat])
            except (IndexError, KeyError, TypeError, ValueError):
                count = 0
            if my_seat in range(4):
                label = seat_name(seat, my_seat)
                absolute = f"座位 {seat}"
            else:
                label = f"座位 {seat}"
                absolute = label
            finish_rank = -1
            try:
                if 0 <= seat < len(finish_ranks):
                    candidate_rank = int(finish_ranks[seat])
                    if candidate_rank in range(1, 5):
                        finish_rank = candidate_rank
            except (TypeError, ValueError):
                finish_rank = -1
            rank_labels = {1: "头游", 2: "二游", 3: "三游", 4: "末游"}
            rows.append({
                "seat": seat,
                "name": label,
                "absolute": absolute,
                "arrow": "" if index == 0 else "→",
                "count": count,
                "finishRank": finish_rank,
                "finishRankLabel": rank_labels.get(finish_rank, ""),
                "active": live and seat == current_seat,
                "kind": kind,
                "kindLabel": "过牌" if kind == "pass" else ("出牌" if kind == "play" else "未出牌"),
                "cards": cards,
            })
        return rows

    def _build_counter(self, data: Mapping[str, Any]) -> tuple[list[dict[str, Any]], int]:
        raw = data.get("card_counter", ())
        by_rank: dict[str, Mapping[str, Any]] = {}
        if isinstance(raw, Mapping):
            raw = [{"rank": rank, **(value if isinstance(value, Mapping) else {})} for rank, value in raw.items()]
        for item in raw if isinstance(raw, (list, tuple)) else ():
            if isinstance(item, Mapping):
                by_rank[str(item.get("rank", ""))] = item
        rank_values = list(range(1, 14)) + [14, 15]
        result: list[dict[str, Any]] = []
        for rank in rank_values:
            item = by_rank.get(str(rank), {})
            def number(key: str) -> int:
                try:
                    return max(0, int(item.get(key, 0) or 0))
                except (TypeError, ValueError):
                    return 0
            total_value = number("total")
            if total_value <= 0:
                total_value = 2 if rank in (14, 15) else 8
            played_value = number("played")
            remaining_value = number("remaining")
            if "remaining" not in item:
                remaining_value = max(0, total_value - played_value)
            result.append({
                "rank": rank,
                "label": self._display_rank(rank),
                "total": total_value,
                "played": played_value,
                "inHand": number("in_hand"),
                "remaining": remaining_value,
                # Compatibility alias for older QML/tests; it now means public remaining.
                "unseen": remaining_value,
                "isLevel": bool(item.get("is_level", False)),
            })
        try:
            total = int(data.get("counter_public_remaining_total", data.get("counter_unseen_total", sum(item["remaining"] for item in result))) or 0)
        except (TypeError, ValueError):
            total = sum(item["unseen"] for item in result)
        return result, max(0, total)

    def _seat_confirmed(self) -> bool:
        if self._table:
            return self._table_seat_confirmed
        try:
            return int(self._observation.get("my_seat", -1)) in range(4)
        except (TypeError, ValueError):
            return False

    def _is_live_phase(self) -> bool:
        return self._phase not in {"idle", "error", "stopped", "stopping", "ended", "settled"}

    def _refresh_table_activity(self) -> None:
        if not self._table_rows:
            return
        current = self.tableCurrentSeat
        live = self._is_live_phase()
        self._table_rows = [{**row, "active": bool(live and row.get("seat") == current)}
                            for row in self._table_rows]

    def _advice_is_visible(self) -> bool:
        if not self._is_live_phase() or self._advice_state != "ready" or not self._candidates:
            return False
        if self._table:
            return self._table_seat_confirmed and self._is_my_turn()
        return bool(self._observation) and self._is_my_turn()

    def _status_label(self) -> str:
        if self._phase in {"ended", "settled"}:
            return "本局结束"
        if self._phase == "error":
            return "需要处理"
        if self._table and not self._table_seat_confirmed:
            return "待确认"
        if self._table or self._observation:
            return "对局中"
        return "等待牌局"

    def _multiplier_summary(self) -> str:
        evidence = self._table_multiplier_evidence
        values = evidence.get("reported_values") if isinstance(evidence, Mapping) else None
        if not isinstance(values, (list, tuple)) or not values:
            return "倍数待确认"
        clean: list[str] = []
        for value in values:
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if number == number and abs(number) != float("inf"):
                clean.append(f"{number:g}")
        return "倍数证据：" + " / ".join(clean) if clean else "倍数待确认"

    def _is_my_turn(self) -> bool:
        if not self._is_live_phase():
            return False
        if self._table:
            if not self._table_seat_confirmed or self.tableMySeat < 0:
                return False
            if self.tableCurrentSeat >= 0:
                return self.tableMySeat == self.tableCurrentSeat
            # Legacy observations may carry the current seat while an older
            # table snapshot has not populated it yet.
            return bool(self._observation) and self.mySeat == self.currentSeat
        return bool(self._observation) and self.mySeat == self.currentSeat

    def _turn_label(self) -> str:
        if not self._is_live_phase():
            return "—"
        if self._table:
            if not self._table_seat_confirmed:
                if self.tableCurrentSeat >= 0:
                    return f"座位 {self.tableCurrentSeat}"
                return "座位待确认"
            if self.tableCurrentSeat >= 0:
                return seat_name(self.tableCurrentSeat, self.tableMySeat)
            return seat_name(self.currentSeat, self.mySeat)
        return seat_name(self.currentSeat, self.mySeat)

    def _seat_order_label(self) -> str:
        if self._seat_confirmed():
            return "逆时针：自己 → 右家 → 对家 → 左家"
        if self._table:
            return "绝对座位：座位 0 → 座位 1 → 座位 2 → 座位 3（待确认）"
        return ""

    @staticmethod
    def _project_model_metadata(value: Any, status: str) -> dict[str, Any]:
        """Expose only bounded, provider-neutral metadata to QML."""
        source = value if isinstance(value, Mapping) else {}

        def text(key: str, *aliases: str, limit: int = 200) -> str:
            raw = source.get(key)
            if raw in (None, ""):
                for alias in aliases:
                    raw = source.get(alias)
                    if raw not in (None, ""):
                        break
            return safe_text(raw, limit).strip()

        raw_capabilities = source.get("capabilities", {})
        capabilities: dict[str, bool] = {}
        if isinstance(raw_capabilities, Mapping):
            for key, enabled in raw_capabilities.items():
                if isinstance(key, str) and key and type(enabled) is bool and len(key) <= 64:
                    capabilities[key] = enabled
        result = {
            "status": text("status", limit=32) or safe_text(status, 32),
            "provider": text("provider"), "backend": text("backend"),
            "runtime": text("runtime"), "model_version": text("model_version"),
            "capabilities": capabilities,
            "fingerprint": text("fingerprint", "model_sha256", limit=128),
        }
        for key, limit in (
            ("runtime_version", 80), ("runtime_contract", 80), ("bundle_fingerprint", 128),
            ("deployment_fingerprint", 128), ("score_kind", 80),
            ("classification", 80), ("formal_evaluation_status", 80),
            ("champion_status", 80), ("requested_device", 16), ("actual_device", 16),
            ("device_reason", 64), ("torch_cuda_build", 32), ("device_evidence", 128),
        ):
            value = text(key, limit=limit)
            if value:
                result[key] = value
        iteration = source.get("iteration")
        if type(iteration) is int and 0 <= iteration < 2**31:
            result["iteration"] = iteration
        for key in ("experimental", "production_ready", "strength_claim", "public_self_supported",
                    "device_fallback"):
            if type(source.get(key)) is bool:
                result[key] = source[key]
        summary = GuanceViewModel._device_summary(result)
        if summary:
            result["device_summary"] = summary
        return result

    _DEVICE_REASON_LABELS = {
        "CUDA_PROBE_OK": "CUDA 执行探测成功",
        "FORCED_CPU": "已强制使用 CPU",
        "LEGACY_CPU_CONTRACT": "旧 CPU 契约",
        "CPU_ONLY_TORCH": "当前 Torch 为 CPU-only 构建",
        "NO_CUDA_DEVICE": "未检测到可用 CUDA 设备",
        "CUDA_NOT_AVAILABLE": "CUDA 不可用",
        "CUDA_INIT_UNAVAILABLE": "CUDA 初始化不可用",
        "CUDA_PROBE_OOM": "CUDA 显存不足",
        "CUDA_PROBE_FAILED": "CUDA 执行探测失败",
        "TORCH_UNAVAILABLE": "Torch 不可用",
    }

    @staticmethod
    def _device_summary(metadata: Mapping[str, Any]) -> str:
        requested = str(metadata.get("requested_device", "") or "")
        actual = str(metadata.get("actual_device", "") or "")
        reason = str(metadata.get("device_reason", "") or "")
        label = GuanceViewModel._DEVICE_REASON_LABELS.get(reason, reason)
        if actual.startswith("cuda:"):
            return safe_text(f"当前使用 GPU 推理（{actual}）", 160)
        if actual == "cpu":
            if metadata.get("device_fallback") is True:
                detail = f"GPU 不可用，当前使用 CPU 推理：{label}"
                if requested:
                    detail += f"（请求：{requested}）"
                return safe_text(detail, 160)
            if reason == "FORCED_CPU":
                return "已强制使用 CPU 推理"
            if reason == "LEGACY_CPU_CONTRACT":
                return "旧 CPU 契约：当前使用 CPU 推理"
            return "当前使用 CPU 推理"
        if reason:
            detail = f"推理设备尚未确定：{label}"
            if requested:
                detail += f"（请求：{requested}）"
            return safe_text(detail, 160)
        return ""

    def _status(self, data: dict[str, Any]) -> None:
        self._demo = bool(data.get("demo", self._demo))
        raw_phase = str(data.get("phase", data.get("status", self._phase))).strip().lower()
        phase = {
            "game_over": "ended",
            "finished": "ended",
            "complete": "ended",
            "settlement": "settled",
            "complete_settlement": "settled",
        }.get(raw_phase, raw_phase)
        if phase in _PHASES and phase != self._phase:
            self._phase = phase
            self._status_title, self._status_detail, self._status_tone = _PHASES[phase]
        self._status_title = safe_text(data.get("title", self._status_title), 100)
        self._status_detail = safe_text(data.get("detail", data.get("message", self._status_detail)), 300)
        self._status_tone = str(data.get("tone", self._status_tone))

        # Runtime status records may carry the certificate as a nested, Qt-free
        # mapping. Flatten it here so QML never reaches into infrastructure.
        certificate = data.get("certificate")
        if isinstance(certificate, Mapping):
            if "certificate_state" not in data and "state" in certificate:
                data = {**data, "certificate_state": certificate.get("state")}
            if "certificate_fingerprint" not in data and "fingerprint" in certificate:
                data = {**data, "certificate_fingerprint": certificate.get("fingerprint")}
            if "certificate_summary" not in data:
                summary = certificate.get("summary", certificate.get("message", ""))
                if summary:
                    data = {**data, "certificate_summary": summary}

        aliases = {
            "networkMode": "network_mode",
            "certificateState": "certificate_state",
            "certificateSummary": "certificate_summary",
            "certificateFingerprint": "certificate_fingerprint",
            "modelState": "model_state",
            "modelSummary": "model_summary",
        }
        for camel, snake in aliases.items():
            if snake not in data and camel in data:
                data = {**data, snake: data[camel]}
        for field in ("network_mode", "model_state", "model_summary"):
            if field in data:
                setattr(self, f"_{field}", safe_text(data[field]))
        if "model_metadata" in data:
            metadata_status = safe_text(data.get("model_state", self._model_state), 32)
            self._model_metadata = self._project_model_metadata(
                data.get("model_metadata"), metadata_status
            )
        elif "model_state" in data:
            self._model_metadata = {
                **self._model_metadata, "status": safe_text(data["model_state"], 32)
            }
        if "certificate_state" in data:
            self._certificate_state = safe_text(data["certificate_state"]).strip().lower()
        if "certificate_fingerprint" in data:
            self._certificate_fingerprint = safe_text(data["certificate_fingerprint"], 128)
        if "certificate_summary" in data:
            self._certificate_summary = self._localize_certificate_summary(
                self._certificate_state, data["certificate_summary"]
            )

        self._running = bool(data.get("running", self._running))
        self._connected = bool(data.get("connected", self._connected))
        # A started collector is not a connected game. The first game frame
        # is the only evidence that should flip this flag before observation.
        if phase == "waiting" and not self._observation:
            self._connected = False
        self._busy = bool(data.get("busy", self._phase in {"preparing", "starting", "stopping"}))
        if self._phase in {"error", "stopped", "stopping", "ended", "settled", "idle"}:
            if self._phase in {"error", "stopped", "stopping", "idle"}:
                self._clear_table()
            if self._phase in {"ended", "settled"}:
                if self._observation:
                    self._terminal_game_key = self._key(self._observation)
            self._clear_observation()
            self._clear({"reason": self._status_detail})
        if self._phase == "thinking":
            self._clear({"reason": "正在计算当前局面的推荐…", "state": "thinking"})

        if phase == "error":
            raw_error = data.get("error_message") or data.get("detail") or data.get("message")
            self._last_error = self._localize_error(raw_error, data.get("error_code", ""))
            self._error_context = self._capture_error_context(data)
            self._status_detail = self._last_error
        elif "error_message" in data:
            # An explicit empty error_message is the controller's clear signal.
            self._last_error = self._localize_error(data.get("error_message", ""), "")
            if not self._last_error:
                self._error_context = {}
        else:
            # Success, idle, waiting, and stop updates must not retain a prior
            # operation's failure in the diagnostics page or first screen.
            self._last_error = ""
            self._error_context = {}
        if "diagnostics" in data:
            diagnostics = data["diagnostics"]
            if isinstance(diagnostics, dict):
                diagnostics = [{"label": k, "value": v} for k, v in diagnostics.items()]
            self._diagnostics = [{"label": safe_text(x.get("label", "")),
                                  "value": safe_text(x.get("value", ""))}
                                 for x in diagnostics[:40]]
        self._refresh_table_activity()
        self.statusChanged.emit()
        self.tableChanged.emit()
        self.adviceChanged.emit()
        self.diagnosticsChanged.emit()

    def _capture_error_context(self, data: Mapping[str, Any]) -> dict[str, str]:
        """Keep only bounded, named diagnostic fields for safe error copying."""
        context: dict[str, str] = {}
        for key, limit in (("error_code", 120), ("game_id", 200), ("state_version", 80)):
            value = data.get(key)
            if value is None or value == "":
                value = self._observation.get(key)
            if value is not None and value != "":
                text = safe_text(value, limit).strip()
                if text:
                    context[key] = text
        return context

    @staticmethod
    def _localize_certificate_summary(state: str, value: Any) -> str:
        text = safe_text(value, 300).strip()
        lower = text.lower()
        if any("一" <= char <= "鿿" for char in text):
            return text
        if state in {"trusted", "ready", "installed", "already_trusted"}:
            return "本机证书已受信任，可以直接开始使用。"
        if "not present" in lower or "missing" in lower:
            return "尚未生成本机证书；点击“开始使用”会先显示授权说明。"
        if "already trusted" in lower or "trust check completed" in lower:
            return "本机证书已受信任，可以直接开始使用。"
        return "尚未确认本机证书信任状态；首次使用会先显示授权说明。"

    @staticmethod
    def _localize_error(value: Any, error_code: Any = "") -> str:
        text = safe_text(value, 500).strip()
        if not text:
            return ""
        if any("一" <= char <= "鿿" for char in text):
            return text
        haystack = f"{error_code} {text}".lower()
        if "certificate" in haystack or "capture ca" in haystack or "trusted" in haystack:
            return "本机证书尚未受信任。请点击“开始使用”，在授权框中勾选同意后继续；未授权不会修改网络。"
        if "helper" in haystack and "not found" in haystack:
            return "未找到采集组件。请修复安装并确认 mitmdump.exe 已存在后重试。"
        if "addon" in haystack and "not found" in haystack:
            return "未找到采集规则文件。请修复安装后重试。"
        if "listener" in haystack or "port" in haystack:
            return "采集组件未能启动监听。请检查端口 18080 是否被占用，修复后再试。"
        if "proxy" in haystack:
            return "本机代理设置未能安全应用。请点击“修复网络”恢复设置后重试。"
        if "model" in haystack:
            return "本地模型未能加载。请在“设置”中导入有效的 .npz 模型文件后重试。"
        if "capture" in haystack or "runtime" in haystack:
            return "采集服务未能启动。请打开“诊断”查看具体原因，修复后再试。"
        # Unknown messages are diagnostic evidence; do not hide them behind a generic label.
        return text

    def _build_recent_plays(self, data: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Build a bounded public-information board for QML.

        Only confirmed public actions from the current/latest trick are exposed.
        No opponent hand or settlement information is included.
        """
        if not data or self.mySeat not in range(4):
            return []
        raw_history = data.get("history", ())
        history = [item for item in raw_history if isinstance(item, Mapping)]
        selected: dict[int, Mapping[str, Any]] = {}
        if history:
            latest_trick = max(int(item.get("trick", 0)) for item in history)
            for item in history:
                if int(item.get("trick", 0)) == latest_trick:
                    try:
                        seat = int(item.get("player", -1))
                    except (TypeError, ValueError):
                        seat = -1
                    if seat in range(4):
                        selected[seat] = item

        remaining = data.get("remaining", ())
        result: list[dict[str, Any]] = []
        live = self._is_live_phase()
        # Daguandan seats are read counter-clockwise from the player's view:
        # self, right, partner, left.
        for offset in (0, 3, 2, 1):
            seat = (self.mySeat + offset) % 4
            action = selected.get(seat)
            if action is None:
                kind = "none"
                kind_label = "未出牌"
                cards: list[dict[str, Any]] = []
                card_text = "未出牌"
            elif str(action.get("kind", "play")) == "pass":
                kind = "pass"
                kind_label = "过牌"
                cards = []
                card_text = "过牌"
            else:
                kind = "play"
                kind_label = "出牌"
                cards = [display_card(card, self.level) for card in public_play_display_order(action.get("cards", ()), self.level)]
                card_text = " ".join(str(card.get("label", "?")) for card in cards) or "出牌"
            try:
                count = int(remaining[seat])
            except (IndexError, KeyError, TypeError, ValueError):
                count = 0
            result.append({
                "name": seat_name(seat, self.mySeat),
                "seat": seat,
                "count": count,
                "finishRank": -1,
                "finishRankLabel": "",
                "active": live and seat == self.currentSeat,
                "kind": kind,
                "kindLabel": kind_label,
                "cards": cards,
                "cardText": card_text,
            })
        return result

    def _clear_table(self) -> None:
        had_table = bool(self._table or self._table_hand or self._table_rows or self._table_counter)
        self._table = {}
        self._table_hand = []
        self._table_rows = []
        self._table_counter = []
        self._table_unseen_total = 0
        self._table_seat_confirmed = False
        self._table_multiplier_evidence = {}
        if had_table:
            self.tableChanged.emit()

    def _clear_observation(self) -> None:
        """Remove all current-game presentation data at a terminal boundary."""
        had_observation = bool(self._observation or self._hand or self._remaining or self._recent_plays)
        self._observation = {}
        self._hand = []
        self._remaining = []
        self._recent_plays = []
        if had_observation:
            self.observationChanged.emit()
            self.tableChanged.emit()

    def _observe(self, data: dict[str, Any]) -> None:
        if self._phase in {"error", "stopped", "stopping"} and data:
            return
        if not data:
            self._clear_observation()
            self._clear({"reason": "等待下一局局面。"})
            self.observationChanged.emit()
            return

        incoming_key = self._key(data)
        if self._phase in {"idle", "waiting", "connected", "confirming_seat", "waiting_start"}:
            self._phase = "ready"
            self._status_title, self._status_detail, self._status_tone = _PHASES["ready"]
        if self._phase in {"ended", "settled"}:
            # A late frame from the finished game must not resurrect its UI.
            if self._terminal_game_key is not None and incoming_key[:2] == self._terminal_game_key[:2]:
                return
            self._phase = "ready"
            self._status_title, self._status_detail, self._status_tone = _PHASES["ready"]
            self._terminal_game_key = None
            phase_changed = True
        else:
            phase_changed = False

        if incoming_key != self._key(self._observation):
            self._clear({"reason": "局面已更新，等待新的推荐。"})
        # Install the new snapshot before deriving presentation rows. The
        # row builder uses mySeat/currentSeat/level properties, so building it
        # first would mark the active player from the previous frame.
        self._observation = data
        self._recent_plays = self._build_recent_plays(data)
        connected_changed = False
        if data and self._running and not self._demo and not self._connected:
            self._connected = True
            connected_changed = True
        self._hand = [display_card(x, self.level) for x in hand_display_order(list(data.get("hand", [])), self.level)]
        remaining = data.get("remaining", [])
        self._remaining = []
        if len(remaining) == 4 and self.mySeat in range(4):
            for offset in (0, 3, 2, 1):
                seat = (self.mySeat + offset) % 4
                self._remaining.append({"name": seat_name(seat, self.mySeat), "seat": seat,
                                        "count": remaining[seat], "active": seat == self.currentSeat,
                                        "partner": offset in (0, 2)})
        if not self.myTurn:
            self._clear({"reason": "等待其他玩家行动。"})
        self.observationChanged.emit()
        self.tableChanged.emit()
        if connected_changed or phase_changed:
            self.statusChanged.emit()

    def _advise(self, data: dict[str, Any]) -> None:
        # Last defence against late inference; the controller remains authority.
        if not self._is_live_phase() or not self._observation or self._key(data) != self._key(self._observation) or not self.myTurn:
            return
        if self._table and not self._table_seat_confirmed:
            return
        status = str(data.get("status", "ready"))
        if status != "ready":
            self._clear({"state": status, "reason": safe_text(data.get("error_message", "推荐暂不可用。"))})
            return
        candidates = []
        for item in data.get("candidates", [])[:5]:
            raw_kind = item.get("kind", "")
            is_pass = raw_kind == 0 or raw_kind == "0" or str(raw_kind).lower() in {"pass", "过牌"}
            kind_key = "pass" if is_pass else str(raw_kind)
            cards = [display_card(x, self.level) for x in item.get("cards", [])]
            kind = kind_name(kind_key)
            claims = self._display_claims(item, data)
            score = item.get("score")
            if type(score) not in (int, float):
                score = None
            score_kind = safe_text(item.get("score_kind", data.get("score_kind", "")), 80)
            score_label = ("策略/路径分数（非胜率）"
                           if any(token in score_kind.lower() for token in ("policy", "path", "marvel"))
                           else "模型分数")
            candidates.append({"cards": cards, "kind": kind, "pass": is_pass,
                               "claim": claims, "score": score, "scoreKind": score_kind,
                               "scoreLabel": score_label})
        self._candidates = candidates
        self._advice_state = "ready" if candidates else "empty"
        self._advice_message = "仅作建议，请在游戏内确认出牌。"
        self._elapsed_ms = float(data.get("elapsed_ms", 0))
        self.adviceChanged.emit()

    @classmethod
    def _display_claims(cls, item: Mapping[str, Any], advice: Mapping[str, Any]) -> list[str]:
        """Format claims only for explicit legacy/FableDan rank semantics."""
        claims = item.get("claim", [])
        if not isinstance(claims, (list, tuple)):
            return []
        claim_kind = safe_text(item.get("claim_kind", ""), 64).strip().lower()
        provider = safe_text(advice.get("provider", ""), 64).strip().lower()
        model_version = safe_text(advice.get("model_version", ""), 64).strip().lower()
        metadata = advice.get("metadata")
        legacy_metadata = isinstance(metadata, Mapping) and bool(metadata.get("legacy"))
        if claim_kind == "fabledan_rank_index":
            return [cls._format_fabledan_rank_index(value) for value in claims]
        explicit_legacy = (
            not provider
            or (provider in {"fabledan", "builtin.fabledan"}
                and (model_version in {"", "legacy"} or legacy_metadata))
        )
        if explicit_legacy:
            return [cls._display_rank(value) for value in claims]
        # Unknown provider claims are opaque annotations, not ranks, cards, or
        # probabilities. Keep a bounded textual representation without decoding.
        return [safe_text(value, 24) for value in claims]

    @classmethod
    def _format_fabledan_rank_index(cls, value: Any) -> str:
        """Explicit compatibility formatter for the canonical FableDan claim kind."""
        return cls._display_rank(value)

    @staticmethod
    def _display_rank(value: Any) -> str:
        if isinstance(value, dict):
            return safe_text(value.get("rank", "?"), 12)
        try:
            rank = int(value)
        except (TypeError, ValueError):
            return safe_text(value, 12)
        return {1: "A", 11: "J", 12: "Q", 13: "K", 14: "小王", 15: "大王"}.get(
            rank, str(rank)
        )

    def _clear(self, data: dict[str, Any]) -> None:
        self._candidates = []
        self._advice_state = data.get("state", "empty")
        self._advice_message = safe_text(data.get("reason", "")) or "状态更新后会重新计算推荐。"
        self._elapsed_ms = 0.0
        self.adviceChanged.emit()

    def _log(self, data: dict[str, Any]) -> None:
        self._logs = [*self._logs[-299:], {
            "time": safe_text(data.get("time", data.get("timestamp", datetime.now().strftime("%H:%M:%S"))), 40),
            "level": safe_text(data.get("level", "INFO"), 12),
            "code": safe_text(data.get("code", data.get("error_code", "")), 80),
            "message": safe_text(data.get("message", "")),
        }]
        self.logsChanged.emit()

    @Slot()
    def enable_demo(self) -> None:
        """Deterministic visual fixture. Never emits start/authorize/model requests."""
        if self._running and not self._demo:
            return
        self._demo = True
        self._status({"demo": True, "phase": "ready", "running": False, "connected": False,
                      "title": "离线界面演示", "detail": "示例数据，不连接微信、不加载模型、不修改网络。",
                      "tone": "info"})
        key = {"session_id": "ui-demo", "game_id": "fixture", "state_version": 4}
        self._observe({**key, "level": 5, "my_seat": 2, "current_seat": 2, "first_seat": 2,
                       "trick": 4, "hand": [0x15, 0x25, 0x11, 0x21, 0x3D, 0x1B, 0x2B,
                                             0x4A, 0x38, 0x47, 0x26, 0x13],
                       "remaining": [16, 8, 12, 15],
                       "history": [{"index": 1, "player": 2, "kind": "play",
                                    "cards": [0x15, 0x25], "trick": 4},
                                   {"index": 2, "player": 1, "kind": "pass",
                                    "cards": [], "trick": 4},
                                   {"index": 3, "player": 0, "kind": "play",
                                    "cards": [0x11], "trick": 4}]})
        self._advise({**key, "status": "ready", "elapsed_ms": 0,
                      "candidates": [{"kind": "pair", "cards": [0x11, 0x21]},
                                     {"kind": "pair", "cards": [0x1B, 0x2B]},
                                     {"kind": "pass", "cards": []}]})
        self._log({"level": "INFO", "code": "UI_DEMO", "message": "离线界面演示已加载，未执行采集或授权。"})

    @Slot()
    def exit_demo(self) -> None:
        if self._demo:
            self._observe({})
            self._status({"demo": False, "phase": "idle", "running": False, "connected": False})
