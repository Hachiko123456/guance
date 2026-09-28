"""Qt view model. Emits intentions; never performs capture or platform operations.

All controller entry points can be called from background threads. Records are
copied and delivered to the object's Qt thread before notifying bindings.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from PySide6.QtCore import QObject, Property, QThread, Qt, QUrl, Signal, Slot

from .formatting import display_card, kind_name, safe_text, seat_name

_PHASES = {
    "idle": ("尚未启动", "点击“开始使用”后，掼策才会等待大掼蛋连接。", "neutral"),
    "preparing": ("正在准备", "正在检查本机组件，请稍候。", "info"),
    "starting": ("正在启动", "网络准备完成后，请重新打开大掼蛋。", "info"),
    "waiting": ("等待游戏连接", "采集已启动，请重新打开微信里的大掼蛋。", "info"),
    "connected": ("已连接，等待对局", "已捕获大掼蛋连接，进入牌桌后会同步局面。", "success"),
    "ready": ("对局已同步", "只显示建议，实际出牌仍由你决定。", "success"),
    "thinking": ("正在计算", "正在根据当前公开局面计算推荐。", "info"),
    "stopping": ("正在停止", "正在恢复之前的网络设置。", "info"),
    "stopped": ("助手已停止", "可再次启动，等待新的对局。", "neutral"),
    "error": ("需要处理", "当前无法安全推荐，请查看诊断。", "danger"),
    "ended": ("本局已结束", "等待下一局，旧推荐已清除。", "neutral"),
}


class GuanceViewModel(QObject):
    """Small presentation state with bounded diagnostics and no external I/O."""

    startRequested = Signal()
    stopRequested = Signal()
    authorizeRequested = Signal(bool)
    repairRequested = Signal()
    removeCertificateRequested = Signal(bool)
    modelImportRequested = Signal(str)
    exportDiagnosticsRequested = Signal()
    demoRequested = Signal()
    floatingChanged = Signal(bool)
    pageChanged = Signal()
    themeChanged = Signal()
    floatingVisibleChanged = Signal()
    statusChanged = Signal()
    observationChanged = Signal()
    adviceChanged = Signal()
    logsChanged = Signal()
    diagnosticsChanged = Signal()
    _incoming = Signal(str, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._page = "assistant"
        self._theme = "system"
        self._floating = False
        self._demo = False
        self._phase = "idle"
        self._status_title, self._status_detail, self._status_tone = _PHASES["idle"]
        self._network_mode = "自动检测"
        self._certificate_state = "unknown"
        self._certificate_summary = "首次使用需要你确认授权"
        self._certificate_fingerprint = ""
        self._model_state = "pending"
        self._model_summary = "尚未加载模型"
        self._running = False
        self._connected = False
        self._busy = False
        self._observation: dict[str, Any] = {}
        self._hand: list[dict[str, Any]] = []
        self._remaining: list[dict[str, Any]] = []
        self._candidates: list[dict[str, Any]] = []
        self._advice_state = "empty"
        self._advice_message = "进入牌桌后，推荐会显示在这里。"
        self._elapsed_ms = 0.0
        self._logs: list[dict[str, str]] = []
        self._diagnostics: list[dict[str, str]] = []
        self._last_error = ""
        self._incoming.connect(self._receive, Qt.ConnectionType.QueuedConnection)

    # Properties are read-only unless user preferences are explicitly settable.
    page = Property(str, lambda s: s._page, notify=pageChanged)
    themeMode = Property(str, lambda s: s._theme, notify=themeChanged)
    floatingVisible = Property(bool, lambda s: s._floating, notify=floatingVisibleChanged)
    demoMode = Property(bool, lambda s: s._demo, notify=statusChanged)
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
    connected = Property(bool, lambda s: s._connected, notify=statusChanged)
    running = Property(bool, lambda s: s._running, notify=statusChanged)
    busy = Property(bool, lambda s: s._busy, notify=statusChanged)
    level = Property(int, lambda s: int(s._observation.get("level", 0)), notify=observationChanged)
    mySeat = Property(int, lambda s: int(s._observation.get("my_seat", -1)), notify=observationChanged)
    currentSeat = Property(int, lambda s: int(s._observation.get("current_seat", -1)), notify=observationChanged)
    myTurn = Property(bool, lambda s: bool(s._observation) and s.mySeat == s.currentSeat, notify=observationChanged)
    turnLabel = Property(str, lambda s: seat_name(s.currentSeat, s.mySeat), notify=observationChanged)
    trick = Property(int, lambda s: int(s._observation.get("trick", 0)), notify=observationChanged)
    handCards = Property(list, lambda s: s._hand, notify=observationChanged)
    remainingCards = Property(list, lambda s: s._remaining, notify=observationChanged)
    observationAvailable = Property(bool, lambda s: bool(s._observation), notify=observationChanged)
    adviceVisible = Property(bool, lambda s: s._advice_state == "ready" and bool(s._candidates), notify=adviceChanged)
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

    @Slot()
    def request_export_diagnostics(self) -> None:
        self.exportDiagnosticsRequested.emit()

    @Slot()
    def request_demo(self) -> None:
        if not self._running and not self._busy:
            self.demoRequested.emit()

    @Slot()
    def clear_logs(self) -> None:
        self._logs = []
        self.logsChanged.emit()

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

    def apply_advice(self, data: dict[str, Any]) -> None:
        self._dispatch("advice", data)

    def append_log(self, data: dict[str, Any]) -> None:
        self._dispatch("log", data)

    def clear_advice(self, reason: str = "") -> None:
        self._dispatch("clear", {"reason": reason})

    @Slot(str, object)
    def _receive(self, name: str, data: dict[str, Any]) -> None:
        {"status": self._status, "observation": self._observe, "advice": self._advise,
         "log": self._log, "clear": self._clear}[name](data)

    @staticmethod
    def _key(data: dict[str, Any]) -> tuple[Any, Any, Any]:
        return (data.get("session_id"), data.get("game_id"), data.get("state_version"))

    def _status(self, data: dict[str, Any]) -> None:
        self._demo = bool(data.get("demo", self._demo))
        phase = str(data.get("phase", data.get("status", self._phase)))
        if phase in _PHASES and phase != self._phase:
            self._phase = phase
            self._status_title, self._status_detail, self._status_tone = _PHASES[phase]
        self._status_title = safe_text(data.get("title", self._status_title), 100)
        self._status_detail = safe_text(data.get("detail", data.get("message", self._status_detail)), 300)
        self._status_tone = str(data.get("tone", self._status_tone))
        for field in ("network_mode", "certificate_state", "certificate_summary",
                      "certificate_fingerprint", "model_state", "model_summary"):
            if field in data:
                setattr(self, f"_{field}", safe_text(data[field]))
        self._running = bool(data.get("running", self._running))
        self._connected = bool(data.get("connected", self._connected))
        self._busy = bool(data.get("busy", self._phase in {"preparing", "starting", "stopping"}))
        if self._phase in {"error", "stopped", "ended", "idle"}:
            self._clear({"reason": self._status_detail})
        if self._phase == "thinking":
            self._clear({"reason": "正在计算当前局面的推荐…", "state": "thinking"})
        if data.get("error_message"):
            self._last_error = safe_text(data["error_message"])
        if "diagnostics" in data:
            diagnostics = data["diagnostics"]
            if isinstance(diagnostics, dict):
                diagnostics = [{"label": k, "value": v} for k, v in diagnostics.items()]
            self._diagnostics = [{"label": safe_text(x.get("label", "")),
                                  "value": safe_text(x.get("value", ""))}
                                 for x in diagnostics[:40]]
        self.statusChanged.emit()
        self.diagnosticsChanged.emit()

    def _observe(self, data: dict[str, Any]) -> None:
        if self._key(data) != self._key(self._observation) or not data:
            self._clear({"reason": "局面已更新，等待新的推荐。"})
        self._observation = data
        self._hand = [display_card(x, self.level) for x in data.get("hand", [])]
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

    def _advise(self, data: dict[str, Any]) -> None:
        # Last defence against late inference; the controller remains authority.
        if not self._observation or self._key(data) != self._key(self._observation) or not self.myTurn:
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
            # FableDan's claim values are ranks (for example, [5, 7]), not
            # encoded cards. Formatting them as cards invents a suit and can
            # display a false recommendation, so keep them as rank labels.
            claims = [self._display_rank(x) for x in item.get("claim", [])]
            candidates.append({"cards": cards, "kind": kind, "pass": is_pass,
                               "claim": claims})
        self._candidates = candidates
        self._advice_state = "ready" if candidates else "empty"
        self._advice_message = "仅作建议，请在游戏内确认出牌。"
        self._elapsed_ms = float(data.get("elapsed_ms", 0))
        self.adviceChanged.emit()

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
                       "remaining": [16, 8, 12, 15]})
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
