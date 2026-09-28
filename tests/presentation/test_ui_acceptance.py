from __future__ import annotations

from PySide6.QtCore import QMetaObject, QObject, QTimer
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtWidgets import QApplication

from guance.presentation.viewmodel import GuanceViewModel


def load_ui():
    app=QApplication.instance() or QApplication([])
    vm=GuanceViewModel()
    engine=QQmlApplicationEngine(app)
    warnings=[]
    engine.warnings.connect(lambda items: warnings.extend(item.toString() for item in items))
    engine.rootContext().setContextProperty("viewModel", vm)
    from guance.presentation.app import _qml_root
    engine.load((_qml_root()/"Main.qml").as_uri())
    app.processEvents()
    assert engine.rootObjects(), warnings
    return app, engine, vm, engine.rootObjects()[0], warnings


def test_main_window_and_pages_load_without_qml_warnings():
    app, engine, vm, root, warnings=load_ui()
    assert root.findChild(type(root), "floatingAdviceWindow") is not None
    # We accept non-fatal Qt platform warnings but not QML type/property failures.
    assert not any("TypeError" in x or "unavailable" in x or "ReferenceError" in x for x in warnings), warnings
    root.hide(); app.processEvents()


def test_floating_window_show_close_and_reopen():
    app, engine, vm, root, _=load_ui()
    floating=root.findChild(type(root), "floatingAdviceWindow")
    assert floating is not None
    assert not floating.property("visible")
    vm.set_floating(True); app.processEvents()
    assert floating.property("visible") is True
    vm.set_floating(False); app.processEvents()
    assert floating.property("visible") is False
    vm.set_floating(True); app.processEvents()
    assert floating.property("visible") is True
    root.hide(); app.processEvents()


def test_authorize_signal_requires_true_consent():
    app=QApplication.instance() or QApplication([]); vm=GuanceViewModel(); values=[]
    vm.authorizeRequested.connect(values.append)
    vm.request_authorize(False); assert values==[]
    vm.request_authorize(True); assert values==[True]


def test_remove_certificate_signal_requires_true_consent():
    app=QApplication.instance() or QApplication([]); vm=GuanceViewModel(); values=[]
    vm.removeCertificateRequested.connect(values.append)
    vm.request_remove_certificate(False); assert values==[]
    vm.request_remove_certificate(True); assert values==[True]


def click(root, name):
    item = root.findChild(QObject, name)
    assert item is not None, name
    assert QMetaObject.invokeMethod(item, "click")
    QApplication.processEvents()
    return item


def test_all_primary_controls_have_stable_objects():
    app, engine, vm, root, warnings = load_ui()
    names = ["startButton", "floatingButton", "demoButton", "navAssistantButton",
             "navSettingsButton", "navDiagnosticsButton", "importModelButton",
             "removeCertificateButton", "repairNetworkButton", "exportDiagnosticsButton",
             "consentDialog", "certificateConsent", "authorizeButton",
             "removeCertificateDialog", "cancelRemoveButton", "confirmRemoveButton",
             "modelFileDialog", "exitDemoButton"]
    for name in names:
        assert root.findChild(QObject, name) is not None, name
    root.hide(); app.processEvents()


def test_navigation_and_demo_controls_change_visible_state():
    app, engine, vm, root, _ = load_ui()
    click(root, "navSettingsButton")
    assert vm.page == "settings"
    click(root, "navDiagnosticsButton")
    assert vm.page == "diagnostics"
    click(root, "navAssistantButton")
    assert vm.page == "assistant"
    click(root, "demoButton")
    assert vm.demoMode is True
    # Demo start button becomes an exit action; exit control is also available.
    click(root, "exitDemoButton")
    assert vm.demoMode is False
    root.hide(); app.processEvents()


def test_start_requires_consent_and_authorize_button_is_guarded():
    app, engine, vm, root, _ = load_ui()
    click(root, "startButton")
    dialog = root.findChild(QObject, "consentDialog")
    checkbox = root.findChild(QObject, "certificateConsent")
    authorize = root.findChild(QObject, "authorizeButton")
    assert dialog.property("visible") is True
    assert authorize.property("enabled") is False
    checkbox.setProperty("checked", True)
    app.processEvents()
    assert authorize.property("enabled") is True
    dialog.close(); root.hide(); app.processEvents()


def test_remove_certificate_requires_confirmation_dialog():
    app, engine, vm, root, _ = load_ui()
    click(root, "navSettingsButton")
    click(root, "removeCertificateButton")
    dialog = root.findChild(QObject, "removeCertificateDialog")
    assert dialog.property("visible") is True
    click(root, "cancelRemoveButton")
    assert dialog.property("visible") is False
    root.hide(); app.processEvents()


def test_floating_button_itself_opens_the_window():
    app, engine, vm, root, _ = load_ui()
    button = root.findChild(QObject, "floatingButton")
    floating = root.findChild(QObject, "floatingAdviceWindow")
    assert button is not None and floating is not None
    assert button.property("visible") is True
    assert QMetaObject.invokeMethod(button, "click")
    app.processEvents()
    assert floating.property("visible") is True
    floating.close(); root.hide(); app.processEvents()


def test_authorize_button_emits_true_and_closes_consent():
    app, engine, vm, root, _ = load_ui()
    values=[]
    vm.authorizeRequested.connect(values.append)
    click(root, "startButton")
    checkbox=root.findChild(QObject, "certificateConsent")
    checkbox.setProperty("checked", True); app.processEvents()
    click(root, "authorizeButton")
    assert values == [True]
    root.hide(); app.processEvents()


def test_repair_and_diagnostics_buttons_emit_intentions():
    app, engine, vm, root, _ = load_ui()
    repairs=[]; exports=[]
    vm.repairRequested.connect(lambda: repairs.append(True))
    vm.exportDiagnosticsRequested.connect(lambda: exports.append(True))
    click(root, "navSettingsButton"); click(root, "repairNetworkButton")
    click(root, "navDiagnosticsButton"); click(root, "exportDiagnosticsButton")
    assert repairs == [True]
    assert exports == [True]
    root.hide(); app.processEvents()


def test_model_import_control_opens_file_dialog_with_npz_filter():
    app, engine, vm, root, _ = load_ui()
    click(root, "navSettingsButton")
    click(root, "importModelButton")
    dialog=root.findChild(QObject, "modelFileDialog")
    assert dialog is not None
    filters=list(dialog.property("nameFilters"))
    assert any("npz" in str(value).lower() for value in filters)
    dialog.close(); root.hide(); app.processEvents()
