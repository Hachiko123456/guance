from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Property, QObject, QUrl, Signal, Slot  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


class FakeViewModel(QObject):
    """Presentation port double; it has no network, certificate, or model I/O."""

    startRequested = Signal()
    stopRequested = Signal()
    authorizeRequested = Signal(bool)
    repairRequested = Signal()
    removeCertificateRequested = Signal(bool)
    modelImportRequested = Signal(str)
    exportDiagnosticsRequested = Signal()
    pageChanged = Signal()
    themeChanged = Signal()
    floatingVisibleChanged = Signal()
    statusChanged = Signal()
    observationChanged = Signal()
    adviceChanged = Signal()
    diagnosticsChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._page = "assistant"
        self._theme = "system"
        self._floating = False
        self._demo = False
        self._phase = "idle"
        self._status_title = "尚未启动"
        self._status_detail = "点击“开始使用”后，掼策才会等待大掼蛋连接。"
        self._status_tone = "neutral"
        self._network_mode = "自动检测"
        self._certificate_state = "unknown"
        self._certificate_summary = "首次使用需要你确认授权"
        self._model_state = "ready"
        self._model_summary = "本地测试模型已就绪"
        self._running = False
        self._busy = False
        self._connected = False
        self._observation: dict[str, Any] = {}
        self._hand: list[dict[str, Any]] = []
        self._remaining: list[dict[str, Any]] = []
        self._recent_plays: list[dict[str, Any]] = []
        self._candidates: list[dict[str, Any]] = []
        self._advice_state = "empty"
        self._advice_message = "进入牌桌后，推荐会显示在这里。"
        self._diagnostics: list[dict[str, str]] = []
        self._last_error = ""
        self.start_calls = 0
        self.stop_calls = 0
        self.repair_calls = 0
        self.remove_calls: list[bool] = []
        self.authorize_calls: list[bool] = []
        self.model_import_calls: list[str] = []
        self.export_calls = 0

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
    certificateFingerprint = Property(str, lambda s: "", notify=statusChanged)
    modelState = Property(str, lambda s: s._model_state, notify=statusChanged)
    modelSummary = Property(str, lambda s: s._model_summary, notify=statusChanged)
    connected = Property(bool, lambda s: s._connected, notify=statusChanged)
    running = Property(bool, lambda s: s._running, notify=statusChanged)
    busy = Property(bool, lambda s: s._busy, notify=statusChanged)
    level = Property(int, lambda s: int(s._observation.get("level", 0)), notify=observationChanged)
    mySeat = Property(int, lambda s: int(s._observation.get("my_seat", -1)), notify=observationChanged)
    currentSeat = Property(int, lambda s: int(s._observation.get("current_seat", -1)), notify=observationChanged)
    myTurn = Property(bool, lambda s: bool(s._observation) and s.mySeat == s.currentSeat,
                      notify=observationChanged)
    turnLabel = Property(str, lambda s: "自己" if s.myTurn else "等待对手", notify=observationChanged)
    trick = Property(int, lambda s: int(s._observation.get("trick", 0)), notify=observationChanged)
    handCards = Property(list, lambda s: s._hand, notify=observationChanged)
    remainingCards = Property(list, lambda s: s._remaining, notify=observationChanged)
    recentPlays = Property(list, lambda s: s._recent_plays, notify=observationChanged)
    observationAvailable = Property(bool, lambda s: bool(s._observation), notify=observationChanged)
    boardAvailable = Property(bool, lambda s: s._demo or bool(s._observation), notify=statusChanged)
    splitLayout = Property(str, lambda s: "side", constant=True)
    displayLevel = Property(int, lambda s: s.level, notify=observationChanged)
    tableTrick = Property(int, lambda s: s.trick, notify=observationChanged)
    seatConfirmed = Property(bool, lambda s: s.mySeat in range(4), notify=observationChanged)
    displayHandCards = Property(list, lambda s: s._hand, notify=observationChanged)
    displaySeatRows = Property(list, lambda s: s._recent_plays, notify=observationChanged)
    cardCounter = Property(list, lambda s: [], constant=True)
    counterPublicRemainingTotal = Property(int, lambda s: 0, constant=True)
    adviceVisible = Property(bool, lambda s: s._advice_state == "ready" and bool(s._candidates),
                             notify=adviceChanged)
    adviceState = Property(str, lambda s: s._advice_state, notify=adviceChanged)
    adviceSubtitle = Property(str, lambda s: s._advice_message, notify=adviceChanged)
    adviceCandidates = Property(list, lambda s: s._candidates, notify=adviceChanged)
    adviceElapsedMs = Property(float, lambda s: 0.0, notify=adviceChanged)
    diagnostics = Property(list, lambda s: s._diagnostics, notify=diagnosticsChanged)
    lastError = Property(str, lambda s: s._last_error, notify=diagnosticsChanged)

    @Slot(str)
    def navigate(self, page: str) -> None:
        if page in {"assistant", "settings", "diagnostics"} and page != self._page:
            self._page = page
            self.pageChanged.emit()

    @Slot(str)
    def set_theme(self, theme: str) -> None:
        if theme in {"system", "light", "dark"} and theme != self._theme:
            self._theme = theme
            self.themeChanged.emit()

    @Slot(bool)
    def set_floating(self, visible: bool) -> None:
        visible = bool(visible)
        if visible != self._floating:
            self._floating = visible
            self.floatingVisibleChanged.emit()

    @Slot()
    def request_start(self) -> None:
        self.start_calls += 1
        self.startRequested.emit()

    @Slot()
    def request_stop(self) -> None:
        self.stop_calls += 1
        self.stopRequested.emit()

    @Slot(bool)
    def request_authorize(self, consent: bool) -> None:
        if consent:
            self.authorize_calls.append(True)
            self.authorizeRequested.emit(True)

    @Slot()
    def request_repair(self) -> None:
        self.repair_calls += 1
        self.repairRequested.emit()

    @Slot(bool)
    def request_remove_certificate(self, consent: bool) -> None:
        if consent:
            self.remove_calls.append(True)
            self.removeCertificateRequested.emit(True)

    @Slot(str)
    def request_model_import(self, path: str) -> None:
        if path:
            self.model_import_calls.append(path)
            self.modelImportRequested.emit(path)

    @Slot()
    def request_export_diagnostics(self) -> None:
        self.export_calls += 1
        self.exportDiagnosticsRequested.emit()

    @Slot()
    def enable_demo(self) -> None:
        if self._running or self._busy:
            return
        self._demo = True
        self._phase = "ready"
        self._status_title = "离线界面演示"
        self._status_detail = "示例数据，不连接微信、不加载模型、不修改网络。"
        self._status_tone = "info"
        self._advice_state = "ready"
        self._candidates = [
            {"kind": "对子", "cards": [{"rank": "5", "suit": "♠"}, {"rank": "5", "suit": "♥"}], "pass": False},
            {"kind": "单张", "cards": [{"rank": "A", "suit": "♠"}], "pass": False},
            {"kind": "过牌", "cards": [], "pass": True},
        ]
        self.statusChanged.emit()
        self.adviceChanged.emit()

    @Slot()
    def exit_demo(self) -> None:
        if self._demo:
            self._demo = False
            self._phase = "idle"
            self._status_title = "尚未启动"
            self._status_detail = "点击“开始使用”后，掼策才会等待大掼蛋连接。"
            self._advice_state = "empty"
            self._candidates = []
            self.statusChanged.emit()
            self.adviceChanged.emit()

    def set_certificate_state(self, value: str) -> None:
        self._certificate_state = value
        self.statusChanged.emit()

    def set_running(self, value: bool) -> None:
        self._running = value
        self.statusChanged.emit()

    def set_candidates(self, candidates: list[dict[str, Any]]) -> None:
        self._candidates = candidates
        self._advice_state = "ready" if candidates else "empty"
        self.adviceChanged.emit()

    def clear_recommendation(self) -> None:
        self._candidates = []
        self._advice_state = "empty"
        self.adviceChanged.emit()


