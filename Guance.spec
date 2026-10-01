# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification for the Guance Windows onedir package.

The PySide6 wheel contains bindings, tools, every Qt module, and every QML
module. The application only needs the Qt modules imported by the Python
composition root plus the small QML import graph used by the UI. The built-in
PyInstaller Qt hooks still collect dependencies for the imported bindings; the
QML data emitted by the broad QtQml hook is filtered below and replaced with
the validated application subset.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath

import PySide6
from PyInstaller.building.build_main import Analysis, COLLECT, EXE, PYZ
from PyInstaller.building.datastruct import TOC


ROOT = Path(SPECPATH).resolve()
PYSIDE6_ROOT = Path(PySide6.__file__).resolve().parent
QML_ROOT = PYSIDE6_ROOT / "qml"


def _qml_data(source: Path, *, recursive: bool) -> list[tuple[str, str]]:
    """Return data entries that preserve the PySide6 QML import layout."""

    if not source.is_dir():
        raise SystemExit(f"required PySide6 QML directory is missing: {source}")

    files = source.rglob("*") if recursive else source.glob("*")
    entries: list[tuple[str, str]] = []
    for file in sorted(path for path in files if path.is_file()):
        relative = file.relative_to(QML_ROOT)
        destination = Path("PySide6") / "qml" / relative.parent
        entries.append((str(file), destination.as_posix()))
    return entries


TARGET_QML_DESTINATIONS: set[str] = set()


def _is_qml_entry(entry: tuple[str, str, str]) -> bool:
    """Identify QML entries emitted by PyInstaller's broad QtQml hook."""

    destination = PurePosixPath(str(entry[0]).replace("\\", "/"))
    return (
        destination.parts[:2] == ("PySide6", "qml")
        and destination.as_posix() not in TARGET_QML_DESTINATIONS
    )


APP_ICON = ROOT / "app.ico"
APP_QML = ROOT / "src" / "guance" / "presentation" / "qml"
MITMPROXY_RUNTIME = ROOT / "runtime" / "mitmproxy"
MODEL = ROOT / "data" / "profiles" / "tencent_daguandan" / "models" / "best.npz"
RUNTIME_HOOK = ROOT / "scripts" / "qt_runtime_hook.py"

for required in (APP_ICON, APP_QML, MITMPROXY_RUNTIME, MODEL, RUNTIME_HOOK):
    if not required.exists():
        raise SystemExit(f"required packaging input is missing: {required}")

if not (MITMPROXY_RUNTIME / "mitmdump.exe").is_file():
    raise SystemExit(f"mitmdump.exe is missing from the prepared runtime: {MITMPROXY_RUNTIME}")
if not (MITMPROXY_RUNTIME / "_internal").is_dir():
    raise SystemExit(f"mitmproxy _internal directory is missing: {MITMPROXY_RUNTIME / '_internal'}")
if not (MITMPROXY_RUNTIME / "addon.py").is_file():
    raise SystemExit(f"mitmproxy addon is missing: {MITMPROXY_RUNTIME / 'addon.py'}")


# These are imported by the application or are needed by its QML plugin graph.
# QtQuickControls2 is a QML-only import in this application, so make its
# binding explicit to ensure the corresponding Qt shared library is retained.
TARGET_HIDDEN_IMPORTS = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtNetwork",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickControls2",
    "PySide6.QtWidgets",
]

# QML imports used by the application:
#   QtQuick, QtQuick.Controls, QtQuick.Controls.Basic,
#   QtQuick.Layouts, QtQuick.Templates, QtQuick.Window, and Qt.labs.platform.
# Keep the base Controls/impl/Templates plugins because Basic controls depend
# on them. Other built-in styles and the large FluentWinUI3 tree are not used.
TARGET_QML_PATHS = [
    ("QtQuick", False),
    ("QtQuick/Controls", False),
    ("QtQuick/Controls/Basic", True),
    ("QtQuick/Controls/impl", True),
    ("QtQuick/Templates", False),
    ("QtQuick/Layouts", False),
    ("QtQuick/Window", False),
    ("Qt/labs/platform", True),
]

target_qml_datas: list[tuple[str, str]] = []
seen_qml_destinations: set[tuple[str, str]] = set()
for relative, recursive in TARGET_QML_PATHS:
    for source, destination in _qml_data(QML_ROOT / relative, recursive=recursive):
        key = (source, destination)
        if key not in seen_qml_destinations:
            seen_qml_destinations.add(key)
            target_qml_datas.append(key)
            TARGET_QML_DESTINATIONS.add(
                (Path(destination) / Path(source).name).as_posix()
            )

