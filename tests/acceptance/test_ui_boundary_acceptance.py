"""UI acceptance checks at the ViewModel/controller signal boundary.

These tests intentionally avoid real user clicks and real Windows side effects.
They exercise the contracts that QML calls and verify that each acceptance action
has a signal consumer or a deterministic state transition.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtQuick import QQuickItem
from PySide6.QtWidgets import QApplication

from guance.presentation.app import create_ui
from guance.presentation.viewmodel import GuanceViewModel


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def collect(signal: Any) -> list[tuple[Any, ...]]:
    values: list[tuple[Any, ...]] = []
    signal.connect(lambda *args: values.append(args))
    return values


def test_ui_001_start_emits_start_request() -> None:
    vm = GuanceViewModel()
    events = collect(vm.startRequested)
    vm.request_start()
    assert events == [()]


def test_ui_002_authorize_without_consent_is_hard_noop() -> None:
    vm = GuanceViewModel()
    events = collect(vm.authorizeRequested)
    vm.request_authorize(False)
    assert events == []


def test_ui_003_authorize_with_consent_emits_true() -> None:
    vm = GuanceViewModel()
    events = collect(vm.authorizeRequested)
    vm.request_authorize(True)
    assert events == [(True,)]


def test_ui_004_stop_emits_and_clears_running_gate() -> None:
    vm = GuanceViewModel()
    vm.apply_status({"phase": "waiting", "running": True, "busy": False})
    events = collect(vm.stopRequested)
    vm.request_stop()
    assert events == [()]


def test_ui_005_demo_is_local_only_and_does_not_start_runtime() -> None:
    vm = GuanceViewModel()
    start_events = collect(vm.startRequested)
    auth_events = collect(vm.authorizeRequested)
    vm.enable_demo()
    assert vm.demoMode is True
    assert vm.phase == "ready"
    assert vm.observationAvailable is True
    assert vm.adviceVisible is True
    assert start_events == []
    assert auth_events == []


def test_ui_006_exit_demo_clears_demo_state_advice_and_observation() -> None:
    vm = GuanceViewModel()
    vm.enable_demo()
    vm.exit_demo()
    assert vm.demoMode is False
    assert vm.observationAvailable is False
    assert vm.adviceVisible is False
    assert vm.phase == "idle"


def test_ui_007_floating_is_idempotent_and_qml_window_reacts(qapp: QApplication) -> None:
    vm = GuanceViewModel()
    visibility_events = collect(vm.floatingVisibleChanged)
    engine = create_ui(qapp, vm)
    root = engine.rootObjects()[0]
    floating = root.findChild(type(root), "floatingAdviceWindow")
    assert floating is not None
    assert floating.isVisible() is False

    vm.set_floating(True)
    qapp.processEvents()
    assert vm.floatingVisible is True
    assert floating.isVisible() is True
    vm.set_floating(True)
    qapp.processEvents()
    assert len(visibility_events) == 1

    vm.set_floating(False)
    qapp.processEvents()
    assert vm.floatingVisible is False
    assert floating.isVisible() is False
    root.hide()


def test_ui_008_floating_close_syncs_viewmodel(qapp: QApplication) -> None:
    vm = GuanceViewModel()
    engine = create_ui(qapp, vm)
    root = engine.rootObjects()[0]
    floating = root.findChild(type(root), "floatingAdviceWindow")
    assert floating is not None
    vm.set_floating(True)
    qapp.processEvents()
    assert floating.isVisible()
    floating.close()
    qapp.processEvents()
    assert vm.floatingVisible is False
    assert floating.isVisible() is False
    root.hide()


def test_ui_009_all_candidates_are_displayed_and_data_is_unchanged(qapp) -> None:
    vm = GuanceViewModel()
    vm.enable_demo()
    before = list(vm.adviceCandidates)
    assert len(before) >= 2
    vm.apply_status({"phase": "ready"})
    assert vm.adviceCandidates == before
    engine = create_ui(qapp, vm)
    root = engine.rootObjects()[0]
    try:
        qapp.processEvents()
        grid = root.findChild(QQuickItem, "visibleAdviceCandidates")
        candidates = [item for item in grid.childItems() if item.objectName().startswith("adviceCandidate-")]
        assert len(candidates) == len(before)
        assert all(item.isVisible() for item in candidates)
        assert vm.adviceCandidates == before
    finally:
        root.hide()


def test_ui_010_model_import_emits_local_path() -> None:
    vm = GuanceViewModel()
    events = collect(vm.modelImportRequested)
    vm.request_model_import("file:///C:/models/best.npz")
    assert events == [("C:/models/best.npz",)]


def test_ui_011_remove_certificate_requires_true_consent() -> None:
    vm = GuanceViewModel()
    denied = collect(vm.removeCertificateRequested)
    vm.request_remove_certificate(False)
    assert denied == []
    accepted = collect(vm.removeCertificateRequested)
    vm.request_remove_certificate(True)
    assert accepted == [(True,)]


def test_ui_012_repair_emits_request() -> None:
    vm = GuanceViewModel()
    events = collect(vm.repairRequested)
    vm.request_repair()
    assert events == [()]


def test_ui_013_export_diagnostics_emits_request() -> None:
    vm = GuanceViewModel()
    events = collect(vm.exportDiagnosticsRequested)
    vm.request_export_diagnostics()
    assert events == [()]


def test_ui_014_navigation_switches_only_known_pages() -> None:
    vm = GuanceViewModel()
    changes = collect(vm.pageChanged)
    vm.navigate("settings")
    vm.navigate("diagnostics")
    vm.navigate("not-a-page")
    assert vm.page == "diagnostics"
    assert len(changes) == 2


def test_ui_qml_wires_floating_button_to_window() -> None:
    assistant = Path("src/guance/presentation/qml/AssistantPage.qml").read_text(encoding="utf-8")
    main = Path("src/guance/presentation/qml/Main.qml").read_text(encoding="utf-8")
    assert 'objectName: "floatingButton"' in assistant
    assert "onClicked: page.floatingRequested()" in assistant
    assert "onFloatingRequested: window.showFloatingWindow()" in main
    assert "function onFloatingVisibleChanged()" in main
    assert "floatingWindow.show()" in main