class FakeController:
    """Fake application port wired to the ViewModel signals for UI-only tests."""

    def __init__(self, vm: FakeViewModel) -> None:
        self.vm = vm
        vm.startRequested.connect(self.start)
        vm.stopRequested.connect(self.stop)
        vm.authorizeRequested.connect(self.authorize)
        vm.repairRequested.connect(self.repair)
        vm.removeCertificateRequested.connect(self.remove_certificate)
        vm.modelImportRequested.connect(self.import_model)
        vm.exportDiagnosticsRequested.connect(self.export_diagnostics)
        self.authorized = 0
        self.imported: list[str] = []
        self.exported = 0

    def start(self) -> None:
        self.vm.start_calls += 0
        self.vm.set_running(True)

    def stop(self) -> None:
        self.vm.set_running(False)
        self.vm.clear_recommendation()

    def authorize(self, consent: bool) -> None:
        if consent:
            self.authorized += 1
            self.vm.set_certificate_state("trusted")
            self.vm.set_running(True)

    def repair(self) -> None:
        self.vm._last_error = ""
        self.vm.diagnosticsChanged.emit()

    def remove_certificate(self, consent: bool) -> None:
        if consent:
            self.vm._certificate_state = "missing"
            self.vm.statusChanged.emit()

    def import_model(self, path: str) -> None:
        self.imported.append(path)

    def export_diagnostics(self) -> None:
        self.exported += 1


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def ui(qapp: QApplication):
    vm = FakeViewModel()
    controller = FakeController(vm)
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("viewModel", vm)
    from guance.presentation.app import _qml_root

    engine.load(QUrl.fromLocalFile(str((_qml_root() / "Main.qml").resolve())))
    qapp.processEvents()
    assert engine.rootObjects(), "QML root did not load"
    root = engine.rootObjects()[0]
    yield qapp, engine, root, vm, controller
    root.hide()
    qapp.processEvents()


def child(root: QObject, name: str) -> QObject:
    result = root.findChild(QObject, name)
    assert result is not None, f"missing QML control: {name}"
    return result
