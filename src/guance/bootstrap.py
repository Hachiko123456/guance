"""Composition root for the first Guance desktop build."""
from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .config import app_paths, ensure_app_paths, default_model_path
from .diagnostics import DiagnosticLog
from .presentation.app import create_ui
from .presentation.viewmodel import GuanceViewModel
from .application.controller import GuanceController
from .application.single_instance import SingleInstance, SingleInstanceError


def _application_icon_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)) / "app.ico"
    return Path(__file__).resolve().parents[2] / "app.ico"


def run_gui(args: Any, extra: list[str] | None = None) -> int:
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("掼策")
    root_holder: dict[str, Any] = {"root": None}

    def activate_existing(_message: dict[str, Any]) -> None:
        root = root_holder.get("root")
        if root is not None:
            root.show()
            root.raise_()
            root.requestActivate()

    instance = SingleInstance(on_forward=activate_existing)
    try:
        result = instance.enter({"command": "activate", "cwd": str(Path.cwd())})
    except SingleInstanceError as exc:
        print(f"Guance is already running or its single-instance endpoint is unavailable: {exc}", file=sys.stderr)
        return 2
    if not result.acquired:
        return 0
    icon_path = _application_icon_path()
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))
    paths = app_paths()
    ensure_app_paths(paths)
    log = DiagnosticLog(paths['logs_dir'])
    vm = GuanceViewModel()
    vm.apply_status({"phase":"idle","model_state":"pending","model_summary":f"模型资源：{default_model_path(paths).name}","network_mode":"自动检测"})
    controller = GuanceController(vm, paths=paths)
    recovery = controller.recover_stale_runtime_proxy()
    if not recovery.get("ok", False):
        instance.leave()
        return 1
    app.aboutToQuit.connect(controller.close)
    app.aboutToQuit.connect(instance.leave)
    controller.refresh_preflight()
    engine = create_ui(app, vm)
    root_holder["root"] = engine.rootObjects()[0] if engine.rootObjects() else None
    engine._guance_keepalive = (paths, log, vm, controller, instance)  # keep composition objects alive
    if getattr(args, "exit_after", 0):
        QTimer.singleShot(int(float(args.exit_after) * 1000), app.quit)
    return int(app.exec())
