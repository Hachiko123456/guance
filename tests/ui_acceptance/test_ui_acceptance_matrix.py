from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QMetaObject, QObject, Qt


def child(root: QObject, name: str) -> QObject:
    result = root.findChild(QObject, name)
    assert result is not None, f"missing QML control: {name}"
    return result


def click(obj: QObject) -> None:
    assert QMetaObject.invokeMethod(obj, "click", Qt.ConnectionType.DirectConnection)

def click_named(root: QObject, name: str) -> QObject:
    obj = child(root, name)
    click(obj)
    return obj


def control_with_text(root: QObject, text: str) -> QObject:
    for obj in root.findChildren(QObject):
        if obj.property("text") == text:
            return obj
    raise AssertionError(f"missing text control: {text}")


def test_ui_001_start_opens_consent_or_starts_after_trust(ui) -> None:
    app, _engine, root, vm, controller = ui
    click_named(root, "startButton")
    dialog = child(root, "consentDialog")
    assert dialog.property("visible") is True
    assert vm.start_calls == 0

    dialog.close()
    vm.set_certificate_state("trusted")
    click_named(root, "startButton")
    app.processEvents()
    assert vm.start_calls == 1
    assert controller.vm.running is True


def test_ui_002_unchecked_consent_disables_authorize_and_cancel_is_noop(ui) -> None:
    app, _engine, root, vm, _controller = ui
    click_named(root, "startButton")
    dialog = child(root, "consentDialog")
    authorize = child(root, "authorizeButton")
    assert authorize.property("enabled") is False

    cancel = control_with_text(root, "暂不授权")
    click(cancel)
    app.processEvents()
    assert dialog.property("visible") is False
    assert vm.authorize_calls == []


def test_ui_003_checked_consent_emits_true_and_closes(ui) -> None:
    app, _engine, root, vm, controller = ui
    click_named(root, "startButton")
    consent = child(root, "certificateConsent")
    authorize = child(root, "authorizeButton")
    consent.setProperty("checked", True)
    app.processEvents()
    assert authorize.property("enabled") is True
    click(authorize)
    app.processEvents()
    assert vm.authorize_calls == [True]
    assert controller.authorized == 1
    assert child(root, "consentDialog").property("visible") is False


def test_ui_004_stop_emits_and_clears_recommendation(ui) -> None:
    app, _engine, root, vm, _controller = ui
    vm.set_running(True)
    vm.set_candidates([{"kind": "对子", "cards": [], "pass": False}])
    app.processEvents()
    click_named(root, "startButton")
    app.processEvents()
    assert vm.stop_calls == 1
    assert vm.running is False
    assert vm.adviceCandidates == []


def test_ui_005_demo_enters_isolated_offline_state(ui) -> None:
    app, _engine, root, vm, controller = ui
    click_named(root, "demoButton")
    app.processEvents()
    assert vm.demoMode is True
    assert vm.running is False
    assert vm.start_calls == 0
    assert vm.authorize_calls == []
    assert vm.adviceVisible is True
    assert controller.authorized == 0


def test_ui_006_exit_demo_clears_demo_state(ui) -> None:
    app, _engine, root, vm, _controller = ui
    click_named(root, "demoButton")
    click_named(root, "exitDemoButton")
    app.processEvents()
    assert vm.demoMode is False
    assert vm.adviceCandidates == []
    assert vm.phase == "idle"


def test_ui_007_floating_window_is_unique_and_repeated_open_is_idempotent(ui) -> None:
    app, _engine, root, vm, _controller = ui
    floating = child(root, "floatingAdviceWindow")
    trigger = child(root, "floatingButton")
    assert floating.property("visible") is False

    click(trigger)
    app.processEvents()
    click(trigger)
    app.processEvents()
    assert vm.floatingVisible is True
    assert floating.property("visible") is True
    assert len(root.findChildren(QObject, "floatingAdviceWindow")) == 1


def test_ui_008_floating_close_syncs_state_and_reopens(ui) -> None:
    app, _engine, root, vm, _controller = ui
    floating = child(root, "floatingAdviceWindow")
    click_named(root, "floatingButton")
    floating.close()
    app.processEvents()
    assert vm.floatingVisible is False
    assert floating.property("visible") is False

    click_named(root, "floatingButton")
    app.processEvents()
    assert vm.floatingVisible is True
    assert floating.property("visible") is True


def test_ui_009_candidate_toggle_preserves_candidates(ui) -> None:
    app, _engine, root, vm, _controller = ui
    candidates = [
        {"kind": "对子", "cards": [], "pass": False},
        {"kind": "单张", "cards": [], "pass": False},
        {"kind": "过牌", "cards": [], "pass": True},
    ]
    vm.set_candidates(candidates)
    app.processEvents()
    toggle = control_with_text(root, "查看其他候选  ⌄")
    before = list(vm.adviceCandidates)
    click(toggle)
    app.processEvents()
    assert toggle.property("checked") is True
    assert vm.adviceCandidates == before
    click(toggle)
    app.processEvents()
    assert toggle.property("checked") is False
    assert vm.adviceCandidates == before


def test_ui_010_model_dialog_cancel_and_selection_path(ui, tmp_path: Path) -> None:
    app, _engine, root, vm, controller = ui
    click_named(root, "navSettingsButton")
    click_named(root, "importModelButton")
    dialog = child(root, "modelFileDialog")
    assert dialog.property("visible") is True
    assert any("npz" in str(value).lower() for value in dialog.property("nameFilters"))

    dialog.close()
    app.processEvents()
    assert controller.imported == []

    selected = tmp_path / "best.npz"
    vm.request_model_import(str(selected))
    app.processEvents()
    assert controller.imported == [str(selected)]


def test_ui_011_remove_certificate_requires_confirmation(ui) -> None:
    app, _engine, root, vm, _controller = ui
    click_named(root, "navSettingsButton")
    click_named(root, "removeCertificateButton")
    dialog = child(root, "removeCertificateDialog")
    assert dialog.property("visible") is True

    click_named(root, "cancelRemoveButton")
    app.processEvents()
    assert dialog.property("visible") is False
    assert vm.remove_calls == []

    click_named(root, "removeCertificateButton")
    click_named(root, "confirmRemoveButton")
    app.processEvents()
    assert vm.remove_calls == [True]
    assert dialog.property("visible") is False


def test_ui_012_repair_network_emits_intent(ui) -> None:
    app, _engine, root, vm, _controller = ui
    click_named(root, "navSettingsButton")
    click_named(root, "repairNetworkButton")
    app.processEvents()
    assert vm.repair_calls == 1


def test_ui_013_diagnostics_export_emits_intent(ui) -> None:
    app, _engine, root, vm, controller = ui
    click_named(root, "navDiagnosticsButton")
    click_named(root, "exportDiagnosticsButton")
    app.processEvents()
    assert vm.export_calls == 1
    assert controller.exported == 1


def test_ui_014_top_navigation_switches_pages(ui) -> None:
    app, _engine, root, vm, _controller = ui
    click_named(root, "navSettingsButton")
    assert vm.page == "settings"
    click_named(root, "navDiagnosticsButton")
    assert vm.page == "diagnostics"
    click_named(root, "navAssistantButton")
    app.processEvents()
    assert vm.page == "assistant"
