"""Presentation-only QQuickWindow with synchronous native resize intent."""
from __future__ import annotations

from PySide6.QtCore import Property, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtQml import QmlElement
from PySide6.QtQuick import QQuickWindow

QML_IMPORT_NAME = "Guance.Presentation"
QML_IMPORT_MAJOR_VERSION = 1


@QmlElement
class FloatingWindow(QQuickWindow):
    """Keep direct Python resize calls distinguishable from QML fit changes."""

    closeRequested = Signal()
    sizeModeChanged = Signal()
    nativeGeometryChanged = Signal()

    def __init__(self, parent: QQuickWindow | None = None) -> None:
        super().__init__(parent)
        self._size_mode = "fit"

    def _get_size_mode(self) -> str:
        return self._size_mode

    def _set_size_mode(self, value: str) -> None:
        value = str(value)
        if value not in {"fit", "manual"} or value == self._size_mode:
            return
        self._size_mode = value
        self.sizeModeChanged.emit()

    sizeMode = Property(str, _get_size_mode, _set_size_mode, notify=sizeModeChanged)
    nativeWidth = Property(int, lambda self: int(self.width()), notify=nativeGeometryChanged)
    nativeHeight = Property(int, lambda self: int(self.height()), notify=nativeGeometryChanged)

    def _mark_manual_native_resize(self) -> None:
        if self.isVisible() and not bool(self.property("applyingGeometry")) and not bool(self.property("restoringLayout")):
            self._set_size_mode("manual")
            self.setProperty("layoutDirty", True)
        self.nativeGeometryChanged.emit()

    @Slot(int)
    def setWidth(self, value: int) -> None:
        super().setWidth(value)
        self._mark_manual_native_resize()

    @Slot(int)
    def setHeight(self, value: int) -> None:
        super().setHeight(value)
        self._mark_manual_native_resize()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.closeRequested.emit()
        event.ignore()
