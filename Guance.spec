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
from hashlib import sha256
import json
import os

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
MODEL_BUNDLE_ROOT = ROOT / "data" / "models"
RUNTIME_HOOK = ROOT / "scripts" / "qt_runtime_hook.py"
EXCLUDE_CAPTURE_RUNTIME = os.environ.get("GUANCE_EXPERIMENTAL_PACKAGE_NO_CAPTURE_RUNTIME") == "1"

required_inputs = [APP_ICON, APP_QML, RUNTIME_HOOK]
if not EXCLUDE_CAPTURE_RUNTIME:
    required_inputs.append(MITMPROXY_RUNTIME)
for required in required_inputs:
    if not required.exists():
        raise SystemExit(f"required packaging input is missing: {required}")

if not EXCLUDE_CAPTURE_RUNTIME:
    if not (MITMPROXY_RUNTIME / "mitmdump.exe").is_file():
        raise SystemExit(f"mitmdump.exe is missing from the prepared runtime: {MITMPROXY_RUNTIME}")
    if not (MITMPROXY_RUNTIME / "_internal").is_dir():
        raise SystemExit(
            f"mitmproxy _internal directory is missing: {MITMPROXY_RUNTIME / '_internal'}"
        )
    if not (MITMPROXY_RUNTIME / "addon.py").is_file():
        raise SystemExit(f"mitmproxy addon is missing: {MITMPROXY_RUNTIME / 'addon.py'}")


# These are imported by the application or are needed by its QML plugin graph.
# QtQuickControls2 is a QML-only import in this application, so make its
# binding explicit to ensure the corresponding Qt shared library is retained.
TARGET_HIDDEN_IMPORTS = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtNetwork",
    "PySide6.QtOpenGL",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickControls2",
    "PySide6.QtWidgets",
    # The optional marvel-gpu profile vendors Torch at build time and imports it
    # lazily at runtime, so PyInstaller's static analysis cannot see Torch's
    # standard-library imports.  These pure-Python modules are imported by
    # Torch/sympy (`timeit`, `heapq`, `pickletools`, `cProfile`, `profile`,
    # `pstats`, `colorsys`) and would otherwise be absent from the frozen
    # archive, turning a valid GPU load into MODEL_UNAVAILABLE.  Including them
    # is inert for profiles that never import Torch.
    "cProfile",
    "colorsys",
    "ctypes.util",
    "heapq",
    "pickletools",
    "profile",
    "pstats",
    "timeit",
    "unittest.mock",
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
    "Qt6QmlWorkerScript.dll",
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

# PySide6 wheels carry a coherent VC runtime set.  PyInstaller and the nested
# capture helper can otherwise contribute older copies in different search
# directories, making the Windows loader select an ABI-incompatible DLL.
CONTROLLED_CRT_NAMES = {
    "concrt140.dll", "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
    "msvcp140_codecvt_ids.dll", "vcruntime140.dll", "vcruntime140_1.dll",
}
CONTROLLED_CRT_SOURCES = []
for name in sorted(CONTROLLED_CRT_NAMES):
    source = PYSIDE6_ROOT / name
    if not source.is_file():
        raise SystemExit(f"required controlled CRT is missing: {source}")
    CONTROLLED_CRT_SOURCES.append(source)


def _is_controlled_crt(entry: tuple[str, str, str]) -> bool:
    destination = PurePosixPath(str(entry[0]).replace("\\", "/"))
    return destination.name.lower() in CONTROLLED_CRT_NAMES


def _is_uncontrolled_icu(entry: tuple[str, str, str]) -> bool:
    destination = PurePosixPath(str(entry[0]).replace("\\", "/"))
    if not destination.name.lower().startswith("icu") or not destination.name.lower().endswith(".dll"):
        return False
    try:
        source = Path(str(entry[1])).resolve()
        return not source.is_relative_to(PYSIDE6_ROOT.resolve())
    except (OSError, ValueError):
        return True


