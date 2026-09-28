"""Offscreen QML smoke tests for user-facing presentation interactions."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, QMetaObject, Qt
from PySide6.QtWidgets import QApplication

from guance.presentation.app import create_ui
from guance.presentation.viewmodel import GuanceViewModel

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def ui(qapp: QApplication):
    vm = GuanceViewModel()
    engine = create_ui(qapp, vm)
    qapp.processEvents()
    root = engine.rootObjects()[0]
    yield qapp, engine, root, vm
    root.hide()
    qapp.processEvents()


def _child(root: QObject, name: str) -> QObject:
    child = root.findChild(QObject, name)
    assert child is not None, f"QML object {name!r} was not created"
    return child


def _click(obj: QObject) -> None:
    assert QMetaObject.invokeMethod(obj, "click", Qt.ConnectionType.DirectConnection)


def test_floating_trigger_opens_closes_and_reopens(ui) -> None:
    app, _engine, root, vm = ui
    floating = _child(root, "floatingAdviceWindow")
    trigger = _child(root, "floatingButton")

    assert floating.property("visible") is False
    _click(trigger)
    app.processEvents()
    assert vm.floatingVisible is True
    assert floating.property("visible") is True

    floating.close()
    app.processEvents()
    assert vm.floatingVisible is False
    assert floating.property("visible") is False

    _click(trigger)
    app.processEvents()
    assert vm.floatingVisible is True
    assert floating.property("visible") is True


def test_start_button_requests_start_when_certificate_is_trusted(ui) -> None:
    app, _engine, root, vm = ui
    requests: list[bool] = []
    vm.startRequested.connect(lambda: requests.append(True))
    vm.apply_status({"certificate_state": "trusted"})
    app.processEvents()

    _click(_child(root, "startButton"))
    app.processEvents()
    assert requests == [True]
    assert not _child(root, "consentDialog").property("opened")


def test_start_button_opens_consent_when_certificate_is_not_trusted(ui) -> None:
    app, _engine, root, vm = ui
    requests: list[bool] = []
    vm.startRequested.connect(lambda: requests.append(True))

    _click(_child(root, "startButton"))
    app.processEvents()
    assert requests == []
    assert _child(root, "consentDialog").property("opened") is True


def test_authorize_button_emits_true_only_after_checkbox_consent(ui) -> None:
    app, _engine, root, vm = ui
    values: list[bool] = []
    vm.authorizeRequested.connect(values.append)
    consent = _child(root, "certificateConsent")
    authorize = _child(root, "authorizeButton")

    assert authorize.property("enabled") is False
    consent.setProperty("checked", True)
    app.processEvents()
    assert authorize.property("enabled") is True
    _click(authorize)
    app.processEvents()
    assert values == [True]


def test_visual_screenshot_is_available_when_requested(ui, tmp_path: Path) -> None:
    """Optional artifact for local GUI review; does not affect normal CI."""
    app, _engine, root, _vm = ui
    directory = os.environ.get("GUANCE_QML_SCREENSHOT_DIR")
    if not directory:
        pytest.skip("set GUANCE_QML_SCREENSHOT_DIR to save a visual artifact")
    app.processEvents()
    image = root.grabWindow()
    assert not image.isNull()
    destination = Path(directory) / "guance-main.png"
    destination.parent.mkdir(parents=True, exist_ok=True)
    assert image.save(str(destination))