# QML plugin DLLs are data from Qt's point of view, but they must remain next
# to their qmldir files so the QML engine can discover them.
TARGET_QT_DLLS = [
    "Qt6LabsPlatform.dll",
    "Qt6OpenGL.dll",
    "Qt6QuickControls2.dll",
    "Qt6QuickControls2Basic.dll",
    "Qt6QuickControls2BasicStyleImpl.dll",
    "Qt6QuickControls2Impl.dll",
    "Qt6QuickLayouts.dll",
    "Qt6QuickTemplates2.dll",
]
target_qt_binaries: list[tuple[str, str]] = []
for name in TARGET_QT_DLLS:
    source = PYSIDE6_ROOT / name
    if not source.is_file():
        raise SystemExit(f"required Qt shared library is missing: {source}")
    # Analysis expects the destination directory for an input binary and
    # appends the source basename itself.
    target_qt_binaries.append((str(source), "PySide6"))

ALLOWED_QT_ROOT_DLLS = {
    "Qt6Core.dll",
    "Qt6Gui.dll",
    "Qt6LabsPlatform.dll",
    "Qt6Network.dll",
    "Qt6OpenGL.dll",
    "Qt6Qml.dll",
    "Qt6QmlMeta.dll",
    "Qt6QmlModels.dll",
    "Qt6Quick.dll",
    "Qt6QuickControls2.dll",
    "Qt6QuickControls2Basic.dll",
    "Qt6QuickControls2BasicStyleImpl.dll",
    "Qt6QuickControls2Impl.dll",
    "Qt6QuickLayouts.dll",
    "Qt6QuickTemplates2.dll",
    "Qt6Widgets.dll",
}


def _is_unwanted_qt_root_dll(entry: tuple[str, str, str]) -> bool:
    destination = PurePosixPath(str(entry[0]).replace("\\", "/"))
    return (
        destination.parts[:1] == ("PySide6",)
        and len(destination.parts) == 2
        and destination.name.startswith("Qt6")
        and destination.name.endswith(".dll")
        and destination.name not in ALLOWED_QT_ROOT_DLLS
    )


datas = [
    (str(APP_ICON), "."),
    (str(APP_QML), "guance/presentation/qml"),
    (str(MITMPROXY_RUNTIME), "runtime/mitmproxy"),
    (str(MODEL), "data/profiles/tencent_daguandan/models"),
    *target_qml_datas,
]
binaries = target_qt_binaries

a = Analysis(
    [str(ROOT / "src" / "guance" / "__main__.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=TARGET_HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(RUNTIME_HOOK)],
    excludes=[
        "PySide6.Qt3DAnimation",
        "PySide6.Qt3DCore",
        "PySide6.Qt3DExtras",
        "PySide6.Qt3DInput",
        "PySide6.Qt3DLogic",
        "PySide6.Qt3DRender",
        "PySide6.QtBluetooth",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtDesigner",
        "PySide6.QtGraphs",
        "PySide6.QtGraphsWidgets",
        "PySide6.QtHelp",
        "PySide6.QtLocation",
        "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets",
        "PySide6.QtOpenGL",
        "PySide6.QtOpenGLWidgets",
        "PySide6.QtPdf",
        "PySide6.QtPdfWidgets",
        "PySide6.QtPositioning",
        "PySide6.QtPrintSupport",
        "PySide6.QtQuick3D",
        "PySide6.QtQuickTest",
        "PySide6.QtRemoteObjects",
        "PySide6.QtScxml",
        "PySide6.QtSensors",
        "PySide6.QtSerialBus",
        "PySide6.QtSerialPort",
        "PySide6.QtSpatialAudio",
        "PySide6.QtSql",
        "PySide6.QtStateMachine",
        "PySide6.QtSvg",
        "PySide6.QtSvgWidgets",
        "PySide6.QtTest",
        "PySide6.QtTextToSpeech",
        "PySide6.QtUiTools",
        "PySide6.QtWebChannel",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineQuick",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebSockets",
        "PySide6.QtWebView",
        "PySide6.QtXml",
    ],
    noarchive=False,
    optimize=0,
)

# hook-PySide6.QtQml.py intentionally collects every QML module installed in
# the wheel. Keep the hook's useful Qt dependency processing, but discard
# that broad QML payload before adding the validated subset above.
a.datas = TOC(entry for entry in a.datas if not _is_qml_entry(entry))
a.binaries = TOC(
    entry
    for entry in a.binaries
    if not _is_qml_entry(entry) and not _is_unwanted_qt_root_dll(entry)
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Guance",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(APP_ICON),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Guance",
)
