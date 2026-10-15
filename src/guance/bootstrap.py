"""Composition root for the Guance desktop application."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any

from PySide6 import __version__ as PYSIDE_VERSION
from PySide6.QtCore import QTimer, qInstallMessageHandler, qVersion
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication
import shiboken6

from .config import app_paths, ensure_app_paths
from .presentation.app import create_ui
from .presentation.viewmodel import GuanceViewModel


def _runtime_factory(paths, *, on_frame, on_status, options, diagnostics):
    from .application.runtime.service import RuntimeService
    from .infrastructure.capture.diagnostics import BoundedDiagnostics
    return RuntimeService(
        paths, on_frame=on_frame, on_status=on_status, options=options,
        diagnostics=diagnostics or BoundedDiagnostics(),
    )


def _training_recorder_factory(root, **kwargs):
    from .infrastructure.training_data import TrainingDataRecorder
    return TrainingDataRecorder(root, **kwargs)


def _proxy_snapshot_factory():
    from .infrastructure.windows.proxy import WindowsProxyBackend
    return WindowsProxyBackend().snapshot()


def _certificate_inspector(cert_dir):
    from .infrastructure.windows.certificate import WindowsCertificateManager
    return WindowsCertificateManager(cert_dir).inspect()


def _application_icon_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)) / "app.ico"
    return Path(__file__).resolve().parents[2] / "app.ico"


def _capture_helper_status(paths: dict[str, Path]) -> tuple[str, str]:
    """Report controlled helper availability without frozen-package fallback."""
    bundled = paths["runtime_dir"] / "mitmproxy" / "mitmdump.exe"
    if bundled.is_file():
        return "available", str(bundled)
    if not getattr(sys, "frozen", False):
        external = Path(r"C:\Program Files\mitmproxy\bin\mitmdump.exe")
        if external.is_file():
            return "available_external_development", str(external)
    return "unavailable", ""


def _safe_output_path(value: str) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _configure_smoke_viewmodel(vm: GuanceViewModel, args: Any) -> None:
    vm.set_theme(getattr(args, "smoke_theme", "system"))
    state = getattr(args, "smoke_state", "default")
    if state == "demo":
        vm.enable_demo()
    elif state == "unavailable":
        vm.apply_status({
            "phase": "idle",
            "model_state": "unavailable",
            "model_summary": "guandan_marvel_v2 / experimental / unavailable",
            "model_metadata": {
                "status": "unavailable", "provider": "guandan_marvel_v2",
                "backend": "guandan", "runtime": "torch-cpu",
                "runtime_version": "external-only",
                "model_version": "MARVEL-V2-v1.3-iter000100",
                "classification": "evaluation_only",
                "formal_evaluation_status": "not_run",
                "champion_status": "not_established",
                "experimental": True, "production_ready": False,
                "strength_claim": False, "public_self_supported": False,
                "capabilities": {"next_token_policy": True},
            },
            "network_mode": "验收模式：不访问网络",
        })
        vm.navigate("settings")
    elif state == "error":
        vm.apply_status({
            "phase": "error",
            "error_code": "PACKAGE_SMOKE_ERROR_FIXTURE",
            "error_message": "验收用错误状态：未访问网络，未修改代理或证书。",
            "model_state": "unavailable",
            "model_summary": "模型不可用（验收夹具）",
            "network_mode": "验收模式：不访问网络",
        })
    else:
        vm.apply_status({
            "phase": "idle", "model_state": "pending",
            "model_summary": "模型尚未加载；默认 FableDan-only 启动",
            "network_mode": "验收模式：不访问网络",
        })


def _run_frozen_smoke(args: Any) -> int:
    """Load the production QML in a bounded mode with no controller/platform I/O."""
    messages: list[dict[str, str | int]] = []

    def handler(kind, context, message):
        if len(messages) < 200:
            messages.append({
                "type": int(kind),
                "category": str(getattr(context, "category", "") or "")[:120],
                "message": str(message)[:1000],
            })

    previous_handler = qInstallMessageHandler(handler)
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("掼策 Phase 8 Smoke")
    app.setFont(QFont("Microsoft YaHei UI", 9))
    icon_path = _application_icon_path()
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))
    paths = app_paths()
    ensure_app_paths(paths)
    capture_capability, capture_helper = _capture_helper_status(paths)
    vm = GuanceViewModel()
    _configure_smoke_viewmodel(vm, args)
    engine = create_ui(app, vm)
    root = engine.rootObjects()[0]
    root.show()
    engine._guance_keepalive = (paths, vm)
    report_path = _safe_output_path(getattr(args, "smoke_report", ""))
    screenshot_path = _safe_output_path(getattr(args, "screenshot", ""))

    def finish() -> None:
        screen = root.screen() or app.primaryScreen()
        image_saved = None
        image_size = None
        if screenshot_path is not None:
            try:
                image = root.grabWindow()
                image_size = [image.width(), image.height()]
                image_saved = bool(image.save(str(screenshot_path)))
            except Exception:
                image_saved = False
        loaded_modules = set(sys.modules)
        report = {
            "schema": "guance-frozen-gui-smoke-v1",
            "frozen": bool(getattr(sys, "frozen", False)),
            "executable": str(Path(sys.executable).resolve()),
            "no_network_mode": True,
            "capture_capability": capture_capability,
            "capture_helper_present": bool(capture_helper),
            "capture_helper_path": capture_helper,
            "controller_created": False,
            "proxy_or_certificate_operations": False,
            "qtcore_imported": "PySide6.QtCore" in loaded_modules,
            "qtqml_imported": "PySide6.QtQml" in loaded_modules,
            "qtquick_imported": "PySide6.QtQuick" in loaded_modules,
            "qml_engine_created": engine is not None,
            "main_window_created": root is not None,
            "root_type": type(root).__name__,
            "root_visible": bool(root.isVisible()),
            "root_size": [int(root.width()), int(root.height())],
            "device_pixel_ratio": float(root.devicePixelRatio()),
            "qt_version": qVersion(),
            "pyside_version": PYSIDE_VERSION,
            "shiboken_version": getattr(shiboken6, "__version__", ""),
            "theme": getattr(args, "smoke_theme", "system"),
            "state": getattr(args, "smoke_state", "default"),
            "data_dir": str(paths["data_dir"].resolve()),
            "screenshot": str(screenshot_path) if screenshot_path else "",
            "screenshot_saved": image_saved,
            "screenshot_pixels": image_size,
            "screen": {
                "name": screen.name() if screen else "",
                "logical_dpi": float(screen.logicalDotsPerInch()) if screen else 0.0,
                "physical_dpi": float(screen.physicalDotsPerInch()) if screen else 0.0,
                "device_pixel_ratio": float(screen.devicePixelRatio()) if screen else 0.0,
                "geometry": ([screen.geometry().x(), screen.geometry().y(),
                              screen.geometry().width(), screen.geometry().height()] if screen else []),
            },
            "module_guards": {
                "torch_loaded": "torch" in loaded_modules,
                "external_guandan_loaded": any(
                    name == "guandan_marvel" or name.startswith("guandan_marvel.")
                    or name == "nanopokerlib" or name.startswith("nanopokerlib.")
                    for name in loaded_modules
                ),
                "native_guandan_loaded": any("_guandan_cpp" in name for name in loaded_modules),
                "marvel_runtime_loaded": (
                    "guance.infrastructure.model.marvel_v2.runtime" in loaded_modules
                    or "guance.infrastructure.model.marvel_v2.deployment_bundle" in loaded_modules
                ),
            },
            "qt_messages": messages,
        }
        ok = (report["qtcore_imported"] and report["qtqml_imported"]
              and report["qtquick_imported"] and report["qml_engine_created"]
              and report["main_window_created"] and report["root_visible"]
              and not any(report["module_guards"].values())
              and (screenshot_path is None or image_saved is True))
        report["status"] = "passed" if ok else "failed"
        if report_path is not None:
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        app.exit(0 if ok else 3)

    QTimer.singleShot(900, finish)
    hard_timeout = max(2.0, float(getattr(args, "exit_after", 0) or 8.0))
    QTimer.singleShot(int(hard_timeout * 1000), lambda: app.exit(4))
    try:
        return int(app.exec())
    finally:
        qInstallMessageHandler(previous_handler)


def run_gui(args: Any, extra: list[str] | None = None) -> int:
    if getattr(args, "frozen_smoke", False):
        return _run_frozen_smoke(args)

    from .application.controller import GuanceController
    from .application.single_instance import SingleInstance, SingleInstanceError
    from .infrastructure.capture.canonical_capture import CanonicalCaptureConverter
    from .infrastructure.capture.diagnostics import BoundedDiagnostics
    from .infrastructure.capture.live_subset import NormalExchangeLiveSubsetProvider
    from .infrastructure.model.client import default_client_factory
    from .infrastructure.protocol.live import GAME_HOST, parse_aggregate

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
        print(
            f"Guance is already running or its single-instance endpoint is unavailable: {exc}",
            file=sys.stderr,
        )
        return 2
    if not result.acquired:
        return 0
    icon_path = _application_icon_path()
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))
    paths = app_paths()
    ensure_app_paths(paths)
    capture_capability, _capture_helper = _capture_helper_status(paths)
    from .diagnostics import DiagnosticLog
    log = DiagnosticLog(paths["logs_dir"])
    vm = GuanceViewModel()
    vm.apply_status({
        "phase": "idle", "model_state": "pending",
        "model_summary": "模型尚未加载；可在设置中导入模型包",
        "network_mode": (
            "采集组件不可用（当前包未包含）"
            if capture_capability == "unavailable" else "自动检测"
        ),
    })
    controller = GuanceController(
        vm, paths=paths, model_client_factory=default_client_factory,
        aggregate_parser=parse_aggregate, accepted_host=GAME_HOST,
        runtime_factory=_runtime_factory, diagnostics_factory=BoundedDiagnostics,
        training_recorder_factory=_training_recorder_factory,
        proxy_snapshot_factory=_proxy_snapshot_factory, certificate_inspector=_certificate_inspector,
        live_subset_provider=NormalExchangeLiveSubsetProvider(
            os.environ.get("GUANCE_MARVEL_V2_SOURCE", "").strip() or None
        ),
        canonical_converter_factory=CanonicalCaptureConverter,
    )
    recovery = controller.recover_stale_runtime_proxy()
    if not recovery.get("ok", False):
        instance.leave()
        return 1
    app.aboutToQuit.connect(controller.close)
    app.aboutToQuit.connect(instance.leave)
    controller.refresh_preflight()
    engine = create_ui(app, vm)
    root_holder["root"] = engine.rootObjects()[0] if engine.rootObjects() else None
    engine._guance_keepalive = (paths, log, vm, controller, instance)
    if getattr(args, "exit_after", 0):
        QTimer.singleShot(int(float(args.exit_after) * 1000), app.quit)
    return int(app.exec())
