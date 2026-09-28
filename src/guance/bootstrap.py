"""Composition root for the first Guance desktop build."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from .config import app_paths, ensure_app_paths, default_model_path
from .diagnostics import DiagnosticLog
from .presentation.app import create_ui
from .presentation.viewmodel import GuanceViewModel
from .application.controller import GuanceController


def run_gui(args: Any, extra: list[str] | None = None) -> int:
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("掼策")
    paths = app_paths()
    ensure_app_paths(paths)
    log = DiagnosticLog(paths['logs_dir'])
    vm = GuanceViewModel()
    controller = GuanceController(vm, paths=paths)
    vm.apply_status({"phase":"idle","model_state":"pending","model_summary":f"模型资源：{default_model_path(paths).name}","network_mode":"自动检测"})
    engine = create_ui(app, vm)
    engine._guance_keepalive = (paths, log, vm, controller)  # keep composition objects alive
    if getattr(args, "exit_after", 0):
        QTimer.singleShot(int(float(args.exit_after) * 1000), app.quit)
    return int(app.exec())
