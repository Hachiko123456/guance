"""Application-window factory for source and frozen builds."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QUrl
from PySide6.QtQml import QQmlApplicationEngine

# Register the presentation-only floating window type before QML loads it.
from .floating_window import FloatingWindow  # noqa: F401


def _qml_root() -> Path:
    """Return the QML directory in a source checkout or PyInstaller bundle."""
    source_root = Path(__file__).resolve().parent / "qml"
    if source_root.exists():
        return source_root
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    for candidate in (
        bundle_root / "guance" / "presentation" / "qml",
        bundle_root / "presentation" / "qml",
        bundle_root / "qml",
    ):
        if candidate.exists():
            return candidate
    return source_root


def create_ui(app: Any, viewmodel: Any) -> QQmlApplicationEngine:
    """Create and load the visible QML application window.

    The caller owns the returned engine and must keep it alive for the duration
    of the application.  ``viewModel`` is the only Python object exposed to
    QML; infrastructure and platform services stay behind the controller.
    """
    engine = QQmlApplicationEngine(app)
    engine.rootContext().setContextProperty("viewModel", viewmodel)
    main_qml = _qml_root() / "Main.qml"
    if not main_qml.exists():
        raise FileNotFoundError(f"QML entry point not found: {main_qml}")
    engine.load(QUrl.fromLocalFile(str(main_qml)))
    if not engine.rootObjects():
        raise RuntimeError(f"QML failed to create a root window: {main_qml}")
    return engine