ALLOWED_QT_ROOT_DLLS = {
    "Qt6Core.dll",
    "Qt6Gui.dll",
    "Qt6LabsPlatform.dll",
    "Qt6Network.dll",
    "Qt6OpenGL.dll",
    "Qt6Qml.dll",
    "Qt6QmlMeta.dll",
    "Qt6QmlModels.dll",
    "Qt6QmlWorkerScript.dll",
    "Qt6Quick.dll",
    "Qt6QuickControls2.dll",
    "Qt6QuickControls2Basic.dll",
    "Qt6QuickControls2BasicStyleImpl.dll",
    "Qt6QuickControls2Impl.dll",
    "Qt6QmlWorkerScript.dll",
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


ALLOWED_QT_PLUGINS = {
    "PySide6/plugins/platforms/qwindows.dll",
    "PySide6/plugins/platforms/qoffscreen.dll",
    "PySide6/plugins/styles/qmodernwindowsstyle.dll",
    "PySide6/plugins/imageformats/qgif.dll",
    "PySide6/plugins/imageformats/qico.dll",
    "PySide6/plugins/imageformats/qjpeg.dll",
    "PySide6/plugins/networkinformation/qnetworklistmanager.dll",
    "PySide6/plugins/tls/qcertonlybackend.dll",
    "PySide6/plugins/tls/qschannelbackend.dll",
}


def _is_unwanted_qt_plugin(entry: tuple[str, str, str]) -> bool:
    destination = PurePosixPath(str(entry[0]).replace("\\", "/"))
    return (destination.parts[:2] == ("PySide6", "plugins")
            and destination.as_posix() not in ALLOWED_QT_PLUGINS)


def _model_bundle_datas() -> list[tuple[str, str]]:
    """Collect only declared FableDan bundle files; never copy a model tree."""
    if not MODEL_BUNDLE_ROOT.is_dir():
        return []
    entries: list[tuple[str, str]] = []
    for bundle_root in sorted(path for path in MODEL_BUNDLE_ROOT.iterdir() if path.is_dir()):
        manifest_path = bundle_root / "manifest.json"
        if not manifest_path.is_file():
            raise SystemExit(f"model bundle manifest is missing: {manifest_path}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            provider = manifest.get("provider")
            runtime = manifest.get("runtime")
            artifact = manifest.get("artifact")
            if provider != "fabledan" or runtime != "numpy" or not isinstance(artifact, str):
                raise ValueError("default package accepts only FableDan/numpy bundles")
            relative = PurePosixPath(artifact)
            if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                raise ValueError("invalid artifact path")
            artifact_path = (bundle_root.joinpath(*relative.parts)).resolve()
            if not artifact_path.is_relative_to(bundle_root.resolve()) or not artifact_path.is_file():
                raise ValueError("artifact is missing or escapes bundle")
            digest = sha256(artifact_path.read_bytes()).hexdigest()
            if manifest.get("artifact_sha256") != digest:
                raise ValueError("artifact hash does not match manifest")
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise SystemExit(f"invalid default model bundle: {manifest_path}: {exc}") from None
        allowed = {manifest_path.resolve(), artifact_path}
        sums = bundle_root / "SHA256SUMS"
        if sums.exists():
            if not sums.is_file():
                raise SystemExit(f"invalid model checksum file: {sums}")
            seen: set[str] = set()
            for line in sums.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                parts = line.split()
                if len(parts) != 2 or len(parts[0]) != 64 or any(c not in "0123456789abcdef" for c in parts[0]):
                    raise SystemExit(f"invalid model checksum entry: {sums}")
                checksum_path = PurePosixPath(parts[1].lstrip("*"))
                if checksum_path.is_absolute() or any(part in {"", ".", ".."} for part in checksum_path.parts):
                    raise SystemExit(f"invalid model checksum path: {sums}")
                key = checksum_path.as_posix()
                if key in seen:
                    raise SystemExit(f"duplicate model checksum entry: {sums}")
                seen.add(key)
                target = (bundle_root.joinpath(*checksum_path.parts)).resolve()
                if (not target.is_relative_to(bundle_root.resolve()) or not target.is_file()
                        or sha256(target.read_bytes()).hexdigest() != parts[0]):
                    raise SystemExit(f"model checksum mismatch: {sums}")
            if str(relative) not in seen or manifest.get("artifact_sha256") != digest:
                raise SystemExit(f"model checksum does not cover artifact: {sums}")
            allowed.add(sums.resolve())
        extras = [path for path in bundle_root.rglob("*") if path.is_file() and path.resolve() not in allowed]
        if extras:
            raise SystemExit(f"unexpected files in model bundle: {[str(path) for path in extras[:4]]}")
        destination = Path("data/models") / bundle_root.name
        entries.extend((str(path), (destination / path.relative_to(bundle_root).parent).as_posix())
                       for path in sorted(allowed) if path.is_file())
    return entries


MODEL_BUNDLE_DATAS = _model_bundle_datas()

datas = [
    (str(APP_ICON), "."),
    (str(APP_QML), "guance/presentation/qml"),
    *([] if EXCLUDE_CAPTURE_RUNTIME else [(str(MITMPROXY_RUNTIME), "runtime/mitmproxy")]),
    *([(str(MODEL), "data/profiles/tencent_daguandan/models")] if MODEL.is_file() else []),
    *MODEL_BUNDLE_DATAS,
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
    if (not _is_qml_entry(entry) and not _is_unwanted_qt_root_dll(entry)
        and not _is_controlled_crt(entry) and not _is_uncontrolled_icu(entry)
        and not _is_unwanted_qt_plugin(entry))
)
for source in CONTROLLED_CRT_SOURCES:
    a.binaries.append((source.name, str(source), "BINARY"))

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
